"""Цикл проверки фриланс-заказов: скан alot.pro, префильтр, дедуп, AI-оценка.

Все функции синхронные (requests) - вызывать через asyncio.to_thread.
Отправку в Telegram делает bot.py: заказ попадает в БД как отправленный
только после успешной отправки.

Схема надёжности (ни один подходящий заказ не теряется молча):
  • курсор alot_last_id идёт только вперёд и не откатывается;
  • неоценённые и неотправленные заказы лежат в БД со статусом pending и счётчиком
    attempts, каждый цикл дозапрашиваются по id (getProjects);
  • без оценки AI заказ в чат не уходит: ждёт в pending, пока AI не ответит
    (о простое AI приходит health-оповещение); «без AI» - только если ключей нет;
  • pending старше FREELANCE_MAX_AGE_HOURS получает статус expired;
  • успешно отправленные заказы дополнительно держатся в памяти (_sent_ids), чтобы
    сбой записи в БД не привёл к дублю.
"""
import html
import json
import logging
import math
import os
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
from freelance import ai_scorer, pricing, sources, yandex_budget
from freelance.alot_client import (
    FreelanceOrder,
    ScanResult,
    _fetch_items,
    _parse_item,
    fetch_new_orders,
    find_upper_bound,
    site_name,
)
from freelance.fl_client import fetch_fl_orders
from freelance.kwork_client import fetch_kwork_orders
from freelance.filters import is_candidate, norm_dedup_key, priority, reject_reason
from health import monitor

DEDUP_DAYS = 3
MAX_AI_PER_CYCLE = 120
AI_WORKERS = 4
# Общий дедлайн на AI-оценку (сек от старта prepare_cycle); неоценённые откладываются
AI_DEADLINE_SEC = 600
# Сколько pending-заказов дозапрашиваем за цикл (пачки по 100 id - лимит getProjects).
# Поток ~150 кандидатов в сутки, за 6 ч свежести pending обычно < 50
PENDING_FETCH_LIMIT = 300
# Сколько pending читаем из БД за цикл, чтобы пометить просроченные (expired)
PENDING_SCAN_LIMIT = 5000
PENDING_BATCH = 100
# Верхняя оценка длительности prepare_cycle (для wait_for в боте)
PREPARE_TIMEOUT_SEC = 1800
# Если столько циклов подряд нет id выше last_id - перепроверяем границу (≈ 1 ч)
STALE_CYCLES_LIMIT = 12
PARTIAL_ALERT_CYCLES = 3   # столько циклов подряд скан обрывается, не продвигая курсор, - тревога
# При первом запуске/застое отступаем от границы на столько id (~50 id/час * 24 ч с запасом)
BOOTSTRAP_LOOKBACK = 1500
# Первый запуск (нет курсора, например БД стёрлась при деплое): берём только последние
# ~50 id (≈1 ч), чтобы не оценивать и не присылать заново суточный поток
FIRST_RUN_LOOKBACK = 50
# Резерв дневной квоты AI для заказов поважнее (группы filters.priority): группа 3
# оценивается, пока осталось больше половины квоты, группа 2 - больше 20%, группа 1 - всегда.
# Отложенный заказ ждёт в pending; квота сбрасывается в 03:00 МСК, а свежесть - 6 ч,
# поэтому после сброса оценятся только недавние отложенные, остальные истекут.
RESERVE_SHARE = {2: 0.2, 3: 0.5}
# Быстрый триаж (ai_scorer.triage_order): off - выключен; shadow - считается и сравнивается с полной
# оценкой в логе, заказы не отсеивает; on - заказы с быстрой оценкой <= TRIAGE_REJECT_MAX не оцениваются
# полной моделью. Любое другое значение - shadow.
TRIAGE_MODE = (os.environ.get("FREELANCE_TRIAGE") or "shadow").strip().lower()
if TRIAGE_MODE not in ("off", "shadow", "on"):
    TRIAGE_MODE = "shadow"
try:
    TRIAGE_REJECT_MAX = max(0, min(4, int(os.environ.get("TRIAGE_REJECT_MAX") or 2)))
except ValueError:
    TRIAGE_REJECT_MAX = 2
TRIAGE_CACHE_MAX = 2000
_triage_cache: dict[int, int] = {}
_triage_lock = threading.Lock()
LAST_ID_KEY = "alot_last_id"
AI_FAIL_STREAK_KEY = "freelance_ai_fail_streak"
QUOTA_OUT_DAY_KEY = "ai_quota_out_day"   # день МСК, за который уже сообщили «квота кончилась»
QUOTA_OUT_MIN_SEC = 3600                  # о паузе короче часа не сообщаем
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
_RESERVED = object()  # не оценивался: квоты мало, она оставлена заказам поважнее


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
    to_send: list[ScoredOrder] = field(default_factory=list)   # прошли оценку («без AI» - нет ключей)
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
    reserved: int = 0        # отложено ради резерва квоты
    triaged: int = 0         # отсеяно быстрым фильтром без полной оценки
    quota_out: datetime | None = None   # квоты нет ни у кого: когда вернётся первый провайдер


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


