"""Цикл проверки фриланс-заказов: скан alot.pro, префильтр, дедуп, AI-оценка.

Все функции синхронные (requests, SDK) — вызывать через asyncio.to_thread.
Отправку в Telegram делает bot.py: заказ попадает в БД как отправленный
только после успешной отправки.
"""
import html
import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from config import (
    ALOT_SEED_ID,
    FREELANCE_MAX_AGE_HOURS,
    FREELANCE_MIN_FIT,
)
from database import (
    freelance_dup_keys,
    freelance_save,
    freelance_seen_ids,
    kv_get,
    kv_set,
)
from freelance import ai_scorer
from freelance.alot_client import (
    FreelanceOrder,
    fetch_new_orders,
    find_upper_bound,
    site_name,
)
from freelance.filters import is_candidate, norm_dedup_key
from health import monitor

DEDUP_DAYS = 3
MAX_AI_PER_CYCLE = 120
AI_WORKERS = 4
# Общий дедлайн на AI-оценку (сек от старта prepare_cycle); неоценённые откладываются
AI_DEADLINE_SEC = 600
# Максимум попыток отправки одного заказа, потом сдаёмся и считаем обработанным
MAX_SEND_ATTEMPTS = 3
# Верхняя оценка длительности prepare_cycle (для wait_for в боте)
PREPARE_TIMEOUT_SEC = 1800
# Если столько циклов подряд нет id выше last_id — перепроверяем границу
STALE_CYCLES_LIMIT = 6
# При первом запуске отступаем от границы на столько id (~50 id/час * 24 ч с запасом)
BOOTSTRAP_LOOKBACK = 1500
LAST_ID_KEY = "alot_last_id"
MSK = ZoneInfo("Europe/Moscow")

_cycle_lock = threading.Lock()  # второй поток prepare_cycle не допускаем


@dataclass
class ScoredOrder:
    order: FreelanceOrder
    ai: dict | None          # None = «без AI»
    dedup: str = ""

    @property
    def fit(self) -> int | None:
        return self.ai["fit"] if self.ai else None


@dataclass
class CycleResult:
    to_send: list[ScoredOrder] = field(default_factory=list)   # прошли оценку
    rejected: list[ScoredOrder] = field(default_factory=list)  # низкий fit: запомнить
    batch_dups: list = field(default_factory=list)  # (ScoredOrder, id оригинала)
    new_last_id: int = 0
    raw_count: int = 0
    error: str = ""
    busy: bool = False


def prepare_cycle() -> CycleResult:
    """Собирает новые заказы и оценивает их. Ничего не отправляет.

    Если предыдущий поток (переживший wait_for в боте) ещё работает,
    возвращает busy: параллельных циклов не бывает.
    """
    if not _cycle_lock.acquire(blocking=False):
        return CycleResult(busy=True, error="предыдущая проверка ещё выполняется")
    try:
        return _prepare_cycle()
    finally:
        _cycle_lock.release()


def _prepare_cycle() -> CycleResult:
    started = time.monotonic()
    res = CycleResult()
    try:
        stored = kv_get(LAST_ID_KEY)
        if stored and stored.isdigit():
            last_id = int(stored)
            max_ahead = 300
            # Застой: долго нет id выше last_id — возможно, last_id ушёл за границу
            if int(kv_get("freelance_stale", "0") or 0) >= STALE_CYCLES_LIMIT:
                top = find_upper_bound(ALOT_SEED_ID)
                logging.warning(f"[FREELANCE] застой, граница пересчитана: {top} (last_id={last_id})")
                if top < last_id or top > last_id + max_ahead:
                    last_id = top - 100
                kv_set("freelance_stale", 0)
        else:
            top = find_upper_bound(ALOT_SEED_ID)
            last_id = top - BOOTSTRAP_LOOKBACK
            max_ahead = BOOTSTRAP_LOOKBACK + 300
            logging.info(f"[FREELANCE] первый запуск: граница id={top}")
        scan = fetch_new_orders(last_id, max_ahead=max_ahead)
    except Exception as e:
        logging.exception("[FREELANCE] ошибка получения заказов")
        monitor.record("alot", 0, e)
        res.error = f"{type(e).__name__}: {e}"
        return res

    monitor.record("alot", scan.raw_count)
    res.raw_count = scan.raw_count
    res.new_last_id = max(last_id, scan.max_id)
    kv_set("freelance_last_raw", scan.raw_count)
    stale = int(kv_get("freelance_stale", "0") or 0)
    kv_set("freelance_stale", 0 if scan.max_id > last_id else stale + 1)

    # Свежесть, префильтр, уже виденные
    threshold = datetime.now(timezone.utc) - timedelta(hours=FREELANCE_MAX_AGE_HOURS)
    fresh = [o for o in scan.orders if o.published_at >= threshold and is_candidate(o)]
    seen = freelance_seen_ids([o.order_id for o in fresh])
    fresh = [o for o in fresh if o.order_id not in seen]
    fresh.sort(key=lambda o: o.order_id, reverse=True)  # новые первыми

    # Дубли: в БД за DEDUP_DAYS и внутри самой выборки
    keys = {o.order_id: norm_dedup_key(o) for o in fresh}
    known = freelance_dup_keys(list(keys.values()), DEDUP_DAYS)
    candidates: list[ScoredOrder] = []
    first_id: dict[str, int] = {}
    for o in fresh:
        k = keys[o.order_id]
        if k in known:
            # дубль уже отправленного/оценённого заказа: запоминаем сразу
            save_order(ScoredOrder(o, None, k), False)
        elif k in first_id:
            # дубль внутри пачки: сохраним в finish_cycle, если оригинал обработан
            res.batch_dups.append((ScoredOrder(o, None, k), first_id[k]))
        else:
            first_id[k] = o.order_id
            candidates.append(ScoredOrder(o, None, k))

    # AI-оценка в несколько потоков, новые первыми. Если AI включён, заказ без
    # оценки (лимит, дедлайн, сбой) НЕ отправляем, чтобы не засорять чат:
    # last_id отодвигается назад, и заказ оценится в следующем цикле.
    ai_on = ai_scorer.is_enabled()

    def _score(item):
        idx, sc = item
        if idx >= MAX_AI_PER_CYCLE or time.monotonic() - started >= AI_DEADLINE_SEC:
            return None
        return ai_scorer.score_order(sc.order)

    with ThreadPoolExecutor(AI_WORKERS) as pool:
        scores = list(pool.map(_score, enumerate(candidates)))

    pending: list[ScoredOrder] = []
    for sc, ai in zip(candidates, scores):
        sc.ai = ai
        if ai is None and ai_on:
            pending.append(sc)
        elif ai is not None and ai["fit"] < FREELANCE_MIN_FIT:
            res.rejected.append(sc)
        else:
            res.to_send.append(sc)
    if pending:
        res.new_last_id = min(res.new_last_id, min(p.order.order_id for p in pending) - 1)

    logging.info(
        f"[FREELANCE] скан: {scan.raw_count}, кандидатов: {len(candidates)}, "
        f"к отправке: {len(res.to_send)}, отсеяно AI: {len(res.rejected)}, "
        f"отложено без оценки: {len(pending)}"
    )
    return res


