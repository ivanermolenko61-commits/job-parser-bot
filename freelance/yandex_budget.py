"""Бюджет гранта Yandex Cloud на AI-оценку: рубли считаются по токенам каждого ответа.

Грант конечен (на 11.10.2026 - 3 762 ₽, действует до 21.11.2026), поэтому:
  • с даты YANDEX_UNTIL (00:00 МСК) Яндекс не вызывается никогда, какие бы ни были настройки;
  • за всё время тратим не больше YANDEX_BUDGET_RUB;
  • за сутки (МСК) - не больше «остаток бюджета / дней до отключения»: грант расходуется
    равномерно, недотраченное за день переходит на следующие дни.
Перед запросом резервируется худшая цена запроса (чтобы 4 потока вместе не вышли за лимит),
после ответа резерв заменяется фактической ценой по usage из ответа.
Учёт хранится в kv (ключ yandex_spent, JSON) - рестарт и деплой его не обнуляют.
Если учёт не удалось прочитать из БД, Яндекс не вызывается (отказ по умолчанию): иначе
траты считались бы с нуля и первая же запись затёрла бы настоящий итог.
"""
import json
import logging
import os
import threading
import time
from datetime import date, datetime
from zoneinfo import ZoneInfo

MSK = ZoneInfo("Europe/Moscow")
# С этого дня (00:00 МСК) Яндекс выключен: грант действует до 21.11.2026, день - запас
DEFAULT_UNTIL = "2026-11-20"
GRANT_START = date(2026, 10, 11)   # с этого дня расходуем грант (для потолка дня)
MAX_DAY_FACTOR = 2                 # перенос недотраченного: в день не больше 2 «ровных» долей
# Из 3 762 ₽ гранта: ~180 ₽ на сравнение моделей (tools/yandex_eval) и ~280 ₽ запаса на погрешность
DEFAULT_BUDGET_RUB = 3300.0
WARN_DAYS = 3                 # за сколько дней до отключения предупредить в Telegram
KV_KEY = "yandex_spent"

# ₽ за 1000 токенов с НДС, синхронный режим (aistudio.yandex.ru/docs/ru/ai-studio/pricing, 11.10.2026):
# (входящие, исходящие, кешированные входящие). Модель не из таблицы не вызывается: цена неизвестна.
# gpt-oss-120b/20b и qwen3 не берём: Яндекс снимает их с поддержки 30.10.2026.
PRICES = {
    "aliceai-llm-flash": (0.1, 0.2, 0.025),
    "aliceai-llm": (0.5, 1.2, 0.5),
    "yandexgpt-5-lite": (0.2, 0.2, 0.2),
    "yandexgpt-5.1": (0.8, 0.8, 0.8),
}
# Худший запрос для резерва: промпт с длинным заказом (обычно ~3 тыс. токенов) и max_tokens ответа
WORST_PROMPT_TOKENS = 6000

_lock = threading.Lock()
_save_lock = threading.Lock()  # пара «снимок + запись в kv»: последняя запись - самая свежая
_state: dict | None = None    # {"total": ₽ за всё время, "day": "YYYY-MM-DD" (МСК), "day_spent": ₽ за этот день}
_pending = 0.0                # зарезервировано под запросы, ответ на которые ещё не пришёл
_load_error_logged = 0.0      # monotonic: когда последний раз писали об ошибке чтения учёта
LOAD_ERROR_LOG_SEC = 600


def _env_date(name: str, default: str) -> date:
    raw = (os.getenv(name) or "").strip() or default
    try:
        return date.fromisoformat(raw)
    except ValueError:
        logging.warning(f"[AI] {name}={raw!r}: не дата ГГГГ-ММ-ДД, беру {default}")
        return date.fromisoformat(default)


def until() -> date:
    """День, с которого Яндекс выключен. Позже даты по умолчанию не сдвигается: грант кончается."""
    return min(_env_date("YANDEX_UNTIL", DEFAULT_UNTIL), date.fromisoformat(DEFAULT_UNTIL))