def scored_from_row(row: dict) -> ScoredOrder:
    """ScoredOrder из строки freelance_orders (для кнопки «Подробнее»). Текста заказа
    в БД нет (body пустой) - в AI-режиме он для карточки не нужен."""
    ai = None
    if row.get("ai_json"):
        try:
            loaded = json.loads(row["ai_json"])
            if isinstance(loaded, dict) and "fit" in loaded:
                ai = loaded
        except ValueError:
            ai = None
    order = FreelanceOrder(
        order_id=int(row["order_id"]), site=row.get("site") or "", title=row.get("title") or "",
        body="", price_text=row.get("price_text") or "", price_value=0.0, categories=[],
        published_at=_parse_dt(row.get("published_at")) or datetime.now(timezone.utc),
        url=row.get("url") or "",
    )
    return ScoredOrder(order, ai, row.get("dedup_key") or "")


# Полные заказы FL.ru/Kwork со статусом pending. В таблице заказов описания нет, а заказ, ждущий
# AI-оценку, может выпасть из ленты (всплеск заказов) или пережить перезапуск: оценивать его по
# одному названию нельзя (отказ окончателен). Хранится одним ключом kv (без изменения схемы БД),
# перезаписывается целиком и чистится по возрасту заказа - не растёт.
FEED_STORE_KEY = "freelance_feed_pending"
_feed_store_lock = threading.Lock()


