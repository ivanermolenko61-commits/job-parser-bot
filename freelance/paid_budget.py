"""Резерв Yandex после гранта. Суммы в миллионных долях рубля."""
import json
import logging
import os
import uuid
from datetime import date, datetime, timedelta

from freelance.yandex_budget import today_msk


MODEL = "aliceai-llm-flash"
RUB = 1_000_000
DAILY = 15 * RUB
MONTHLY = 500 * RUB
TOTAL = 100 * RUB
PILOT_DAYS = 7
# С запасом над длиной системного и пользовательского сообщений в UTF-8.
INPUT_BOUND = 32_768
INPUT_BYTE_LIMIT = 16_384
OUTPUT_LIMIT = 2_500
INPUT_MICROS = 100        # 0,10 ₽ за 1000 токенов
OUTPUT_MICROS = 200       # 0,20 ₽ за 1000 токенов


def start_date() -> date | None:
    if (os.getenv("YANDEX_PAID_PILOT") or "").strip() != "1":
        return None
    try:
        return date.fromisoformat(os.getenv("YANDEX_PAID_PILOT_START", ""))
    except ValueError:
        return None


def active(now: datetime | None = None) -> bool:
    start = start_date()
    return start is not None and 0 <= (today_msk(now) - start).days < PILOT_DAYS


def worst_micros(model: str, body: dict) -> int | None:
    if model != MODEL or not isinstance(body.get("messages"), list):
        return None
    output = body.get("max_tokens")
    if type(output) is not int or not 0 < output <= OUTPUT_LIMIT:
        return None
    messages = json.dumps(body["messages"], ensure_ascii=False).encode("utf-8")
    if len(messages) > INPUT_BYTE_LIMIT:
        return None
    return INPUT_BOUND * INPUT_MICROS + output * OUTPUT_MICROS


def _read(conn, day: date) -> dict:
    iso_day, month = day.isoformat(), day.strftime("%Y-%m")
    row = conn.execute("""
        SELECT min(pilot_start) AS start, max(state='overrun') AS overrun,
            COALESCE(sum(CASE WHEN day=? THEN micros ELSE 0 END),0) AS day,
            COALESCE(sum(CASE WHEN substr(day,1,7)=? THEN micros ELSE 0 END),0) AS month,
            COALESCE(sum(micros),0) AS total,
            COALESCE(sum(CASE WHEN state='reserved' THEN micros ELSE 0 END),0) AS held
        FROM yandex_paid_ledger
    """, (iso_day, month)).fetchone()
    return dict(row)


def reserve(model: str, body: dict, now: datetime | None = None) -> str | None:
    """Коммит до HTTP. После сбоя процесса незакрытый резерв продолжает занимать лимит."""
    from database import _connect

    start, day = start_date(), today_msk(now)
    worst = worst_micros(model, body)
    if start is None or not 0 <= (day - start).days < PILOT_DAYS or worst is None:
        return None
    token = uuid.uuid4().hex
    try:
        with _connect(timeout=1) as conn:
            conn.execute("BEGIN IMMEDIATE")
            usage = _read(conn, day)
            if usage["overrun"] or usage["start"] not in (None, start.isoformat()):
                return None
            if min(DAILY - usage["day"], MONTHLY - usage["month"], TOTAL - usage["total"]) < worst:
                return None
            conn.execute("INSERT INTO yandex_paid_ledger VALUES (?,?,?,?, 'reserved')",
                         (token, start.isoformat(), day.isoformat(), worst))
    except Exception:
        logging.exception("[AI] Yandex: резерв не записан, запрос остановлен")
        return None
    return token


def finish(token: str, usage=None, *, definitely_not_sent: bool = False) -> None:
    """Один раз заменяет резерв на расход. Неясный исход оставляет полную сумму."""
    from database import _connect

    prompt = usage.get("prompt_tokens") if isinstance(usage, dict) else None
    completion = usage.get("completion_tokens") if isinstance(usage, dict) else None
    valid = type(prompt) is int and type(completion) is int and prompt > 0 and completion >= 0
    if not definitely_not_sent and not valid:
        return
    try:
        with _connect(timeout=1) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT micros FROM yandex_paid_ledger WHERE token=? AND state='reserved'",
                               (token,)).fetchone()
            if row is None:
                return
            if definitely_not_sent:
                micros, state = 0, "released"
            else:
                # Вход считаем по полной цене, даже если провайдер указал часть как кешированную.
                micros = min(prompt * INPUT_MICROS + completion * OUTPUT_MICROS, 10**15)
                state = "settled" if micros <= row[0] else "overrun"
                if state == "overrun":
                    logging.error("[AI] Yandex: расход превысил резерв, платный режим заблокирован")
            conn.execute("UPDATE yandex_paid_ledger SET micros=?,state=? WHERE token=?", (micros, state, token))
    except Exception:
        logging.exception("[AI] Yandex: сверка расхода не записана, резерв сохранён")


def summary(now: datetime | None = None) -> dict:
    from database import _connect

    start = start_date()
    state = {"configured": start is not None, "active": active(now), "known": False,
             "day_rub": 0, "month_rub": 0, "total_rub": 0, "held_rub": 0, "reason": ""}
    try:
        with _connect(timeout=1) as conn:
            usage = _read(conn, today_msk(now))
    except Exception:
        state.update(active=False, reason="учёт недоступен")
        return state
    state.update(known=True, day_rub=usage["day"] / RUB, month_rub=usage["month"] / RUB,
                 total_rub=usage["total"] / RUB, held_rub=usage["held"] / RUB)
    # Тот же верхний резерв, который используется для запроса с OUTPUT_LIMIT.
    worst = INPUT_BOUND * INPUT_MICROS + OUTPUT_LIMIT * OUTPUT_MICROS
    if usage["overrun"]:
        state.update(active=False, reason="фактический расход превысил резерв")
    elif start is not None and usage["start"] not in (None, start.isoformat()):
        state.update(active=False, reason="дата пилота уже зафиксирована")
    elif min(MONTHLY - usage["month"], TOTAL - usage["total"]) < worst:
        state.update(active=False, reason="лимит пилота или месяца исчерпан")
    elif DAILY - usage["day"] < worst:
        state["reason"] = "дневной лимит исчерпан"
    return state