def budget() -> float:
    raw = (os.getenv("YANDEX_BUDGET_RUB") or "").strip()
    try:
        value = float(raw) if raw else DEFAULT_BUDGET_RUB
    except ValueError:
        logging.warning(f"[AI] YANDEX_BUDGET_RUB={raw!r}: не число, беру {DEFAULT_BUDGET_RUB:g}")
        value = DEFAULT_BUDGET_RUB
    return max(0.0, min(value, DEFAULT_BUDGET_RUB))   # больше гранта поставить нельзя


def today_msk(now: datetime | None = None) -> date:
    return (now or datetime.now(MSK)).astimezone(MSK).date()


def days_left(now: datetime | None = None) -> int:
    """Сколько дней (включая сегодняшний) Яндекс ещё работает; 0 и меньше - выключен."""
    return (until() - today_msk(now)).days


def expired(now: datetime | None = None) -> bool:
    return days_left(now) <= 0


def cost(model: str, prompt_tokens: float, completion_tokens: float, cached_tokens: float = 0) -> float:
    price_in, price_out, price_cached = PRICES[model]
    cached = min(max(0.0, cached_tokens), prompt_tokens)
    return ((prompt_tokens - cached) * price_in + cached * price_cached
            + completion_tokens * price_out) / 1000


def worst_cost(model: str, max_tokens: int) -> float:
    return cost(model, WORST_PROMPT_TOKENS, max_tokens)


def usage_cost(model: str, usage) -> float | None:
    """Цена ответа по usage (OpenAI-формат); None - usage нет или он нечитаемый."""
    if not isinstance(usage, dict):
        return None
    try:
        prompt = float(usage.get("prompt_tokens") or 0)
        completion = float(usage.get("completion_tokens") or 0)
        details = usage.get("prompt_tokens_details")
        cached = float(details.get("cached_tokens") or 0) if isinstance(details, dict) else 0.0
    except (TypeError, ValueError):
        return None
    if prompt <= 0 and completion <= 0:
        return None
    return cost(model, prompt, completion, cached)


def _load() -> dict | None:
    """Учёт из kv (под _lock); успешно прочитанный кешируется до конца процесса.
    None - прочитать не удалось (ошибка БД или испорченное значение): Яндекс не вызываем,
    повторим чтение при следующем обращении. Пустой kv - честный ноль (трат ещё не было)."""
    global _state, _load_error_logged
    if _state is None:
        try:
            from database import kv_get
            raw = kv_get(KV_KEY)
            state = {"total": 0.0, "day": "", "day_spent": 0.0}
            if raw:
                data = json.loads(raw)
                state = {"total": max(0.0, float(data["total"])),
                         "day": str(data.get("day") or ""),
                         "day_spent": max(0.0, float(data.get("day_spent") or 0))}
        except Exception as e:
            if time.monotonic() - _load_error_logged >= LOAD_ERROR_LOG_SEC or not _load_error_logged:
                _load_error_logged = time.monotonic()   # не чаще раза в 10 минут: вызов на каждый заказ
                logging.error(f"[AI] yandex: учёт расходов гранта не прочитан из БД ({type(e).__name__}), "
                              f"Яндекс не вызываем")
            return None
        _state = state
    return _state


def _roll_day(st: dict, now: datetime | None) -> None:
    today = today_msk(now).isoformat()
    if st["day"] != today:
        st["day"], st["day_spent"] = today, 0.0


def _save(st: dict) -> None:
    """Запись в kv (под _save_lock, без _lock)."""
    try:
        from database import kv_set
        kv_set(KV_KEY, json.dumps({"total": round(st["total"], 4), "day": st["day"],
                                   "day_spent": round(st["day_spent"], 4)}))
    except Exception as e:
        logging.warning(f"[AI] yandex: учёт расходов не записан в БД: {type(e).__name__}")