def save_order(sc: ScoredOrder, sent: bool) -> None:
    o = sc.order
    freelance_save(
        o.order_id, o.site, o.title, o.url, o.price_text,
        o.published_at.isoformat(), sc.dedup, sc.fit,
        json.dumps(sc.ai, ensure_ascii=False) if sc.ai else "", sent,
    )


def register_failure(order_id: int) -> int:
    """Счётчик неудачных попыток отправки заказа (kv). Возвращает новое значение."""
    key = f"freelance_attempts:{order_id}"
    n = int(kv_get(key, "0") or 0) + 1
    kv_set(key, n)
    return n


def finish_cycle(res: CycleResult, sent: list[ScoredOrder], skipped: list[ScoredOrder],
                 retry: list[ScoredOrder], bad: list[ScoredOrder] | None = None) -> None:
    """Фиксирует итог цикла.

    sent    — уже сохранены ботом сразу после отправки (тут нужны для проверки дублей);
    skipped — показаны сводкой (лимит), сохраняются как обработанные;
    retry   — сетевой сбой/лимиты Telegram или несостоявшаяся сводка: заказ подберётся
              снова, last_id не уходит дальше него; после MAX_SEND_ATTEMPTS попыток
              заказ считается обработанным;
    bad     — «битые» заказы (BadRequest, ошибка форматирования): обработаны, без повтора.
    """
    done_ids = {sc.order.order_id for sc in sent}
    for sc in res.rejected + skipped + list(bad or []):
        save_order(sc, False)
        done_ids.add(sc.order.order_id)

    still_retry: list[ScoredOrder] = []
    for sc in retry:
        if register_failure(sc.order.order_id) >= MAX_SEND_ATTEMPTS:
            logging.error(f"[FREELANCE] сдаёмся после {MAX_SEND_ATTEMPTS} попыток: {sc.order.url}")
            save_order(sc, False)
            done_ids.add(sc.order.order_id)
        else:
            still_retry.append(sc)

    # Дубли внутри пачки сохраняем, только если оригинал обработан
    for dup, orig_id in res.batch_dups:
        if orig_id in done_ids:
            save_order(dup, False)

    new_last = res.new_last_id
    if still_retry:
        new_last = min(new_last, min(sc.order.order_id for sc in still_retry) - 1)
    if new_last:
        kv_set(LAST_ID_KEY, new_last)
    kv_set("freelance_last_ok", datetime.now(timezone.utc).isoformat())


# ---------- Форматирование ----------

def format_order(sc: ScoredOrder) -> str:
    o, ai = sc.order, sc.ai
    e = html.escape
    fire = "🔥 " if ai and ai["fit"] >= 9 else ""
    t = o.published_at.astimezone(MSK).strftime("%H:%M")
    lines = [
        f"{fire}💼 <b>{e(o.title[:200])}</b>   [{e(site_name(o.site))}]",
        f"💰 {e(o.price_text or 'не указана')} · 🕒 {t}",
    ]
    if ai:
        parts = [f"{ai['fit']}/10"]
        if ai["difficulty"]:
            parts.append(ai["difficulty"])
        if ai["hours"]:
            h = ai["hours"]
            parts.append(f"~{int(h) if h == int(h) else h} ч")
        if ai["price_rub"]:
            parts.append(f"предложить {ai['price_rub']}")
        lines.append("🤖 " + " · ".join(e(p) for p in parts))
        if ai["summary"]:
            lines.append(f"📝 {e(ai['summary'][:300])}")
        if ai["risks"]:
            lines.append(f"⚠️ Риск: {e(ai['risks'][:300])}")
    else:
        lines.append("🤖 без AI")
        body = " ".join((o.body or "").split())
        if body:
            lines.append(f"📝 {e(body[:300])}{'…' if len(body) > 300 else ''}")
    return "\n".join(lines)
