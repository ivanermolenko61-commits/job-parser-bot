"""Цикл проверки фриланс-заказов: скан alot.pro, префильтр, дедуп, AI-оценка.

Все функции синхронные (requests) - вызывать через asyncio.to_thread.
Отправку в Telegram делает bot.py: заказ попадает в БД как отправленный
только после успешной отправки.

Схема надёжности (ни один подходящий заказ не теряется молча):
  • курсор alot_last_id идёт только вперёд и не откатывается;
  • неоценённые и неотправленные заказы лежат в БД со статусом pending и счётчиком
    attempts, каждый цикл дозапрашиваются по id (getProjects);
  • после MAX_AI_ATTEMPTS неудачных оценок заказ уходит с пометкой «без AI»;
  • pending старше FREELANCE_MAX_AGE_HOURS получает статус expired;
  • успешно отправленные заказы дополнительно держатся в памяти (_sent_ids), чтобы
    сбой записи в БД не привёл к дублю.
"""
import html
import json
import logging
import math
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
    freelance_pending,
    freelance_save,
    freelance_seen_ids,
    freelance_set_status,
    kv_get,
    kv_set,
)
from freelance import ai_scorer
from freelance.alot_client import (
    FreelanceOrder,
    _fetch_items,
    _parse_item,
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
# После стольких неудачных оценок заказ отправляется «без AI», а не теряется
MAX_AI_ATTEMPTS = 3
# Сколько pending-заказов дозапрашиваем за цикл (один запрос getProjects)
PENDING_FETCH_LIMIT = 100
# Верхняя оценка длительности prepare_cycle (для wait_for в боте)
PREPARE_TIMEOUT_SEC = 1800
# Если столько циклов подряд нет id выше last_id - перепроверяем границу
STALE_CYCLES_LIMIT = 6
# При первом запуске/застое отступаем от границы на столько id (~50 id/час * 24 ч с запасом)
BOOTSTRAP_LOOKBACK = 1500
LAST_ID_KEY = "alot_last_id"
AI_FAIL_STREAK_KEY = "freelance_ai_fail_streak"
# Повторы записи отправленного заказа в БД
SAVE_RETRIES = 3
SAVE_RETRY_PAUSE_SEC = 0.5
# Лимит Telegram - 4096; с запасом
MAX_MESSAGE_LEN = 4000
MSK = ZoneInfo("Europe/Moscow")

_cycle_lock = threading.Lock()  # второй поток prepare_cycle не допускаем

# order_id -> (ScoredOrder, message_id): отправлены в Telegram, но запись в БД
# могла не удаться. prepare_cycle исключает эти id, finish_cycle дописывает их.
_sent_ids: dict[int, tuple] = {}
_sent_lock = threading.Lock()
# Статус, выбранный пользователем (taken/dismissed) для заказа, которого ещё нет в БД
# (сбой записи после отправки): finish_cycle запишет его вместо «sent»
_sent_status: dict[int, str] = {}


def mark_sent_status(order_id: int, status: str) -> bool:
    """Запоминает статус для отправленного, но ещё не записанного заказа.
    True - заказ найден в памяти (_sent_ids)."""
    with _sent_lock:
        if order_id not in _sent_ids:
            return False
        _sent_status[order_id] = status
        return True

_SKIPPED = object()  # заказ не оценивался (лимит цикла / дедлайн): попытку не считаем


@dataclass
class ScoredOrder:
    order: FreelanceOrder
    ai: dict | None          # None = «без AI»
    dedup: str = ""
    attempts: int = 0        # неудачных AI-оценок (и сбоев отправки) подряд

    @property
    def fit(self) -> int | None:
        if not self.ai:
            return None
        try:
            return int(self.ai["fit"])
        except (KeyError, TypeError, ValueError):
            return None


@dataclass
class CycleResult:
    to_send: list[ScoredOrder] = field(default_factory=list)   # прошли оценку (или «без AI»)
    rejected: list[ScoredOrder] = field(default_factory=list)  # низкий fit: запомнить
    unscored: list[ScoredOrder] = field(default_factory=list)  # не оценены: pending
    batch_dups: list = field(default_factory=list)  # (ScoredOrder, id оригинала)
    new_last_id: int = 0
    raw_count: int = 0
    error: str = ""
    busy: bool = False
    # статистика цикла (для лога и проверок)
    candidates: int = 0
    ai_attempted: int = 0
    ai_ok: int = 0
    models_used: dict = field(default_factory=dict)  # модель -> сколько оценок
    pending_loaded: int = 0


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


def _parse_dt(value: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _load_pending(threshold: datetime, sent_now: set[int]) -> dict[int, ScoredOrder]:
    """Pending-заказы из БД: просроченные помечает expired, остальные дозапрашивает по id."""
    rows = freelance_pending(PENDING_FETCH_LIMIT)
    live: dict[int, dict] = {}
    for row in rows:
        oid = row["order_id"]
        if oid in sent_now:
            continue
        published = _parse_dt(row["published_at"])
        if published is not None and published < threshold:
            freelance_set_status(oid, "expired")
            continue
        live[oid] = row
    if not live:
        return {}
    try:
        items = _fetch_items(sorted(live))
    except Exception:
        # Сеть/alot недоступны: pending остаются в БД и будут запрошены в следующий раз
        logging.exception("[FREELANCE] не удалось дозапросить pending-заказы")
        return {}
    result: dict[int, ScoredOrder] = {}
    for item in items:
        order = _parse_item(item)
        if order is None or order.order_id not in live:
            continue
        row = live[order.order_id]
        if order.published_at < threshold:
            freelance_set_status(order.order_id, "expired")
            continue
        if not is_candidate(order):
            freelance_set_status(order.order_id, "rejected")
            continue
        ai = None
        if row.get("ai_json"):
            try:
                loaded = json.loads(row["ai_json"])
                if isinstance(loaded, dict) and "fit" in loaded:
                    ai = loaded
            except ValueError:
                ai = None
        result[order.order_id] = ScoredOrder(
            order, ai, row.get("dedup_key") or norm_dedup_key(order),
            attempts=int(row.get("attempts") or 0),
        )
    return result


def _prepare_cycle() -> CycleResult:
    started = time.monotonic()
    res = CycleResult()
    try:
        stored = kv_get(LAST_ID_KEY)
        top = None
        if stored and stored.isdigit():
            last_id = int(stored)
            # Потолок скана за цикл: если курсор отстал (откат старой версией, простой),
            # догоняем границу, а не по 300 id; дойдя до границы, скан останавливается сам
            max_ahead = BOOTSTRAP_LOOKBACK + 300
            # Застой: долго нет id выше last_id - возможно, граница ушла далеко вперёд
            if int(kv_get("freelance_stale", "0") or 0) >= STALE_CYCLES_LIMIT:
                top = find_upper_bound(ALOT_SEED_ID)
                logging.warning(f"[FREELANCE] застой, граница пересчитана: {top} (last_id={last_id})")
                if top < last_id:
                    # курсор оказался за границей (повреждён): единственный допустимый откат
                    last_id = top - 100
                    kv_set(LAST_ID_KEY, last_id)
                else:
                    last_id = max(last_id, top - BOOTSTRAP_LOOKBACK)
                max_ahead = max(BOOTSTRAP_LOOKBACK + 300, top - last_id + 300)
                kv_set("freelance_stale", 0)
        else:
            top = find_upper_bound(ALOT_SEED_ID)
            last_id = top - BOOTSTRAP_LOOKBACK
            max_ahead = BOOTSTRAP_LOOKBACK + 300
            logging.info(f"[FREELANCE] первый запуск: граница id={top}")
        scan = fetch_new_orders(last_id, max_ahead=max_ahead)
    except Exception as e:
        logging.exception("[FREELANCE] ошибка получения заказов")
        monitor.record("alot", 0, e, label="Источник фриланс-заказов alot.pro")
        res.error = f"{type(e).__name__}: {e}"
        return res

    monitor.record("alot", scan.raw_count, label="Источник фриланс-заказов alot.pro")
    res.raw_count = scan.raw_count
    res.new_last_id = max(last_id, scan.max_id)  # курсор только вперёд
    kv_set("freelance_last_raw", scan.raw_count)
    stale = int(kv_get("freelance_stale", "0") or 0)
    kv_set("freelance_stale", 0 if scan.max_id > last_id else stale + 1)

    with _sent_lock:
        sent_now = set(_sent_ids)

    # Свежесть, префильтр, уже виденные
    threshold = datetime.now(timezone.utc) - timedelta(hours=FREELANCE_MAX_AGE_HOURS)
    fresh = [o for o in scan.orders if o.published_at >= threshold and is_candidate(o)]
    seen = freelance_seen_ids([o.order_id for o in fresh])
    fresh = [o for o in fresh if o.order_id not in seen and o.order_id not in sent_now]

    # Pending: дозапрашиваем по id и добавляем к кандидатам
    pending = _load_pending(threshold, sent_now)
    res.pending_loaded = len(pending)

    pool_orders: list[tuple[FreelanceOrder, ScoredOrder | None]] = [(o, None) for o in fresh]
    pool_orders += [(sc.order, sc) for sc in pending.values()]
    pool_orders.sort(key=lambda t: t[0].order_id, reverse=True)  # новые первыми

    # Дубли: в БД за DEDUP_DAYS и внутри самой выборки
    keys = {o.order_id: (sc.dedup if sc else norm_dedup_key(o)) for o, sc in pool_orders}
    known = freelance_dup_keys(list(keys.values()), DEDUP_DAYS)
    # Для pending: оригинал мог уйти в чат уже после того, как заказ стал pending.
    # Считаем только реально отправленные, без самих pending (иначе два pending
    # с одним ключом отбросили бы друг друга)
    pending_ids = list(pending)
    known_sent = freelance_dup_keys(
        [keys[i] for i in pending_ids], DEDUP_DAYS, sent_only=True, exclude_ids=pending_ids
    ) if pending_ids else set()
    candidates: list[ScoredOrder] = []
    first_id: dict[str, int] = {}
    for o, existing in pool_orders:
        k = keys[o.order_id]
        if (existing is None and k in known) or (existing is not None and k in known_sent):
            # дубль уже отправленного/оценённого заказа: запоминаем сразу
            # (для pending статус pending меняется на rejected)
            _save_retry(existing or ScoredOrder(o, None, k), "rejected")
        elif k and k in first_id:
            # дубль внутри пачки: сохраним в finish_cycle, если оригинал обработан
            res.batch_dups.append((existing or ScoredOrder(o, None, k), first_id[k]))
        else:
            if k:
                first_id[k] = o.order_id
            candidates.append(existing or ScoredOrder(o, None, k))
    res.candidates = len(candidates)

    # AI-оценка в несколько потоков, новые первыми. Заказ без оценки (лимит, дедлайн,
    # сбой) остаётся pending и оценится в следующем цикле; после MAX_AI_ATTEMPTS
    # неудач уходит «без AI».
    ai_on = ai_scorer.is_enabled()
    to_score = [sc for sc in candidates
                if sc.ai is None and ai_on and sc.attempts < MAX_AI_ATTEMPTS]

    def _score(item):
        idx, sc = item
        if idx >= MAX_AI_PER_CYCLE or time.monotonic() - started >= AI_DEADLINE_SEC:
            return _SKIPPED
        try:
            return ai_scorer.score_order(sc.order)
        except Exception:
            logging.exception("[FREELANCE] сбой оценки заказа")
            return None

    with ThreadPoolExecutor(AI_WORKERS) as pool:
        results = list(pool.map(_score, enumerate(to_score)))
    scored = {id(sc): r for sc, r in zip(to_score, results)}

    for sc in candidates:
        if sc.ai is None:
            if id(sc) not in scored:
                # AI выключен или попытки исчерпаны: отправляем «без AI»
                res.to_send.append(sc)
                continue
            r = scored[id(sc)]
            if r is _SKIPPED:
                res.unscored.append(sc)
                continue
            res.ai_attempted += 1
            if r is None:
                sc.attempts += 1
                if sc.attempts >= MAX_AI_ATTEMPTS:
                    logging.warning(
                        f"[FREELANCE] {MAX_AI_ATTEMPTS} неудачных оценок, уходит «без AI»: {sc.order.url}")
                    res.to_send.append(sc)
                else:
                    res.unscored.append(sc)
                continue
            res.ai_ok += 1
            sc.ai = r
            res.models_used[r.get("model", "?")] = res.models_used.get(r.get("model", "?"), 0) + 1
        # оценка есть (новая или сохранённая раньше)
        fit = sc.fit
        if fit is not None and fit < FREELANCE_MIN_FIT:
            res.rejected.append(sc)
        else:
            res.to_send.append(sc)

    _report_ai_health(res)

    logging.info(
        f"[FREELANCE] скан: {scan.raw_count}, pending: {res.pending_loaded}, "
        f"кандидатов: {len(candidates)}, к отправке: {len(res.to_send)}, "
        f"отсеяно AI: {len(res.rejected)}, ждут оценки: {len(res.unscored)}, "
        f"AI: {res.ai_ok}/{res.ai_attempted} {res.models_used}"
    )
    return res


def _report_ai_health(res: CycleResult) -> None:
    """Оповещение, если AI не работает: 3 цикла подряд без единой оценки или ошибка ключа."""
    label = "AI-оценка заказов"
    st = ai_scorer.status()
    if st["config_error"]:
        monitor.record("gemini", 0, RuntimeError(st["config_error"]), label=label, immediate=True)
        return
    if res.ai_attempted == 0:
        return
    if res.ai_ok == 0:
        streak = int(kv_get(AI_FAIL_STREAK_KEY, "0") or 0) + 1
        kv_set(AI_FAIL_STREAK_KEY, streak)
        reason = st["last_error"] or "нет ответа от моделей"
        monitor.record("gemini", 0, RuntimeError(reason), label=label)
    else:
        kv_set(AI_FAIL_STREAK_KEY, 0)
        monitor.record("gemini", res.ai_ok, label=label)


def save_order(sc: ScoredOrder, status: str, message_id: int | None = None) -> None:
    o = sc.order
    freelance_save(
        o.order_id, o.site, o.title, o.url, o.price_text,
        o.published_at.isoformat(), sc.dedup, sc.fit,
        json.dumps(sc.ai, ensure_ascii=False) if sc.ai else "",
        status, sc.attempts, message_id,
    )


def save_sent(sc: ScoredOrder, message_id: int | None) -> bool:
    """Фиксирует отправленный заказ: сначала в памяти (защита от дубля), затем в БД
    с повторами. False - в БД записать не удалось (finish_cycle попробует ещё раз)."""
    oid = sc.order.order_id
    with _sent_lock:
        _sent_ids[oid] = (sc, message_id)
    for i in range(SAVE_RETRIES):
        try:
            with _sent_lock:
                status = _sent_status.get(oid, "sent")
            save_order(sc, status, message_id)
            with _sent_lock:
                if _sent_status.get(oid, "sent") != status:
                    continue  # статус сменили, пока писали: запишем ещё раз
                _sent_ids.pop(oid, None)
                _sent_status.pop(oid, None)
            return True
        except Exception:
            logging.exception(f"[FREELANCE] запись отправленного {sc.order.url} "
                              f"не удалась ({i + 1}/{SAVE_RETRIES})")
            if i < SAVE_RETRIES - 1:
                time.sleep(SAVE_RETRY_PAUSE_SEC)
    return False


def _save_retry(sc: ScoredOrder, status: str) -> bool:
    for i in range(SAVE_RETRIES):
        try:
            save_order(sc, status)
            return True
        except Exception:
            logging.exception(f"[FREELANCE] запись {status} {sc.order.url} не удалась ({i + 1}/{SAVE_RETRIES})")
            if i < SAVE_RETRIES - 1:
                time.sleep(SAVE_RETRY_PAUSE_SEC)
    return False


def finish_cycle(res: CycleResult, sent: list[ScoredOrder], skipped: list[ScoredOrder],
                 retry: list[ScoredOrder]) -> None:
    """Фиксирует итог цикла.

    sent    - уже сохранены ботом сразу после отправки (тут нужны для проверки дублей);
    skipped - показаны сводкой (лимит), сохраняются как обработанные;
    retry   - не отправлены (сеть, лимиты Telegram, сбой форматирования): получают
              статус pending и подбираются снова, пока не истечёт срок свежести.
    Курсор last_id только растёт; если какую-то запись сохранить не удалось, курсор
    не двигается (заказ будет найден повторным сканом, а не потерян).
    """
    done_ids = {sc.order.order_id for sc in sent}
    failed = False
    for sc in res.rejected + skipped:
        if _save_retry(sc, "rejected"):
            done_ids.add(sc.order.order_id)
        else:
            failed = True

    for sc in res.unscored:
        if not _save_retry(sc, "pending"):
            failed = True
    for sc in retry:
        sc.attempts += 1
        if not _save_retry(sc, "pending"):
            failed = True

    # Дубли внутри пачки сохраняем, только если оригинал обработан
    for dup, orig_id in res.batch_dups:
        if orig_id in done_ids:
            _save_retry(dup, "rejected")

    # Отправленные, но не записанные ранее: дописываем (защита от дубля)
    with _sent_lock:
        leftovers = list(_sent_ids.items())
    for oid, (sc, message_id) in leftovers:
        try:
            with _sent_lock:
                status = _sent_status.get(oid, "sent")
            save_order(sc, status, message_id)
            with _sent_lock:
                if _sent_status.get(oid, "sent") == status:
                    _sent_ids.pop(oid, None)
                    _sent_status.pop(oid, None)
        except Exception:
            logging.exception(f"[FREELANCE] не удалось дописать отправленный {sc.order.url}")
            failed = True

    if not failed and res.new_last_id:
        stored = kv_get(LAST_ID_KEY)
        current = int(stored) if stored and stored.isdigit() else 0
        if res.new_last_id > current:
            kv_set(LAST_ID_KEY, res.new_last_id)
    elif failed:
        logging.error("[FREELANCE] курсор не сдвинут из-за ошибок записи в БД")
    kv_set("freelance_last_ok", datetime.now(timezone.utc).isoformat())


# ---------- Форматирование ----------

def _num(value) -> float:
    try:
        f = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    return f if math.isfinite(f) else 0.0


def _fmt_hours(value) -> str:
    h = round(_num(value), 1)
    if h <= 0 or h > 9999:
        return ""
    return f"{h:.1f}".rstrip("0").rstrip(".")


def format_order(sc: ScoredOrder, compact: bool = False) -> str:
    """HTML-сообщение о заказе. Итог не длиннее MAX_MESSAGE_LEN (иначе упрощается)."""
    o, ai = sc.order, sc.ai
    e = html.escape
    fit = sc.fit
    fire = "🔥 " if fit is not None and fit >= 9 else ""
    t = o.published_at.astimezone(MSK).strftime("%H:%M")
    lines = [
        f"{fire}💼 <b>{e((o.title or '')[:200])}</b>   [{e(site_name(o.site))}]",
        f"💰 {e((o.price_text or 'не указана')[:100])} · 🕒 {t}",
    ]
    if ai and fit is not None:
        parts = [f"{fit}/10"]
        difficulty = str(ai.get("difficulty") or "")
        if difficulty:
            parts.append(difficulty[:20])
        hours = _fmt_hours(ai.get("hours"))
        if hours:
            parts.append(f"~{hours} ч")
        price_rub = str(ai.get("price_rub") or "").strip()
        if price_rub:
            parts.append(f"предложить {price_rub[:100]}")
        lines.append("🤖 " + " · ".join(e(p) for p in parts))
        if not compact:
            summary = str(ai.get("summary") or "")
            risks = str(ai.get("risks") or "")
            if summary:
                lines.append(f"📝 {e(summary[:300])}")
            if risks:
                lines.append(f"⚠️ Риск: {e(risks[:300])}")
    else:
        lines.append("🤖 без AI")
        if not compact:
            body = " ".join((o.body or "").split())
            if body:
                lines.append(f"📝 {e(body[:300])}{'…' if len(body) > 300 else ''}")
    text = "\n".join(lines)
    if len(text) > MAX_MESSAGE_LEN:
        if not compact:
            return format_order(sc, compact=True)
        return format_order_plain(sc)  # HTML резать нельзя: сломаются теги
    return text


def format_order_plain(sc: ScoredOrder) -> str:
    """Запасной формат без HTML: название, цена, ссылка (и оценка, если есть)."""
    o = sc.order
    t = o.published_at.astimezone(MSK).strftime("%H:%M")
    lines = [
        f"💼 {(o.title or '')[:200]}   [{site_name(o.site)}]",
        f"💰 {(o.price_text or 'не указана')[:100]} · 🕒 {t}",
    ]
    fit = sc.fit
    lines.append(f"🤖 {fit}/10" if fit is not None else "🤖 без AI")
    lines.append(f"🔗 {o.url}")
    return "\n".join(lines)[:MAX_MESSAGE_LEN]