def _allowance(st: dict, now: datetime | None) -> float:
    """Лимит на сегодня: остаток бюджета на начало дня / дней до отключения, но не больше
    MAX_DAY_FACTOR «ровных» долей (бюджет / все дни гранта): перенос не копится в один день."""
    left_days = days_left(now)
    if left_days <= 0:
        return 0.0
    before_today = st["total"] - st["day_spent"]
    even = budget() / max(1, (until() - GRANT_START).days)
    return min(max(0.0, budget() - before_today) / left_days, MAX_DAY_FACTOR * even)


def _cheapest_worst(max_tokens: int, models=None) -> float:
    names = [m for m in (models or PRICES) if m in PRICES] or list(PRICES)
    return min(worst_cost(m, max_tokens) for m in names)


def reserve(model: str, max_tokens: int, now: datetime | None = None) -> float | None:
    """Резервирует худшую цену запроса. Возвращает резерв (₽) или None - запрос нельзя:
    срок вышел, модель без цены, кончился бюджет на сегодня или на весь грант."""
    global _pending
    if model not in PRICES:
        return None
    worst = worst_cost(model, max_tokens)
    with _lock:
        if expired(now):
            return None
        st = _load()
        if st is None:
            return None
        _roll_day(st, now)
        if st["total"] + _pending + worst > budget():
            return None
        if st["day_spent"] + _pending + worst > _allowance(st, now):
            return None
        _pending += worst
        return worst


def release(reserved: float) -> None:
    """Запрос не тарифицируется (не ушёл, 4xx/5xx): резерв снимаем."""
    global _pending
    with _lock:
        _pending = max(0.0, _pending - reserved)


def settle(model: str, reserved: float, usage, now: datetime | None = None) -> float:
    """Ответ получен: резерв заменяем ценой по usage (нет usage - списываем резерв целиком)."""
    global _pending
    spent = usage_cost(model, usage)
    if spent is None:
        logging.warning(f"[AI] yandex:{model}: в ответе нет usage, списываю по худшей цене {reserved:.2f} ₽")
        spent = reserved
    with _save_lock:
        with _lock:
            _pending = max(0.0, _pending - reserved)
            st = _state   # резерв был, значит учёт уже прочитан (reserve без него не даёт)
            if st is None:
                return spent
            _roll_day(st, now)
            st["total"] += spent
            st["day_spent"] += spent
            snapshot = dict(st)
        _save(snapshot)
    return spent


def can_spend_today(model: str, max_tokens: int, now: datetime | None = None) -> bool:
    """Хватит ли сегодняшнего лимита ещё хотя бы на один худший запрос (без резерва)."""
    if model not in PRICES or expired(now):
        return False
    worst = worst_cost(model, max_tokens)
    with _lock:
        st = _load()
        if st is None:
            return False
        _roll_day(st, now)
        return (st["total"] + _pending + worst <= budget()
                and st["day_spent"] + _pending + worst <= _allowance(st, now))


def summary(now: datetime | None = None, max_tokens: int = 2500, models=None) -> dict:
    """Для статуса и оповещений: срок, бюджет, траты. unknown - учёт не прочитан из БД.
    exhausted - весь бюджет: худший запрос самой дешёвой из models (по умолчанию - всех
    моделей с ценой) в него уже не влезет."""
    with _lock:
        st = _load()
        base = {"until": until().isoformat(), "days_left": max(0, days_left(now)),
                "expired": expired(now), "budget": budget()}
        if st is None:
            return {**base, "unknown": True, "total": 0.0, "today": 0.0, "allowance": 0.0,
                    "exhausted": False}
        _roll_day(st, now)
        return {
            **base,
            "unknown": False,
            "total": round(st["total"], 2),
            "today": round(st["day_spent"], 2),
            "allowance": round(_allowance(st, now), 2),
            "exhausted": st["total"] + _pending + _cheapest_worst(max_tokens, models) > budget(),
        }