def _feed_store_read() -> dict[str, dict]:
    try:
        data = json.loads(kv_get(FEED_STORE_KEY, "") or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _order_to_dict(o: FreelanceOrder) -> dict:
    # ровно то, что увидит AI (_user_prompt: mask_contacts, затем BODY_LIMIT; маскирование идемпотентно),
    # поэтому оценка после восстановления не теряет текста; контакты заказчика в БД не попадают
    body = ai_scorer.mask_contacts(o.body or "")[:ai_scorer.BODY_LIMIT]
    return {"site": o.site, "title": o.title, "body": body,
            "price_text": o.price_text, "price_value": o.price_value, "categories": list(o.categories),
            "published_at": o.published_at.isoformat(), "url": o.url}


def _order_from_dict(order_id: int, d: dict) -> FreelanceOrder | None:
    try:
        published = _parse_dt(d["published_at"])
        if published is None:
            return None
        return FreelanceOrder(
            order_id=order_id, site=str(d["site"]), title=str(d["title"]), body=str(d.get("body") or ""),
            price_text=str(d.get("price_text") or ""), price_value=float(d.get("price_value") or 0),
            categories=[str(c) for c in d.get("categories") or []], published_at=published,
            url=str(d["url"]))
    except (KeyError, TypeError, ValueError):
        return None


def _feed_store_update(order: FreelanceOrder, status: str) -> None:
    """pending - запомнить полный заказ FL.ru/Kwork, любой другой статус - забыть."""
    if not sources.is_feed_id(order.order_id):
        return
    threshold = datetime.now(timezone.utc) - timedelta(hours=FREELANCE_MAX_AGE_HOURS)
    try:
        with _feed_store_lock:
            data = _feed_store_read()
            if status == "pending":
                data[str(order.order_id)] = _order_to_dict(order)
            else:
                data.pop(str(order.order_id), None)
            for key in [k for k, d in data.items()
                        if not isinstance(d, dict) or (_parse_dt(d.get("published_at")) or threshold) < threshold]:
                del data[key]
            kv_set(FEED_STORE_KEY, json.dumps(data, ensure_ascii=False))
    except Exception:
        # заказ в таблице заказов уже записан; без описания он просто не будет оценён после выпадения из ленты
        logging.exception("[FREELANCE] не удалось сохранить описание заказа FL.ru/Kwork")


def _load_pending(threshold: datetime, sent_now: set[int],
                  feed_orders: list[FreelanceOrder] | None = None,
                  fetch_alot: bool = True) -> dict[int, ScoredOrder]:
    """Pending-заказы из БД: просроченные помечает expired, остальные дозапрашивает.

    Заказы alot.pro дозапрашиваются по id (fetch_alot=False - не трогать alot: выключен или
    лежит), заказы FL.ru и Kwork берутся из свежей ленты цикла (feed_orders), а выпавшие из
    ленты - из сохранённых полных заказов (_feed_store_*)."""
    # Все pending (новые первыми): просроченные помечаем expired, иначе при долгом
    # простое AI старые строки за пределами лимита висели бы pending
    rows = freelance_pending(PENDING_SCAN_LIMIT)
    live: dict[int, dict] = {}
    for row in rows:
        oid = row["order_id"]
        if oid in sent_now:
            continue
        published = _parse_dt(row["published_at"])
        if published is not None and published < threshold:
            freelance_set_status(oid, "expired")
            continue
        if len(live) < PENDING_FETCH_LIMIT:
            live[oid] = row
    if not live:
        return {}
    items: list[dict] = []
    ids = sorted((i for i in live if fetch_alot and not sources.is_feed_id(i)), reverse=True)  # новые первыми: при сбое сети теряем хвост
    for i in range(0, len(ids), PENDING_BATCH):
        try:
            items += _fetch_items(ids[i:i + PENDING_BATCH])
        except Exception:
            # Сеть/alot недоступны: pending остаются в БД и будут запрошены в следующий раз
            logging.exception("[FREELANCE] не удалось дозапросить pending-заказы")
            break
    fetched = [o for o in (_parse_item(item) for item in items) if o]
    # FL.ru/Kwork: заказ из свежей ленты; выпавший из ленты (или лента не получена, или был
    # перезапуск) - полный заказ с описанием из сохранённых. Без описания заказ не оцениваем, он
    # ждёт, пока не истечёт срок. Заказы выключенного источника не берём: они тратили бы квоту AI
    # вопреки переключателю
    feed_by_id = {o.order_id: o for o in (feed_orders or [])}
    stored = _feed_store_read() if any(sources.is_feed_id(i) and i not in feed_by_id for i in live) else {}
    for oid in live:
        if not sources.is_feed_id(oid) or oid in feed_by_id or str(oid) not in stored:
            continue
        o = _order_from_dict(oid, stored[str(oid)]) if isinstance(stored[str(oid)], dict) else None
        if o is not None:
            feed_by_id[oid] = o
    fetched += [o for oid, o in feed_by_id.items()
                if oid in live and sources.is_enabled(sources.source_of(oid))]
    result: dict[int, ScoredOrder] = {}
    for order in fetched:
        if order.order_id not in live:
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


_PREFILTER_LOG_MAX = 2000
_prefilter_logged: set[int] = set()


def _log_dropped(order: FreelanceOrder, why: str) -> None:
    """Отсев заказа Youdo - в лог (с причиной), чтобы потерю можно было разобрать по логу.
    Контакты в названии маскируем: заказ в лог попадает целиком."""
    if order.site == "youdoru":
        title = ai_scorer.mask_contacts(order.title or "")[:80]
        logging.info(f"[FREELANCE] отсеян: {why}, {order.site} {order.order_id}, {title!r}")


def _prefilter(orders) -> list[FreelanceOrder]:
    """Префильтр с учётом причин: причины в сводку цикла, а отсев Youdo и по стоп-словам -
    в лог один раз на заказ (скан каждый цикл перечитывает хвост окна)."""
    passed: list[FreelanceOrder] = []
    reasons: dict[str, int] = {}
    for o in orders:
        why = reject_reason(o)
        if why is None:
            passed.append(o)
            continue
        key = why.split(":", 1)[0]
        reasons[key] = reasons.get(key, 0) + 1
        if (o.site == "youdoru" or key == "stop_word") and o.order_id not in _prefilter_logged:
            if len(_prefilter_logged) >= _PREFILTER_LOG_MAX:
                _prefilter_logged.clear()
            _prefilter_logged.add(o.order_id)
            title = ai_scorer.mask_contacts(o.title or "")[:80]
            logging.info(f"[FREELANCE] префильтр: {why}, {o.site} {o.order_id}, {title!r}")
    if reasons:
        logging.info(f"[FREELANCE] префильтр отсеял: {reasons}")
    return passed


def _fetch_source(name: str, fetch, label: str) -> list[FreelanceOrder]:
    """Заказы одного прямого источника (FL.ru, Kwork). Выключенный источник не опрашивается и
    сломанным не считается. Сбой не роняет цикл: пишется в health, возвращается []."""
    if not sources.is_enabled(name):
        monitor.reset_streak(name)
        return []
    try:
        orders = fetch()
    except Exception as e:
        logging.exception(f"[FREELANCE] ошибка получения заказов: {label}")
        monitor.record(name, 0, e, label=label)
        return []
    monitor.record(name, len(orders), label=label)
    return orders


def _fetch_fl_orders() -> list[FreelanceOrder]:
    return _fetch_source(sources.FLRU, lambda: fetch_fl_orders(), "Источник фриланс-заказов FL.ru")


def _fetch_kwork_orders() -> list[FreelanceOrder]:
    return _fetch_source(sources.KWORK, lambda: fetch_kwork_orders(), "Источник фриланс-заказов Kwork")


def _fetch_feed_orders() -> list[FreelanceOrder]:
    """Заказы прямых источников (кроме alot.pro), которые включены."""
    return [*_fetch_fl_orders(), *_fetch_kwork_orders()]


ALOT_SILENT_SEC = 3 * 3600   # столько alot.pro без новых заказов - считаем, что он «спал»


def _track_alot_activity(has_new: bool) -> None:
    """Следит за тем, когда alot.pro в последний раз отдавал новые заказы. Если он молчал
    дольше ALOT_SILENT_SEC и снова ожил, а прямые источники включены, - одно сообщение:
    они дублируют alot и тратят квоту AI. Время засевается при первом успешном скане, чтобы
    «спящий» на момент деплоя alot тоже был замечен."""
    now = time.time()
    try:
        prev = float(kv_get("alot_last_new_at", "") or "")
    except ValueError:
        prev = None
    if prev is None:
        kv_set("alot_last_new_at", int(now))
        return
    if not has_new:
        return
    kv_set("alot_last_new_at", int(now))
    if now - prev < ALOT_SILENT_SEC:
        return
    direct = [sources.TITLES[n] for n in (sources.FLRU, sources.KWORK) if sources.is_enabled(n)]
    if direct:
        monitor.notify(
            f"🟢 <b>alot.pro снова отдаёт заказы</b>\n"
            f"{' и '.join(direct)} дублируют его заказы и тратят квоту AI. Если они больше не нужны: "
            f"💼 Фриланс → 🔌 Источники."
        )


def _scan_alot() -> tuple[ScanResult, int]:
    """Скан alot.pro от курсора. Возвращает (скан, курсор до скана)."""
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
        last_id = top - FIRST_RUN_LOOKBACK
        max_ahead = FIRST_RUN_LOOKBACK + 300
        logging.info(f"[FREELANCE] первый запуск: граница id={top}, "
                     f"берём последние {FIRST_RUN_LOOKBACK} id")
    return fetch_new_orders(last_id, max_ahead=max_ahead), last_id


def _prepare_cycle() -> CycleResult:
    started = time.monotonic()
    res = CycleResult()
    scan, last_id = ScanResult(), 0
    alot_ok, alot_err = False, None
    if sources.is_enabled(sources.ALOT):
        try:
            scan, last_id = _scan_alot()
            alot_ok = True
        except Exception as e:
            logging.exception("[FREELANCE] ошибка получения заказов")
            monitor.record("alot", 0, e, label="Источник фриланс-заказов alot.pro")
            alot_err = e
    else:
        monitor.reset_streak("alot")   # выключен намеренно: не поломка
    feed_orders = _fetch_feed_orders()   # FL.ru и Kwork (если включены)
    # Ошибка цикла - только когда брать заказы неоткуда. Пустая лента включённого FL.ru/Kwork -
    # нормальный исход: pending из БД (в том числе выпавшие из ленты) всё равно дообрабатываются
    if not alot_ok and not feed_orders and not any(
            sources.is_enabled(n) for n in (sources.FLRU, sources.KWORK)):
        res.error = (f"{type(alot_err).__name__}: {alot_err}" if alot_err
                     else "нет включённых источников заказов")
        return res
    if alot_ok:
        # Скан, который раз за разом обрывается сбоем и не продвигает курсор, - не норма:
        # новые заказы не загружаются. После PARTIAL_ALERT_CYCLES циклов - ошибка в health
        # (оповещение) и счёт застоя, чтобы со временем пересчитать границу.
        partial_streak = int(kv_get("freelance_partial", "0") or 0)
        partial_streak = partial_streak + 1 if (scan.partial and scan.max_id <= last_id) else 0
        kv_set("freelance_partial", partial_streak)
        stuck = partial_streak >= PARTIAL_ALERT_CYCLES
        if stuck:
            monitor.record("alot", 0, RuntimeError(
                f"скан обрывается сбоем alot.pro {partial_streak} циклов подряд, новые заказы не загружаются"),
                label="Источник фриланс-заказов alot.pro")
        else:
            monitor.record("alot", scan.raw_count, label="Источник фриланс-заказов alot.pro")
        res.new_last_id = max(last_id, scan.max_id)  # курсор только вперёд
        stale = int(kv_get("freelance_stale", "0") or 0)
        if scan.max_id > last_id:
            kv_set("freelance_stale", 0)
        elif not scan.partial or stuck:
            # единичный оборванный скан не признак застоя: до границы просто не дошли
            kv_set("freelance_stale", stale + 1)
        _track_alot_activity(scan.max_id > last_id)
    res.raw_count = scan.raw_count + len(feed_orders)
    kv_set("freelance_last_raw", res.raw_count)

    with _sent_lock:
        sent_now = set(_sent_ids)

    # Свежесть, префильтр, уже виденные (заказы всех источников - в одном потоке)
    threshold = datetime.now(timezone.utc) - timedelta(hours=FREELANCE_MAX_AGE_HOURS)
    fresh = _prefilter(o for o in [*scan.orders, *feed_orders] if o.published_at >= threshold)
    seen = freelance_seen_ids([o.order_id for o in fresh])
    fresh = [o for o in fresh if o.order_id not in seen and o.order_id not in sent_now]

    # Pending: alot дозапрашиваем по id (если включён), FL.ru и Kwork берём из ленты этого цикла
    pending = _load_pending(threshold, sent_now, feed_orders, fetch_alot=alot_ok)
    res.pending_loaded = len(pending)

    pool_orders: list[tuple[FreelanceOrder, ScoredOrder | None]] = [(o, None) for o in fresh]
    pool_orders += [(sc.order, sc) for sc in pending.values()]
    # новые первыми: по времени публикации (id FL.ru и alot.pro несравнимы), при равенстве по id
    pool_orders.sort(key=lambda t: (t[0].published_at, t[0].order_id), reverse=True)

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
            _log_dropped(o, "дубль отправленного/оценённого")
            _save_retry(existing or ScoredOrder(o, None, k), "rejected")
        elif k and k in first_id:
            # дубль внутри пачки: сохраним в finish_cycle, если оригинал обработан
            _log_dropped(o, f"дубль заказа {first_id[k]} в этой пачке")
            res.batch_dups.append((existing or ScoredOrder(o, None, k), first_id[k]))
        else:
            if k:
                first_id[k] = o.order_id
            candidates.append(existing or ScoredOrder(o, None, k))
    res.candidates = len(candidates)

    # AI-оценка в несколько потоков, новые первыми. Заказ без оценки (лимит, дедлайн,
    # сбой) остаётся pending и оценивается в следующих циклах, пока свежий: без
    # оценки в чат не уходит (иначе при исчерпанной квоте летит весь поток без фильтра).
    # «Без AI» отправляется только когда AI выключен совсем (нет ключей).
    # Очередь: сначала группа приоритета 1 (см. filters.priority), внутри группы новые первыми.
    ai_on = ai_scorer.is_enabled()
    to_score = [sc for sc in candidates if sc.ai is None and ai_on]
    to_score.sort(key=lambda sc: (priority(sc.order), -sc.order.published_at.timestamp(),
                                  -sc.order.order_id))
    # Квоты нет ни у кого: не дёргаем провайдеров (каждая попытка была бы «сбоем» и
    # через 3 цикла - ложным «AI не работает»), заказы ждут в pending
    res.quota_out = ai_scorer.quota_exhausted() if to_score else None

    def _score(item):
        idx, sc = item
        if idx >= MAX_AI_PER_CYCLE or time.monotonic() - started >= AI_DEADLINE_SEC:
            return _SKIPPED
        will_score = not res.quota_out and not _quota_reserved(priority(sc.order))
        # shadow: быстрая оценка нужна только для сравнения с полной, значит, когда полная будет
        triage = _triage(sc) if (TRIAGE_MODE == "on" or (TRIAGE_MODE == "shadow" and will_score)) else None
        if TRIAGE_MODE == "on" and triage is not None and triage <= TRIAGE_REJECT_MAX:
            return {"fit": triage, "triage": True, "model": f"triage:{ai_scorer.TRIAGE_MODEL}",
                    "summary": "отсеян быстрым фильтром"}
        if not will_score:
            return _RESERVED
        try:
            result = ai_scorer.score_order(sc.order)
        except Exception:
            logging.exception("[FREELANCE] сбой оценки заказа")
            return None
        if result is not None:
            _save_scored_now(sc, result)
        if TRIAGE_MODE == "shadow" and triage is not None and result is not None:
            _log_triage_shadow(sc, triage, result)
        return result

    with ThreadPoolExecutor(AI_WORKERS) as pool:
        results = list(pool.map(_score, enumerate(to_score)))
    scored = {id(sc): r for sc, r in zip(to_score, results)}

    for sc in candidates:
        fresh_scored = False
        if sc.ai is None:
            if id(sc) not in scored:
                # AI выключен (нет ключей): отправляем «без AI»
                res.to_send.append(sc)
                continue
            r = scored[id(sc)]
            if r is _SKIPPED or r is _RESERVED:
                if r is _RESERVED:
                    res.reserved += 1
                res.unscored.append(sc)
                continue
            if not (isinstance(r, dict) and r.get("triage")):
                # быстрый фильтр в «здоровье» AI не считаем: иначе при лежащей полной модели
                # успешные триажи скрыли бы алерт «AI не работает»
                res.ai_attempted += 1
            if r is None:
                sc.attempts += 1
                res.unscored.append(sc)
                continue
            if r.get("triage"):
                res.triaged += 1
            else:
                res.ai_ok += 1
            sc.ai = r
            fresh_scored = True
            res.models_used[r.get("model", "?")] = res.models_used.get(r.get("model", "?"), 0) + 1
        # оценка есть (новая или сохранённая раньше): цену считаем кодом, если её ещё нет
        _ensure_price(sc)
        if fresh_scored:
            # сразу в БД как pending: сбой/рестарт до finish_cycle не должен
            # заставить тратить квоту на повторную оценку (finish_cycle перезапишет статус)
            _save_retry(sc, "pending")
        fit = sc.fit
        # отсев быстрым фильтром окончателен при любом FREELANCE_MIN_FIT (порог мог быть <= TRIAGE_REJECT_MAX)
        if (fit is not None and fit < FREELANCE_MIN_FIT) or (sc.ai or {}).get("triage"):
            res.rejected.append(sc)
        else:
            res.to_send.append(sc)

    _report_ai_health(res)

    quota_note = f", квота пуста до {res.quota_out.astimezone(MSK):%H:%M}" if res.quota_out else ""
    logging.info(
        f"[FREELANCE] скан: {res.raw_count}, pending: {res.pending_loaded}, "
        f"кандидатов: {len(candidates)}, к отправке: {len(res.to_send)}, "
        f"отсеяно AI: {len(res.rejected)}, ждут оценки: {len(res.unscored)} "
        f"(из них в резерве квоты: {res.reserved}{quota_note}), "
        f"AI: {res.ai_ok}/{res.ai_attempted} {res.models_used}, быстрый фильтр: {res.triaged}"
    )
    # Что именно отсеял AI: чтобы по логу было видно, не режется ли нужное
    for sc in res.rejected:
        how = ", быстрый фильтр" if (sc.ai or {}).get("triage") else ""
        logging.info(f"[FREELANCE] отсеян AI: fit {sc.fit}/10{how}, {sc.order.site}, "
                     f"{ai_scorer.mask_contacts(sc.order.title or '')[:80]!r}")
    return res


def _save_scored_now(sc: ScoredOrder, result: dict) -> None:
    """Оценка уже получена и стоила квоты: сразу в БД как pending, не дожидаясь конца пачки.
    Рестарт посреди цикла иначе заставил бы оценивать те же заказы заново. Работаем с копией:
    sc.ai основной поток выставит сам (по нему считаются счётчики цикла)."""
    try:
        tmp = ScoredOrder(sc.order, dict(result), sc.dedup, sc.attempts)
        _ensure_price(tmp)
        _save_retry(tmp, "pending")
    except Exception:
        logging.exception(f"[FREELANCE] не удалось сразу сохранить оценку {sc.order.url}")


def _triage(sc: ScoredOrder) -> int | None:
    """Быстрая оценка заказа (результат помним: заказ в резерве квоты не триажим каждый цикл).
    Только для заказов без оценки и без неудачных попыток."""
    if sc.ai is not None or sc.attempts:
        return None
    oid = sc.order.order_id
    with _triage_lock:
        if oid in _triage_cache:
            return _triage_cache[oid]
    try:
        value = ai_scorer.triage_order(sc.order)
    except Exception:
        logging.exception("[TRIAGE] сбой быстрой оценки")
        return None
    if value is not None:
        with _triage_lock:
            if len(_triage_cache) >= TRIAGE_CACHE_MAX:
                _triage_cache.clear()
            _triage_cache[oid] = value
    return value


def _log_triage_shadow(sc: ScoredOrder, triage: int, result: dict) -> None:
    """Сравнение быстрой и полной оценки (режим shadow): по этим строкам решаем, включать ли триаж."""
    try:
        full = int(result["fit"])
    except (KeyError, TypeError, ValueError):
        return
    miss = triage <= TRIAGE_REJECT_MAX and full >= FREELANCE_MIN_FIT
    title = ai_scorer.mask_contacts(sc.order.title or "")[:60]
    logging.info(f"[TRIAGE] заказ {sc.order.order_id}: быстрая {triage}, полная {full}"
                 f"{' - ПРОПУСТИЛ БЫ ХОРОШИЙ' if miss else ''} {title!r}")


def _quota_reserved(group: int) -> bool:
    """True - квоты осталось мало, и заказ этой группы приоритета не оцениваем (RESERVE_SHARE).
    Резерв не применяем, если daily_capacity() вернул None (доступен Gemini или нет
    провайдеров с дневным потолком). Проверка и занятие слота не атомарны: у самого порога
    4 потока могут пропустить пару лишних заказов - это допустимо."""
    share = RESERVE_SHARE.get(group)
    if share is None:
        return False
    cap = ai_scorer.daily_capacity()
    if cap is None:
        return False
    left, total = cap
    return left <= total * share


def _ensure_price(sc: ScoredOrder) -> None:
    """Считает цену по оценке модели и кладёт price_* в sc.ai (уйдёт в ai_json).
    Старые оценки без category (формат с price_rub) не трогаем; цену, посчитанную
    прежней формулой (price_v меньше текущей), пересчитываем."""
    ai = sc.ai
    if not ai or "category" not in ai or ai.get("price_v") == pricing.PRICE_VERSION:
        return
    try:
        price = pricing.price_order(ai, sc.order.price_value)
        # старые price_*/market_*/budget_* не должны пережить пересчёт (например, устаревший рынок)
        for key in [k for k in ai if k.startswith(("price_", "market_", "budget"))]:
            del ai[key]
        ai.update(price)
    except Exception:
        logging.exception(f"[FREELANCE] не удалось посчитать цену {sc.order.url}")


def _report_ai_health(res: CycleResult) -> None:
    """Оповещение, если AI не работает: 3 цикла подряд без единой оценки (не ответил никто
    из провайдеров). Отклонённый ключ отдельного провайдера - отдельное предупреждение,
    «AI не работает» оно не вызывает, пока оценки приходят от других."""
    label = "AI-оценка заказов"
    st = ai_scorer.status()
    key_errors = st.get("key_errors") or {}
    for name in ("gemini", *st.get("providers", {})):
        reason = key_errors.get(name, "")
        if reason:
            monitor.record(f"ai_key_{name}", 0, RuntimeError(reason),
                           label=f"Ключ AI-провайдера {name}", immediate=True,
                           bad_text="ключ отклонён", reason=reason)
        else:
            monitor.record(f"ai_key_{name}", 1, label=f"Ключ AI-провайдера {name}",
                           recovered_text="принят")
    # признак платного вызова (402 или платная модель) - сразу и отдельно: провайдер выключен до рестарта
    paid_errors = st.get("paid_errors") or {}
    for name in st.get("providers", {}):
        reason = paid_errors.get(name, "")
        if reason:
            monitor.record(f"ai_paid_{name}", 0, RuntimeError(reason),
                           label=f"AI-провайдер {name}", immediate=True,
                           bad_text="платный вызов, провайдер выключен до рестарта", reason=reason)
    _report_yandex_grant(st)
    if res.quota_out:
        # пустая квота - не сбой: серия «AI не работает» начинается заново
        kv_set(AI_FAIL_STREAK_KEY, 0)
        monitor.reset_streak("gemini")
        _report_quota_out(res)
    if res.ai_attempted == 0:
        return
    if res.ai_ok == 0:
        streak = int(kv_get(AI_FAIL_STREAK_KEY, "0") or 0) + 1
        kv_set(AI_FAIL_STREAK_KEY, streak)
        reason = ai_scorer.failure_summary() or "нет ответа от моделей"
        # reason= задаём явно: иначе в алерте был бы префикс «ошибка: RuntimeError:»
        monitor.record("gemini", 0, RuntimeError(reason), label=label, reason=reason)
    else:
        kv_set(AI_FAIL_STREAK_KEY, 0)
        monitor.record("gemini", res.ai_ok, label=label)


def _report_quota_out(res: CycleResult) -> None:
    """Квоты AI кончились у всех: одно спокойное сообщение за сутки (МСК) вместо
    «AI не работает». Отметка в kv, чтобы рестарт не повторял его. Короткая пауза
    (минутный лимит, до возврата меньше QUOTA_OUT_MIN_SEC) - молча ждём."""
    now = datetime.now(MSK)
    if (res.quota_out - now).total_seconds() < QUOTA_OUT_MIN_SEC:
        return
    day = now.date().isoformat()
    try:
        if kv_get(QUOTA_OUT_DAY_KEY) == day:
            return
        kv_set(QUOTA_OUT_DAY_KEY, day)
    except Exception as e:
        logging.warning(f"[FREELANCE] сообщение о квоте отложено: БД недоступна ({type(e).__name__})")
        return
    monitor.notify(
        f"⏸ <b>AI-квота на сегодня кончилась</b> ({now:%H:%M} МСК)\n"
        f"Оценка продолжится ~{res.quota_out.astimezone(MSK):%H:%M} МСК. Заказы ждут оценки "
        f"{FREELANCE_MAX_AGE_HOURS} ч, сейчас в очереди: {len(res.unscored)}."
    )


def _report_yandex_grant(st: dict) -> None:
    """Грант Яндекса: по одному сообщению в Telegram за WARN_DAYS дней до отключения,
    при отключении по дате и при исчерпании всего бюджета. Отметка «уже сообщали» - в kv,
    чтобы рестарт не повторял сообщения."""
    info = (st.get("providers", {}).get("yandex") or {}).get("budget")
    if not info or info.get("unknown"):
        return   # Яндекс не настроен (нет YANDEX_ENABLED=1 / ключа / каталога) или учёт не прочитан
    until = datetime.fromisoformat(info["until"]).strftime("%d.%m.%Y")
    spent = f"потрачено {info['total']:.0f} из {info['budget']:.0f} ₽"
    if info["expired"]:
        key, text = "yandex_grant_off", (
            f"срок гранта вышел ({until}), Яндекс больше не вызывается; {spent}. "
            f"Убери yandex из AI_PROVIDERS и YANDEX_ENABLED на BotHost")
    elif info["exhausted"]:
        key, text = "yandex_grant_spent", f"бюджет гранта исчерпан ({spent}), Яндекс больше не вызывается"
    elif info["days_left"] <= yandex_budget.WARN_DAYS:
        key, text = "yandex_grant_soon", (
            f"Яндекс отключится {until} (осталось дней: {info['days_left']}); {spent}")
    else:
        return
    try:
        if kv_get(key):
            return
        kv_set(key, datetime.now(timezone.utc).isoformat())
    except Exception as e:
        # без отметки в БД не шлём: иначе сообщение повторялось бы каждый цикл
        logging.warning(f"[FREELANCE] оповещение о гранте Яндекса отложено: БД недоступна ({type(e).__name__})")
        return
    monitor.record(key, 0, RuntimeError(text), label="Грант Yandex Cloud", immediate=True,
                   bad_text="внимание", reason=text)


def save_order(sc: ScoredOrder, status: str, message_id: int | None = None) -> None:
    o = sc.order
    freelance_save(
        o.order_id, o.site, o.title, o.url, o.price_text,
        o.published_at.isoformat(), sc.dedup, sc.fit,
        json.dumps(sc.ai, ensure_ascii=False) if sc.ai else "",
        status, sc.attempts, message_id,
        category=(sc.ai or {}).get("category"),
        budget=o.price_value if o.price_value and o.price_value > 0 else None,
        hours=(sc.ai or {}).get("hours") or None,
    )
    _feed_store_update(o, status)


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


def _money(value) -> str:
    return f"{int(_num(value)):,}".replace(",", " ")


def _thousands(value: float) -> str:
    k = value / 1000
    return f"{k:.1f}".rstrip("0").rstrip(".")


def _price_basis_line(ai: dict) -> str:
    """Откуда цена: формула · рынок категории · бюджет."""
    parts = []
    basis = str(ai.get("price_basis") or "").strip()
    if basis:
        parts.append(basis[:100])
    m_low, m_high = _num(ai.get("market_low")), _num(ai.get("market_high"))
    name = pricing.CATEGORY_NAMES.get(str(ai.get("category") or ""), "заказов")
    if m_low > 0 and m_high > 0:
        market = f"рынок {name}: {_thousands(m_low)}–{_thousands(m_high)} тыс."
        verdict = str(ai.get("market_verdict") or "").strip()
        parts.append(f"{market} — цена {verdict[:40]}" if verdict else market)
    elif ai.get("market_n") is not None:
        parts.append(f"рынок {name}: мало данных ({int(_num(ai.get('market_n')))} из {pricing.MIN_POINTS})")
    budget = _num(ai.get("budget"))
    if budget > 0:
        b_rate = _num(ai.get("budget_rate"))
        b_text = f"бюджет {_money(budget)} ₽" + (f" ≈ {_money(b_rate)} ₽/ч" if b_rate > 0 else "")
        own = _num(ai.get("price_own"))
        if ai.get("budget_low"):
            b_text += ", занижен"
        elif not ai.get("budget_capped"):
            b_text += ", ок"
        if ai.get("budget_capped") and own > 0:
            b_text += f" · упёрлось в бюджет +10% (по расчёту {_money(own)} ₽)"
        elif ai.get("budget_raised") and own > 0:
            b_text += f" · поднято к бюджету (по расчёту {_money(own)} ₽)"
        parts.append(b_text)
    return " · ".join(parts)


def _components_line(ai: dict) -> str:
    comps = ai.get("components")
    if not isinstance(comps, list):
        return ""
    items = []
    for c in comps[:8]:
        if not isinstance(c, dict):
            continue
        name = str(c.get("name") or "").strip()[:50]
        hours = _fmt_hours(c.get("hours"))
        if name and hours:
            items.append(f"{name} {hours} ч")
    return " · ".join(items)


def _price_note(ai: dict) -> str:
    """Одна пометка после цены; приоритет: бюджет занижен -> упёрлось в бюджет ->
    поднято к бюджету -> вердикт рынка (если не «в рынке»)."""
    if ai.get("budget_low"):
        return "бюджет занижен"
    if ai.get("budget_capped"):
        return "упёрлось в бюджет"
    if ai.get("budget_raised"):
        return "бюджет выше расчёта"
    verdict = str(ai.get("market_verdict") or "").strip()
    if verdict and verdict != "в рынке":
        return verdict[:40]
    return ""


def format_order(sc: ScoredOrder, compact: bool = False, detailed: bool = False) -> str:
    """HTML-сообщение о заказе. По умолчанию короткая карточка; detailed=True добавляет
    вилку цены, расчёт, риск, компоненты и сложность (кнопка «Подробнее»).
    Итог не длиннее MAX_MESSAGE_LEN: подробный упрощается до короткого, короткий - до plain."""
    o, ai = sc.order, sc.ai
    e = html.escape
    fit = sc.fit
    fire = "🔥 " if fit is not None and fit >= 9 else ""
    t = o.published_at.astimezone(MSK).strftime("%H:%M")
    money = f"💰 {e((o.price_text or 'не указана')[:100])} · 🕒 {t}"
    lines = [f"{fire}💼 <b>{e((o.title or '')[:200])}</b>   [{e(site_name(o.site))}]"]
    if ai and fit is not None:
        parts = [f"🤖 {fit}/10"]
        hours = _fmt_hours(ai.get("hours"))
        if hours:
            parts.append(f"~{hours} ч")
        price_mid = _num(ai.get("price_mid"))
        if price_mid <= 0:
            # старая запись без расчёта цены: прежний вывод
            price_rub = str(ai.get("price_rub") or "").strip()
            if price_rub:
                parts.append(f"предложить {e(price_rub[:100])}")
        lines.append(" · ".join([money] + parts))
        if price_mid > 0:
            line = f"💵 Предложить: {_money(price_mid)} ₽"
            note = _price_note(ai)
            if note:
                line += f" · {e(note)}"
            lines.append(line)
        summary = str(ai.get("summary") or "")
        if detailed:
            if price_mid > 0:
                low, high = _num(ai.get("price_low")), _num(ai.get("price_high"))
                if low > 0 and high > 0 and (low != price_mid or high != price_mid):
                    lines.append(f"   вилка: {_money(low)}–{_money(high)} ₽")
                basis = _price_basis_line(ai)
                if basis:
                    lines.append(f"   {e(basis)}")
            difficulty = str(ai.get("difficulty") or "")
            if difficulty:
                lines.append(f"⚙️ Сложность: {e(difficulty[:20])}")
        if not compact:
            if summary:
                lines.append(f"📝 {e(summary[:300])}")
            if detailed:
                risks = str(ai.get("risks") or "")
                if risks:
                    lines.append(f"⚠️ Риск: {e(risks[:300])}")
                comps = _components_line(ai)
                if comps:
                    lines.append(f"🧩 {e(comps)}")
            questions = ai.get("questions")
            if isinstance(questions, list) and questions and str(questions[0]).strip():
                lines.append(f"❓ Уточнить: {e(str(questions[0]).strip()[:200])}")
    else:
        lines.append(money)
        lines.append("🤖 без AI")
        if not compact:
            body = " ".join((o.body or "").split())
            if body:
                lines.append(f"📝 {e(body[:300])}{'…' if len(body) > 300 else ''}")
    text = "\n".join(lines)
    if len(text) > MAX_MESSAGE_LEN:
        if detailed:
            return format_order(sc, compact=compact)
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
