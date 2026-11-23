"""Цикл проверки фриланс-заказов: скан alot.pro, префильтр, дедуп, AI-оценка.

Все функции синхронные (requests) - вызывать через asyncio.to_thread.
Отправку в Telegram делает bot.py: заказ попадает в БД как отправленный
только после успешной отправки.

Схема надёжности (ни один подходящий заказ не теряется молча):
  • курсор alot_last_id идёт только вперёд и не откатывается;
  • неоценённые и неотправленные заказы лежат в БД со статусом pending и счётчиком
    attempts, каждый цикл дозапрашиваются по id (getProjects);
  • без оценки AI заказ в чат не уходит: ждёт в pending, пока AI не ответит
    (о простое AI приходит health-оповещение);
  • принятые заказы остаются в очереди до обработки и доставки;
  • успешно отправленные заказы дополнительно держатся в памяти (_sent_ids), чтобы
    сбой записи в БД не привёл к дублю.
"""
import html
import hashlib
import json
import logging
import math
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from config import (
    ALOT_SEED_ID,
    ALOT_STALE_HOURS,
    FREELANCE_FRESH_HOURS,
    FREELANCE_MIN_FIT,
    FREELANCE_QUEUE_MAX_HOURS,
)
from database import (
    freelance_count_excluded,
    freelance_dup_candidates,
    freelance_get,
    freelance_known,
    freelance_mark_desc_changed,
    freelance_mark_filtered,
    freelance_assessment_get,
    freelance_claim_assessment_notice,
    freelance_pending,
    freelance_reassess_attempt,
    freelance_reassess_finish,
    freelance_reassess_pending,
    freelance_reassess_queue,
    freelance_reassess_state,
    freelance_record_assessment,
    freelance_replace_assessment,
    freelance_requeue,
    freelance_save,
    freelance_set_queue_reason,
    kv_get,
    kv_set,
)
from freelance import ai_scorer, manual, pricing, profile, sources, yandex_budget
from freelance.budget import parse_budget, rub_amount
from freelance import journal as jr
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
from freelance.filters import EXCLUDED_SITES, norm_dedup_key, priority, reject_reason
from health import monitor

DEDUP_DAYS = 3
MAX_AI_PER_CYCLE = 120
AI_WORKERS = 4
# Общий дедлайн на AI-оценку (сек от старта prepare_cycle); неоценённые откладываются
AI_DEADLINE_SEC = 45
# Сколько pending-заказов дозапрашиваем за цикл (пачки по 100 id - лимит getProjects).
# Поток ~150 кандидатов в сутки, за 6 ч свежести pending обычно < 50
PENDING_FETCH_LIMIT = 300
# Сколько pending читаем из БД за цикл, чтобы пометить просроченные (expired)
PENDING_SCAN_LIMIT = 5000
PENDING_BATCH = 100
# Верхняя оценка длительности prepare_cycle (для wait_for в боте)
PREPARE_TIMEOUT_SEC = 1800
# Если столько циклов подряд нет id выше last_id - перепроверяем границу (≈ 1 ч)
STALE_CYCLES_LIMIT = 60
PARTIAL_ALERT_CYCLES = 3   # столько циклов подряд скан обрывается, не продвигая курсор, - тревога
# При первом запуске/застое отступаем от границы на столько id (~50 id/час * 24 ч с запасом)
BOOTSTRAP_LOOKBACK = 1500
# Первый запуск (нет курсора, например БД стёрлась при деплое): берём только последние
# ~50 id (≈1 ч), чтобы не оценивать и не присылать заново суточный поток
FIRST_RUN_LOOKBACK = 50
# Резерв дневной квоты AI для заказов поважнее (группы filters.priority): группа 3
# оценивается, пока осталось больше половины квоты, группа 2 - больше 20%, группа 1 - всегда.
# Отложенные заказы ждут следующей доступной квоты в SQLite.
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
# Shadow-триаж тратит ту же квоту провайдера (Cloudflare Workers AI делит нейроны между триажем и полной
# оценкой), поэтому в shadow он ограничен малой выборкой в сутки, а не TRIAGE_MAX_PER_DAY
try:
    TRIAGE_SHADOW_MAX_PER_DAY = max(0, int(os.environ.get("TRIAGE_SHADOW_MAX_PER_DAY") or 100))
except ValueError:
    TRIAGE_SHADOW_MAX_PER_DAY = 100
# Отсекающий триаж (on) включается только по накопленным данным shadow: без доказанного отсутствия
# пропусков хороших заказов он остаётся в shadow. Нужно: выборка не меньше TRIAGE_MIN_SAMPLE,
# среди неё не меньше TRIAGE_MIN_GOOD подходящих (fit >= порога) и ни одного пропуска
TRIAGE_MIN_SAMPLE = 150
# 0 пропусков среди 60 подходящих: верхняя граница доли пропусков ~5% (правило трёх, доверие 95%).
# Прежние 15 давали лишь <=20% и не доказывали полноту
TRIAGE_MIN_GOOD = 60
TRIAGE_EVIDENCE_KEY = "triage_shadow_stats"
# Совпадение с полной AI-оценкой НЕ доказывает полноту: полная оценка сама может ошибаться. Поэтому отсечение
# требует ещё независимой разметки - решений владельца по кнопкам («Взял» = подходит, «Не подходит» = отказ)
# для заказов, которые прошли shadow-триаж: не меньше TRIAGE_MIN_LABELED подходящих и ни одного пропуска
TRIAGE_MIN_LABELED = 8
TRIAGE_SCORES_KEY = "triage_shadow_scores"   # order_id -> быстрая оценка, последние TRIAGE_SCORES_MAX заказов
TRIAGE_SCORES_MAX = 400
# Подтверждение владельца: случайная выборка заказов, которые триаж отсёк бы (быстрая оценка <= TRIAGE_REJECT_MAX),
# просматривается глазами (/triage_audit), итог вносится /triage_approve. Только числа в настройках сервера
# (FREELANCE_TRIAGE=on) отсечение не включают. TRIAGE_AUDIT_MIN просмотренных без единого подходящего: верхняя
# граница доли подходящих среди отсекаемых ~6% (3/50, доверие 95%) - только для этой выборки и этих настроек
TRIAGE_AUDIT_MIN = 50
TRIAGE_AUDIT_KEY = "triage_audit_approval"
# В режиме on каждый TRIAGE_CONTROL_EVERY-й заказ (по id) всё равно оценивается полной моделью, даже если триаж
# его отсёк бы: так пропуски продолжают измеряться, а первый же пропуск возвращает режим shadow
TRIAGE_CONTROL_EVERY = 10
# n - сравнений с полной оценкой; good - из них подходящих (полная >= порога); cut - сравнений, где триаж отсёк бы заказ
# (быстрая <= TRIAGE_REJECT_MAX); miss - подходящие среди отсекаемых (cut AND good); extra - расхождение в другую сторону
# (триаж пропустил заказ, полная модель его отклонила: лишний расход квоты, не потеря); ctl_cut/ctl_miss - то же только
# для контрольного среза режима on. Три разные величины: доля подходящих среди отсекаемых = miss/cut, доля пропущенных
# среди подходящих = miss/good, расхождения с полной моделью = miss + extra из n
_EVIDENCE_FIELDS = ("n", "good", "miss", "lab_good", "lab_bad", "lab_miss", "cut", "extra", "ctl_cut", "ctl_miss")
_triage_evidence_lock = threading.Lock()
_triage_warned = {"on": False}
# Повторная оценка оценённого заказа после существенной правки описания (Ф06)
REASSESS_PER_CYCLE = 2          # не больше стольких повторных оценок за цикл: основной поток заказов важнее
REASSESS_MAX_ATTEMPTS = 3
REASSESS_SIMILARITY = 0.8       # схожесть слов старого и нового текста ниже - правка существенная
REASSESS_LENGTH_CHANGE = 0.25   # длина текста изменилась на столько и более - правка существенная
NOTICE_FIT_DELTA = 2            # повторное уведомление: оценка изменилась на столько и более
NOTICE_PRICE_CHANGE = 0.3       # или цена полного объёма изменилась на столько и более

# Давность наблюдений триажа: набор статистики и подтверждение владельца недействительны после паузы
# в записях и по достижении предельного возраста (совпадение отпечатка настроек этого не отменяет)
TRIAGE_EVIDENCE_MAX_IDLE_DAYS = 14
TRIAGE_EVIDENCE_MAX_AGE_DAYS = 60

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

# Старение очереди: заказ группы приоритета 3, прождавший AGING_HOURS, ставится наравне с группой 2 (но не
# выше): подходящие заказы не вытесняются навсегда потоком более «приоритетных». Выше группы 1 и
# выше одной ступени не поднимаем - старый заказ чаще уже закрыт
AGING_HOURS = 12.0
AGING_MAX_BOOST = 1.0


def _queue_rank(sc: "ScoredOrder", now: datetime) -> float:
    """Ранг в очереди AI (меньше - раньше): группа приоритета минус бонус за ожидание."""
    group = priority(sc.order)
    waited = max(0.0, (now - sc.first_seen).total_seconds() / 3600) if sc.first_seen else 0.0
    return group - min(AGING_MAX_BOOST, waited / AGING_HOURS)


def _eff_group(sc: "ScoredOrder", now: datetime) -> int:
    """Группа приоритета с учётом старения (для резерва квоты)."""
    return max(1, math.ceil(_queue_rank(sc, now) - 1e-9))

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
    queue_reason: str | None = None   # почему не оценён (journal.Q_*)
    first_seen: datetime | None = None   # когда бот впервые увидел заказ (очередь и старение)
    stale_note: str = ""     # пометка карточки: оценка относится к прежнему тексту / обновлена после правки

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
    excluded: dict = field(default_factory=dict)   # {площадка: сколько вакансионных заказов отсеяно}
    intake: dict = field(default_factory=dict)     # счётчики приёма: new, filtered, duplicate, stale, updated
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
    reassessed: int = 0      # повторных оценок после правки описания за цикл
    reassess_notes: list[str] = field(default_factory=list)   # повторные уведомления об обновлённой оценке


def prepare_cycle(only_manual: bool = False) -> CycleResult:
    """Собирает новые заказы и оценивает их. Ничего не отправляет.

    Если предыдущий поток (переживший wait_for в боте) ещё работает,
    возвращает busy: параллельных циклов не бывает.
    """
    if not _cycle_lock.acquire(blocking=False):
        return CycleResult(busy=True, error="предыдущая проверка ещё выполняется")
    try:
        return _prepare_cycle(only_manual) if only_manual else _prepare_cycle()
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
        published_at=_parse_dt(row.get("published_at")),
        url=row.get("url") or "",
    )
    return ScoredOrder(order, ai, row.get("dedup_key") or "", stale_note=_stale_note(row, ai))


STALE_PENDING = "описание изменено, оценка относится к прежнему тексту (ждёт повторной оценки)"
STALE_MINOR = "описание немного изменено, оценка прежняя"
STALE_DONE = "оценка обновлена после правки описания"


def _stale_note(row: dict, ai: dict | None) -> str:
    """Пометка карточки о версии оценки: до повторной оценки явно говорит, что оценка устарела."""
    if row.get("desc_changed"):
        try:
            state = freelance_reassess_state(int(row["order_id"]))
        except Exception:
            state = None
        if state and state.get("significant") and state.get("status") == "pending":
            return STALE_PENDING
        return STALE_MINOR
    return STALE_DONE if ai and ai.get("reassessed") else ""


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
    threshold = datetime.now(timezone.utc) - timedelta(hours=FREELANCE_QUEUE_MAX_HOURS)
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


def _parse_seen(value) -> datetime | None:
    """seen_at в БД - время SQLite (UTC, 'YYYY-MM-DD HH:MM:SS')."""
    try:
        return datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return _parse_dt(value) if value else None


def _load_pending(threshold: datetime, sent_now: set[int],
                  feed_orders: list[FreelanceOrder] | None = None,
                  fetch_alot: bool = True,
                  have: dict[int, FreelanceOrder] | None = None, only_manual: bool = False) -> dict[int, ScoredOrder]:
    """Восстанавливает очередь включённых источников из ленты и сохранённых данных."""
    have = have or {}
    active = {n: sources.is_enabled(n) and (n != sources.ALOT or fetch_alot) for n in sources.ALL_SOURCES}
    inactive = [n for n in sources.ALL_SOURCES if not active[n]]
    live: dict[int, dict] = {}

    ranges = [(sources.MANUAL_ID_OFFSET, 1 << 62)] if only_manual else sources.id_ranges(inactive)
    for row in freelance_pending(PENDING_SCAN_LIMIT, ranges, exclude_ranges=not only_manual):
        oid = row["order_id"]
        if oid in sent_now:
            continue
        if len(live) < PENDING_FETCH_LIMIT:
            live[oid] = row
    if not live:
        return {}
    items: list[dict] = []
    ids = sorted((i for i in live if sources.source_of(i) == sources.ALOT and i not in have), reverse=True)  # новые первыми: при сбое сети теряем хвост
    for i in range(0, len(ids), PENDING_BATCH):
        try:
            items += _fetch_items(ids[i:i + PENDING_BATCH])
        except Exception:
            # Сеть/alot недоступны: ожидающие остаются в БД и будут запрошены в следующий раз
            logging.exception("[FREELANCE] не удалось дозапросить ожидающие заказы")
            break
    fetched = [o for o in (_parse_item(item) for item in items) if o]
    fetched += [have[i] for i in live if i in have and not sources.is_feed_id(i)]
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
    fetched += [o for oid, o in feed_by_id.items() if oid in live]
    fetched += [manual.from_row(row) for row in live.values() if row.get("origin") == "manual"]
    # После исчезновения объявления восстанавливаем сохранённый для оценки текст.
    available = {order.order_id for order in fetched}
    restored = set()
    for oid, row in live.items():
        if oid in available or not (row.get("snippet") or row.get("ai_json")):
            continue
        order = manual.from_row(row)
        order.published_at = _parse_dt(row.get("published_at"))
        fetched.append(order)
        restored.add(oid)
    result: dict[int, ScoredOrder] = {}
    for order in fetched:
        if order.order_id not in live or order.order_id in result:
            continue
        row = live[order.order_id]
        why = None if order.order_id in restored else reject_reason(order)
        if why is not None:
            freelance_mark_filtered(order.order_id, why)
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
            attempts=int(row.get("attempts") or 0), queue_reason=row.get("queue_reason"),
            first_seen=_parse_seen(row.get("seen_at")),
        )
    # без описания (выпал из ленты FL.ru/Kwork, перезапуск): ждёт, причина видна в журнале
    for oid, row in live.items():
        if oid not in result and sources.is_feed_id(oid) and row.get("tech") == jr.QUEUED \
                and row.get("queue_reason") != jr.Q_NO_TEXT:
            freelance_set_queue_reason(oid, jr.Q_NO_TEXT)
    return result


def _full_snippet(o: FreelanceOrder) -> str:
    """Обезличенный текст заказа для повторной оценки: ровно то, что видит AI."""
    return ai_scorer.mask_contacts(o.body or "")[:ai_scorer.BODY_LIMIT]


def _db_budget(o: FreelanceOrder) -> float | None:
    """Бюджет для колонки budget (и статистики рынка): только рублёвая сопоставимая сумма. Иностранная и неизвестная
    валюта, ставка в час, отсутствие суммы и испорченные значения дают None, а не число «как есть»."""
    amount = rub_amount(parse_budget(o.price_text))
    return amount if amount > 0 else None


def _journal_new(o: FreelanceOrder, tech: str, *, full: bool, **states) -> None:
    """Первая запись об увиденном заказе. Название маскируется всегда (телефон, почта, токен -> метка):
    карточка из БД показывает обезличенное название, контакты заказчика в журнал не попадают."""
    origin = sources.source_of(o.order_id)
    snippet = _full_snippet(o)
    if not full:
        snippet = snippet[:jr.SNIPPET_FILTERED]
    title = ai_scorer.mask_contacts(o.title or "")   # контакты заказчика в БД не попадают ни в каком виде
    freelance_save(
        o.order_id, o.site, title, o.url, ai_scorer.mask_contacts(o.price_text or ""),
        o.published_at.isoformat(), norm_dedup_key(o), None, "", "pending", 0, None,
        budget=_db_budget(o),
        origin=origin, title_key=jr.title_key(o.site, o.title),
        content_hash=jr.content_hash(o.title, o.body), snippet=snippet,
        pub_unreliable=jr.pub_unreliable(o.site, origin), tech=tech, **states,
    )


def _find_duplicate(o: FreelanceOrder) -> int | None:
    """Оригинал, из-за которого заказ не надо оценивать заново. У alot.pro нет ни ссылки, ни id исходной
    биржи, поэтому «биржа + id» работает только внутри одного источника (order_id уже в БД). Между
    источниками (alot и прямой FL.ru/Kwork) совпадают биржа и название, а текст сравнивается как
    вспомогательный признак (journal.same_listing); по одному названию заказы не склеиваются."""
    origin = sources.source_of(o.order_id)
    canonical = manual.original(o.url)
    if canonical is not None and canonical != o.order_id:
        return canonical
    cands = freelance_dup_candidates(jr.title_key(o.site, o.title), DEDUP_DAYS, o.order_id)
    if not cands:
        return None
    new = {"body": jr.norm_text(_full_snippet(o)), "origin": origin, "price": o.price_value or 0,
           "published_ts": o.published_at.timestamp() if o.published_at else 0, "reliable": bool(o.published_at) and not jr.pub_unreliable(o.site, origin)}
    for c in cands:
        published = _parse_dt(c.get("published_at"))
        other = {"body": jr.norm_text(c.get("snippet") or ""), "origin": c.get("origin"),
                 "price": c.get("price") or 0, "published_ts": published.timestamp() if published else 0,
                 "reliable": not c.get("unreliable") and published is not None}
        if jr.same_listing(new, other):
            return int(c["order_id"])
    return None


def _handle_updated(o: FreelanceOrder, known: dict, stats: dict) -> None:
    """Заказ уже в журнале, но заказчик мог поправить описание (отпечаток содержимого изменился).
    Ожидающий оценки возьмёт новый текст сам; отсеянный правилом получает шанс пройти заново; уже
    оценённый не оценивается повторно, на нём остаётся пометка desc_changed."""
    old_hash = known.get("content_hash")
    new_hash = jr.content_hash(o.title, o.body)
    if not old_hash or old_hash == new_hash:
        return
    snippet = _full_snippet(o)
    stats["updated"] = stats.get("updated", 0) + 1
    if known.get("tech") == jr.FILTERED and reject_reason(o) is None:
        freelance_requeue(o.order_id, new_hash, snippet)
    else:
        freelance_mark_desc_changed(o.order_id, new_hash, snippet)
        if known.get("tech") == jr.ASSESSED:
            _queue_reassess(o.order_id, new_hash, known.get("snippet") or "", snippet)


def _words(text: str) -> set[str]:
    return set(re.findall(r"\w+", (text or "").lower().replace("ё", "е")))


def _text_change(old: str, new: str) -> tuple[float, bool]:
    """(схожесть слов 0..1, правка существенна). Существенна, если слов общих меньше REASSESS_SIMILARITY
    или длина текста изменилась на REASSESS_LENGTH_CHANGE и более; пустой прежний текст - всегда существенна."""
    a, b = _words(old), _words(new)
    similarity = len(a & b) / len(a | b) if (a or b) else 1.0
    la, lb = len(old or ""), len(new or "")
    length_change = abs(lb - la) / max(la, lb, 1)
    return similarity, (not a) or similarity < REASSESS_SIMILARITY or length_change >= REASSESS_LENGTH_CHANGE


def _queue_reassess(order_id: int, new_hash: str, old_snippet: str, new_snippet: str) -> None:
    """Оценённый заказ получил новое описание: запись о повторной оценке (оценка прежняя до её выполнения)."""
    try:
        similarity, significant = _text_change(old_snippet, new_snippet)
        waiting = freelance_reassess_state(order_id)
        if waiting and waiting.get("status") == "pending" and waiting.get("significant"):
            significant = True      # незавершённая существенная правка не отменяется мелкой последующей
        row = freelance_get(order_id) or {}
        try:
            ai = json.loads(row.get("ai_json") or "{}")
        except ValueError:
            ai = {}
        old_price = _num(ai.get("price_mid")) if isinstance(ai, dict) else 0.0
        freelance_reassess_queue(order_id, new_hash, similarity, significant, row.get("fit"), old_price or None)
    except Exception:
        logging.exception(f"[FREELANCE] не удалось поставить повторную оценку заказа {order_id}")


def _intake(orders, last_id: int,
            sent_now: set[int]) -> tuple[dict[int, FreelanceOrder], dict, list[FreelanceOrder]]:
    """Приём всех увиденных заказов: каждый получает объяснимое состояние в журнале. Порядок - от старых
    к новым, чтобы оригиналом считался первый опубликованный. Возвращает ({order_id: заказ} для
    дальнейшей работы в памяти, счётчики, новые заказы к оценке). Вакансионные доски строк не получают (их тысячи в сутки),
    только счётчик; считаются лишь новые за курсором alot (хвост окна скан перечитывает)."""
    uniq: dict[int, FreelanceOrder] = {}
    for o in orders:
        uniq.setdefault(o.order_id, o)
    known = freelance_known(list(uniq))
    stats: dict = {"new": 0, "filtered": 0, "duplicate": 0, "stale": 0, "updated": 0, "excluded": {}}
    new_orders: list[FreelanceOrder] = []
    reasons: dict[str, int] = {}
    threshold = datetime.now(timezone.utc) - timedelta(hours=FREELANCE_QUEUE_MAX_HOURS)
    for o in sorted(uniq.values(), key=lambda x: (x.published_at, x.order_id)):
        if o.order_id in sent_now:
            continue
        if o.site in EXCLUDED_SITES:
            if sources.source_of(o.order_id) == sources.ALOT and o.order_id > last_id:
                stats["excluded"][o.site] = stats["excluded"].get(o.site, 0) + 1
            continue
        if o.order_id in known:
            _handle_updated(o, known[o.order_id], stats)
            continue
        canonical = manual.original(o.url)
        if canonical is not None and canonical != o.order_id:
            manual.complete_from_feed(canonical, o)
            _journal_new(o, jr.DUPLICATE, full=False, dup_of=canonical)
            stats["duplicate"] += 1
            continue
        origin = sources.source_of(o.order_id)
        why = reject_reason(o)
        if why is not None:
            key = why.split(":", 1)[0]
            reasons[key] = reasons.get(key, 0) + 1
            if (o.site == "youdoru" or key == "stop_word") and o.order_id not in _prefilter_logged:
                if len(_prefilter_logged) >= _PREFILTER_LOG_MAX:
                    _prefilter_logged.clear()
                _prefilter_logged.add(o.order_id)
                title = ai_scorer.mask_contacts(o.title or "")[:80]
                logging.info(f"[FREELANCE] префильтр: {why}, {o.site} {o.order_id}, {title!r}")
            _journal_new(o, jr.FILTERED, full=False, reject_reason=why)
            stats["filtered"] += 1
            continue
        if not jr.pub_unreliable(o.site, origin) and o.published_at < threshold:
            _journal_new(o, jr.STALE, full=False, stale_reason=jr.S_OLD_AT_DISCOVERY)
            stats["stale"] += 1
            continue
        dup_of = _find_duplicate(o)
        if dup_of is not None:
            _log_dropped(o, f"дубль заказа {dup_of}")
            _journal_new(o, jr.DUPLICATE, full=False, dup_of=dup_of)
            stats["duplicate"] += 1
            continue
        _journal_new(o, jr.QUEUED, full=True, queue_reason=jr.Q_WAIT)
        stats["new"] += 1
        new_orders.append(o)
    if reasons:
        logging.info(f"[FREELANCE] префильтр отсеял: {reasons}")
    return uniq, stats, new_orders


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


def feed_lag(orders, now: datetime | None = None):
    """Насколько свежа выдача alot.pro: (время самого нового заказа, часов назад, время последнего заказа Youdo
    или None, если его нет в просмотренном окне). None - окно пустое, судить не по чему."""
    if not orders:
        return None
    now = now or datetime.now(timezone.utc)
    newest = max(o.published_at for o in orders)
    youdo = [o.published_at for o in orders if o.site == "youdoru"]
    return newest, (now - newest).total_seconds() / 3600, (max(youdo) if youdo else None)


_feed_seen: dict = {"newest": None}   # самый новый заказ, когда-либо увиденный в выдаче этим процессом


def _report_feed_freshness(scan, now: datetime | None = None, threshold: float = ALOT_STALE_HOURS) -> None:
    """Оповещение «выдача alot.pro отстаёт»: alot.pro сам запаздывает с заказами (например, на 5-7 часов), и тогда бот
    «молчит» не по своей вине. Серия плохих проверок и сообщение о восстановлении - в health.monitor (в памяти процесса).
    Окно скана обычно перечитывает последние ~100 id и не пустеет; если всё же пустое, судим по самому новому
    заказу, который процесс видел раньше (после рестарта памяти нет - тогда пустое окно ничего не говорит)."""
    now = now or datetime.now(timezone.utc)
    lag = feed_lag(scan.orders, now)
    youdo = lag[2] if lag else None
    candidates = [t for t in (lag[0] if lag else None, _feed_seen["newest"]) if t is not None]
    if not candidates:
        return   # ни этого окна, ни истории: судить не по чему (сбой скана учитывается отдельным источником "alot")
    newest = max(candidates)
    _feed_seen["newest"] = newest
    hours = (now - newest).total_seconds() / 3600
    stale = hours > threshold
    reason = None
    if stale:
        youdo_text = f"заказ Youdo в выдаче: {youdo.astimezone(MSK):%d.%m %H:%M} МСК" if youdo else "заказов Youdo в просмотренном окне нет"
        reason = (f"самый новый заказ в выдаче alot.pro опубликован {newest.astimezone(MSK):%d.%m %H:%M} МСК "
                  f"({hours:.1f} ч назад, порог {threshold:g} ч); {youdo_text}. Новые заказы Youdo и других бирж "
                  f"в бот не приходят, пока alot.pro не догонит: проверьте Youdo вручную")
    monitor.record("alot_feed", 0 if stale else 1, label="Свежесть выдачи alot.pro", bad_text="выдача отстаёт",
                   recovered_text="снова свежая", reason=reason)


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
    failed_page = getattr(orders, "failed_page", None)
    if failed_page is None:
        monitor.record(name, len(orders), label=label)
    else:
        error = RuntimeError(f"Частичный обход, недоступна страница {failed_page}")
        monitor.record(name, len(orders), error, label=label)
    return orders


def _fetch_fl_orders() -> list[FreelanceOrder]:
    return _fetch_source(sources.FLRU, lambda: fetch_fl_orders(), "Источник фриланс-заказов FL.ru")


def _fetch_kwork_orders() -> list[FreelanceOrder]:
    return _fetch_source(sources.KWORK, lambda: fetch_kwork_orders(), "Источник фриланс-заказов Kwork")


def _fetch_feed_orders() -> list[FreelanceOrder]:
    """Заказы прямых источников (кроме alot.pro), которые включены."""
    with ThreadPoolExecutor(2) as pool:
        fl = pool.submit(_fetch_fl_orders)
        kwork = pool.submit(_fetch_kwork_orders)
        return [*fl.result(), *kwork.result()]


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
            f"{' и '.join(direct)} работают напрямую и обычно быстрее alot.pro. Один и тот же заказ с двух "
            f"источников определяется до AI и оценивается один раз, квота не тратится дважды; "
            f"отключать прямые источники из-за восстановления alot.pro не нужно."
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


def _prepare_cycle(only_manual: bool = False) -> CycleResult:
    res = CycleResult()
    scan, last_id = ScanResult(), 0
    alot_ok, alot_err = False, None
    if not only_manual and sources.is_enabled(sources.ALOT):
        try:
            scan, last_id = _scan_alot()
            alot_ok = True
        except Exception as e:
            logging.exception("[FREELANCE] ошибка получения заказов")
            monitor.record("alot", 0, e, label="Источник фриланс-заказов alot.pro")
            alot_err = e
    elif not only_manual:
        monitor.reset_streak("alot")   # выключен намеренно: не поломка
        monitor.reset_streak("alot_feed")
    feed_orders = [] if only_manual else _fetch_feed_orders()   # FL.ru и Kwork (если включены)
    # Ошибка цикла - только когда брать заказы неоткуда. Пустая лента включённого FL.ru/Kwork -
    # нормальный исход: pending из БД (в том числе выпавшие из ленты) всё равно дообрабатываются
    if not alot_ok and not feed_orders and not manual.pending() and not any(
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
        try:
            _report_feed_freshness(scan)
        except Exception:
            logging.exception("[FREELANCE] не удалось проверить свежесть выдачи alot.pro")
        res.new_last_id = max(last_id, scan.max_id)  # курсор только вперёд
        stale = int(kv_get("freelance_stale", "0") or 0)
        if scan.max_id > last_id:
            kv_set("freelance_stale", 0)
        elif not scan.partial or stuck:
            # единичный оборванный скан не признак застоя: до границы просто не дошли
            kv_set("freelance_stale", stale + 1)
        _track_alot_activity(scan.max_id > last_id)
    res.raw_count = scan.raw_count + len(feed_orders)
    if not only_manual:
        kv_set("freelance_last_raw", res.raw_count)

    with _sent_lock:
        sent_now = set(_sent_ids)

    # Приём: каждый увиденный заказ получает состояние в журнале (отсеян с причиной, дубль с оригиналом,
    # устарел, ждёт оценки). Свежесть от публикации больше не отбрасывает заказ молча
    with manual.intake_lock:
        have, intake, new_orders = _intake([*scan.orders, *feed_orders], last_id, sent_now)
    res.intake = intake
    res.excluded = intake["excluded"]
    seen_now = datetime.now(timezone.utc)
    fresh = [ScoredOrder(o, None, norm_dedup_key(o), queue_reason=jr.Q_WAIT, first_seen=seen_now)
             for o in new_orders]

    # Ожидающие (новые и прежние): alot-заказы из скана берутся из памяти, остальные дозапрашиваются по id,
    # FL.ru и Kwork - из ленты этого цикла
    threshold = datetime.now(timezone.utc) - timedelta(hours=FREELANCE_QUEUE_MAX_HOURS)
    pending = _load_pending(threshold, sent_now, feed_orders, fetch_alot=alot_ok, have=have, only_manual=only_manual)
    res.pending_loaded = len(pending)
    fresh_ids = {sc.order.order_id for sc in fresh}
    candidates: list[ScoredOrder] = fresh + [sc for oid, sc in pending.items() if oid not in fresh_ids]
    res.candidates = len(candidates)

    # AI-оценка в несколько потоков, новые первыми. Заказ без оценки (лимит, дедлайн,
    # сбой) остаётся pending и оценивается в следующих циклах, пока свежий: без
    # оценки в чат не уходит (иначе при исчерпанной квоте летит весь поток без фильтра).
    # Очередь: сначала группа приоритета 1 (см. filters.priority), внутри группы новые первыми.
    ai_on = ai_scorer.is_enabled()
    to_score = [sc for sc in candidates if sc.ai is None and ai_on]
    now_ts = datetime.now(timezone.utc)
    to_score.sort(key=lambda sc: (_queue_rank(sc, now_ts), -(sc.order.published_at or sc.first_seen or now_ts).timestamp()))
    # Чередуем новые заявки и накопленный остаток, чтобы обрабатывать оба потока.
    newest = sorted((sc for sc in to_score if sc.order.order_id in fresh_ids),
                    key=lambda sc: (priority(sc.order), -(sc.order.published_at or now_ts).timestamp()))
    waiting = [sc for sc in to_score if sc.order.order_id not in fresh_ids]
    if newest and waiting:
        batch = max(1, AI_WORKERS - 1)
        merged = []
        rounds = min(len(waiting), (len(newest) + batch - 1) // batch)
        for index in range(rounds):
            merged.extend(newest[index * batch:(index + 1) * batch])
            merged.append(waiting[index])
        to_score = merged + newest[rounds * batch:] + waiting[rounds:]
    # Квоты нет ни у кого: не дёргаем провайдеров (каждая попытка была бы «сбоем» и
    # через 3 цикла - ложным «AI не работает»), заказы ждут в pending
    res.quota_out = ai_scorer.quota_exhausted() if to_score else None

    triage_mode = effective_triage_mode()

    started = time.monotonic()

    def _score(item):
        idx, sc = item
        if idx >= MAX_AI_PER_CYCLE or time.monotonic() - started >= AI_DEADLINE_SEC:
            return _SKIPPED
        will_score = not res.quota_out and not _quota_reserved(_eff_group(sc, now_ts))
        # shadow: быстрая оценка нужна только для сравнения с полной, значит, когда полная будет
        shadow_ok = (triage_mode == "shadow" and will_score
                     and ai_scorer.triage_today() < TRIAGE_SHADOW_MAX_PER_DAY)
        triage = _triage(sc) if (triage_mode == "on" or shadow_ok) else None
        control = triage_mode == "on" and _is_control(sc.order.order_id)
        if triage_mode == "on" and not control and triage is not None and triage <= TRIAGE_REJECT_MAX:
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
        if (triage_mode == "shadow" or control) and triage is not None and result is not None:
            _log_triage_shadow(sc, triage, result, control)
        return result

    with ThreadPoolExecutor(AI_WORKERS) as pool:
        results = list(pool.map(_score, enumerate(to_score)))
    scored = {id(sc): r for sc, r in zip(to_score, results)}

    for sc in candidates:
        fresh_scored = False
        if sc.ai is None:
            if id(sc) not in scored:
                # Даже при отключённом провайдере отправляем только после оценки.
                sc.queue_reason = jr.Q_NO_QUOTA
                res.unscored.append(sc)
                continue
            r = scored[id(sc)]
            if r is _SKIPPED or r is _RESERVED:
                if r is _RESERVED:
                    res.reserved += 1
                    sc.queue_reason = jr.Q_NO_QUOTA if res.quota_out else jr.Q_RESERVED
                else:
                    sc.queue_reason = jr.Q_WAIT
                res.unscored.append(sc)
                continue
            if not (isinstance(r, dict) and r.get("triage")):
                # быстрый фильтр в «здоровье» AI не считаем: иначе при лежащей полной модели
                # успешные триажи скрыли бы алерт «AI не работает»
                res.ai_attempted += 1
            if r is None:
                sc.attempts += 1
                sc.queue_reason = jr.Q_ERROR
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

    if not only_manual:
        _reassess_changed(res)
    _report_ai_health(res)

    quota_note = f", квота пуста до {res.quota_out.astimezone(MSK):%H:%M}" if res.quota_out else ""
    logging.info(
        f"[FREELANCE] приём: {res.intake}, "
        f"скан: {res.raw_count}, pending: {res.pending_loaded}, "
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


def _reassess_changed(res: CycleResult) -> None:
    """Повторная оценка заказов с существенно изменённым описанием. Идёт после основной очереди и только когда
    квоты хватает: не больше REASSESS_PER_CYCLE за цикл, не больше REASSESS_MAX_ATTEMPTS попыток на версию текста.
    Любой сбой не должен ломать цикл."""
    try:
        for row in freelance_reassess_pending(REASSESS_PER_CYCLE, REASSESS_MAX_ATTEMPTS):
            if not ai_scorer.is_enabled() or ai_scorer.quota_exhausted() or _quota_reserved(3):
                return      # квота проверяется перед каждым заказом: запись остаётся pending
            try:
                _reassess_one(res, row)
            except Exception:
                logging.exception(f"[FREELANCE] повторная оценка заказа {row.get('order_id')} не удалась")
    except Exception:
        logging.exception("[FREELANCE] повторная оценка: сбой")


def _reassess_one(res: CycleResult, row: dict) -> None:
    oid, version = int(row["order_id"]), row["content_hash"]
    cur = freelance_get(oid)
    if cur is None or cur.get("tech") != jr.ASSESSED or cur.get("content_hash") != version:
        freelance_reassess_finish(oid, version, "skipped")   # заказ ушёл или текст изменился ещё раз (там своя запись)
        return
    stored = freelance_assessment_get(oid, version)
    if stored is not None:
        # такая версия текста уже оценена (например, правка вернулась к прежнему тексту): квота не тратится
        try:
            new_ai = json.loads(stored["ai_json"])
        except ValueError:
            new_ai = None
    else:
        new_ai = None
    reused = isinstance(new_ai, dict) and "fit" in new_ai
    if not reused:
        order = FreelanceOrder(
            order_id=oid, site=cur.get("site") or "", title=cur.get("title") or "", body=cur.get("snippet") or "",
            price_text=cur.get("price_text") or "", price_value=0.0, categories=[],
            published_at=_parse_dt(cur.get("published_at")), url=cur.get("url") or "")
        try:
            result = ai_scorer.score_order(order)
        except Exception:
            logging.exception(f"[FREELANCE] повторная оценка заказа {oid}: сбой AI")
            result = None
        if result is None:
            freelance_reassess_attempt(oid, version)     # и пустой ответ, и исключение - неудачная попытка версии
            return
        sc = ScoredOrder(order, dict(result), cur.get("dedup_key") or "")
        _ensure_price(sc)
        sc.ai["reassessed"] = True
        new_ai = sc.ai
        freelance_record_assessment(oid, version, sc.fit, str(result.get("model") or ""),
                                    json.dumps(new_ai, ensure_ascii=False))
    new_ai["reassessed"] = True
    new_fit = int(new_ai["fit"])
    delivered = cur.get("delivery") == jr.D_CARD
    delivery = verdict = None
    if cur.get("delivery") in (None, jr.D_NONE, jr.D_PENDING, jr.D_UNKNOWN):
        # ещё не доставлен: решение о доставке принимается по новой оценке (это первое уведомление, не повторное)
        low = new_fit < FREELANCE_MIN_FIT
        delivery, verdict = (jr.D_NONE, jr.V_LOW) if low else (jr.D_PENDING, jr.V_OK)
    if not freelance_replace_assessment(oid, version, new_fit, json.dumps(new_ai, ensure_ascii=False),
                                        new_ai.get("category"), new_ai.get("hours") or None, delivery, verdict):
        return      # за время оценки текст изменился: запись о новой версии поставлена отдельно
    freelance_reassess_finish(oid, version, "done")
    res.reassessed += 1
    if delivered and _notice_needed(row.get("old_fit"), row.get("old_price"), new_fit, _num(new_ai.get("price_mid"))):
        if freelance_claim_assessment_notice(oid, version):
            res.reassess_notes.append(format_reassess_note(cur, row.get("old_fit"), row.get("old_price"), new_ai))


def _notice_needed(old_fit, old_price, new_fit: int, new_price: float) -> bool:
    """Повторное уведомление о доставленном заказе: оценка пересекла порог вверх, изменилась на NOTICE_FIT_DELTA и более
    либо цена полного объёма изменилась на NOTICE_PRICE_CHANGE и более. Иначе оценка обновляется молча."""
    if old_fit is None:
        return False
    old_fit = int(old_fit)
    if old_fit < FREELANCE_MIN_FIT <= new_fit or abs(new_fit - old_fit) >= NOTICE_FIT_DELTA:
        return True
    old_price = _num(old_price)
    return old_price > 0 and new_price > 0 and abs(new_price - old_price) / old_price >= NOTICE_PRICE_CHANGE


def format_reassess_note(row: dict, old_fit, old_price, ai: dict) -> str:
    """Короткое HTML-сообщение: описание заказа изменилось, оценка обновлена (было -> стало)."""
    e = html.escape
    lines = [f"🔄 Описание заказа изменилось, оценка обновлена: <b>{e((row.get('title') or '')[:200])}</b>"]
    lines.append(f"🤖 было {old_fit}/10 → стало {ai.get('fit')}/10")
    price = _num(ai.get("price_mid"))
    if price > 0:
        was = f"{_money(old_price)} → " if _num(old_price) > 0 else ""
        lines.append(f"💵 Цена полного объёма: {was}{_money(price)} ₽")
    summary = str(ai.get("summary") or "")
    if summary:
        lines.append(f"📝 {e(summary[:300])}")
    if row.get("url"):
        lines.append(e(str(row["url"])))
    return "\n".join(lines)


def _save_scored_now(sc: ScoredOrder, result: dict) -> None:
    """Оценка уже получена и стоила квоты: сразу в БД как pending, не дожидаясь конца пачки.
    Рестарт посреди цикла иначе заставил бы оценивать те же заказы заново. Работаем с копией:
    sc.ai основной поток выставит сам (по нему считаются счётчики цикла)."""
    try:
        tmp = ScoredOrder(sc.order, dict(result), sc.dedup, sc.attempts)
        _ensure_price(tmp)
        _save_retry(tmp, "pending")
        if not result.get("triage") and sc.order.body:
            freelance_record_assessment(sc.order.order_id, jr.content_hash(sc.order.title, sc.order.body),
                                        tmp.fit, str(result.get("model") or ""),
                                        json.dumps(tmp.ai, ensure_ascii=False))
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


def _evidence_signature() -> str:
    """Отпечаток условий, при которых набрана статистика: модель, промпты, цепочка полной оценки (ai_scorer)
    и пороги (TRIAGE_REJECT_MAX, FREELANCE_MIN_FIT)."""
    return f"{ai_scorer.triage_signature()}|rej{TRIAGE_REJECT_MAX}|fit{FREELANCE_MIN_FIT}"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _evidence_aged(started, updated, now: datetime | None = None) -> bool:
    """Набор наблюдений недействителен: пауза с последней записи больше TRIAGE_EVIDENCE_MAX_IDLE_DAYS, возраст набора
    больше TRIAGE_EVIDENCE_MAX_AGE_DAYS либо меток времени нет (старый формат), либо метка из будущего."""
    first, last = _parse_dt(str(started or "")), _parse_dt(str(updated or ""))
    if first is None or last is None:
        return True
    now = now or _now()
    return (now - last > timedelta(days=TRIAGE_EVIDENCE_MAX_IDLE_DAYS)
            or now - first > timedelta(days=TRIAGE_EVIDENCE_MAX_AGE_DAYS)
            or last > now + timedelta(days=1) or last < first)


def _load_evidence() -> dict:
    try:
        data = json.loads(kv_get(TRIAGE_EVIDENCE_KEY, "") or "{}")
        ev = {k: int(data.get(k) or 0) for k in _EVIDENCE_FIELDS}
        ev["sig"] = str(data.get("sig") or "")
        ev["started"], ev["updated"] = str(data.get("started") or ""), str(data.get("updated") or "")
        return ev
    except (ValueError, TypeError, AttributeError):
        return {**{k: 0 for k in _EVIDENCE_FIELDS}, "sig": "", "started": "", "updated": ""}


def triage_evidence() -> dict:
    """Накопленная статистика триажа: n/good/miss - сравнение с полной AI-оценкой; lab_good/lab_bad/lab_miss -
    независимая разметка владельца. Хранится в kv (переживает рестарт). Статистика, набранная при другой модели,
    промпте или порогах (отпечаток не совпал), не используется: возвращаются нули и stale=True."""
    ev = _load_evidence()
    current = _evidence_signature()
    if ev["sig"] != current:
        return {**{k: 0 for k in _EVIDENCE_FIELDS}, "sig": current, "stale": bool(ev["sig"] or any(
            ev[k] for k in _EVIDENCE_FIELDS)), "expired": False}
    if any(ev[k] for k in _EVIDENCE_FIELDS) and _evidence_aged(ev["started"], ev["updated"]):
        # тот же отпечаток, но наблюдения старые или с паузой: возврат настроек их не оживляет
        return {**{k: 0 for k in _EVIDENCE_FIELDS}, "sig": current, "stale": True, "expired": True}
    return {**ev, "stale": False, "expired": False}


def _save_evidence(ev: dict) -> None:
    now = _now().isoformat()
    started = ev.get("started") or now
    kv_set(TRIAGE_EVIDENCE_KEY, json.dumps({**{k: ev[k] for k in _EVIDENCE_FIELDS}, "sig": ev["sig"],
                                            "started": started, "updated": now}))


def _is_control(order_id: int) -> bool:
    """Контрольный срез режима on: каждый ~10-й заказ (по хэшу id и отпечатка настроек) оценивается полной моделью,
    даже если триаж его отсёк бы. Без состояния в памяти, поэтому одинаков после рестарта. Выборка детерминированная
    (систематическая), не случайная: не защищает от закономерностей, привязанных к самому хэшу; при смене настроек
    срез меняется. Расход квоты: ~10% отсекаемых заказов оцениваются полной моделью"""
    h = hashlib.sha1(f"{order_id}|{_evidence_signature()}".encode("utf-8")).digest()
    return int.from_bytes(h[:4], "big") % TRIAGE_CONTROL_EVERY == 0


def _record_triage_evidence(miss: bool, good: bool, order_id: int | None = None, triage: int | None = None,
                            control: bool = False) -> None:
    try:
        with _triage_evidence_lock:
            ev = triage_evidence()   # при смене настроек начинается с нуля
            ev["n"] += 1
            ev["good"] += 1 if good else 0
            ev["miss"] += 1 if miss else 0
            if triage is not None:
                cut = int(triage) <= TRIAGE_REJECT_MAX
                ev["cut"] += 1 if cut else 0
                ev["extra"] += 1 if (not cut and not good) else 0
                if control and cut:
                    ev["ctl_cut"] += 1
                    ev["ctl_miss"] += 1 if miss else 0
            _save_evidence(ev)
            if order_id is not None and triage is not None:
                scores = _load_scores()
                scores[str(order_id)] = [int(triage), ev["sig"]]
                while len(scores) > TRIAGE_SCORES_MAX:
                    scores.pop(next(iter(scores)))
                kv_set(TRIAGE_SCORES_KEY, json.dumps(scores))
    except Exception:
        logging.exception("[TRIAGE] не удалось записать выборку сравнения")


def _load_scores() -> dict:
    try:
        data = json.loads(kv_get(TRIAGE_SCORES_KEY, "") or "{}")
        return data if isinstance(data, dict) else {}
    except (ValueError, TypeError):
        return {}


def record_triage_label(order_id: int, suitable: bool) -> str | None:
    """Решение владельца по заказу (suitable: «Взял» - True, «Не подходит» - False) как независимая разметка.
    Учитывается только если у заказа есть быстрая оценка, набранная при текущих настройках. Пропуск - подходящий
    заказ с быстрой оценкой <= TRIAGE_REJECT_MAX. Повторная отметка того же заказа не удваивает счётчики.
    Возвращает: 'good' / 'bad' / 'miss' либо None (оценки нет, устарела или уже учтён)."""
    try:
        with _triage_evidence_lock:
            scores = _load_scores()
            rec = scores.get(str(order_id))
            if not rec or rec[1] != _evidence_signature() or (len(rec) > 2 and rec[2]):
                return None
            ev = triage_evidence()
            if suitable:
                ev["lab_good"] += 1
                kind = "good"
                if int(rec[0]) <= TRIAGE_REJECT_MAX:
                    ev["lab_miss"] += 1
                    kind = "miss"
            else:
                ev["lab_bad"] += 1
                kind = "bad"
            _save_evidence(ev)
            scores[str(order_id)] = [rec[0], rec[1], 1]   # заказ размечен
            kv_set(TRIAGE_SCORES_KEY, json.dumps(scores))
            return kind
    except Exception:
        logging.exception("[TRIAGE] не удалось записать разметку")
        return None


def retarget_triage_label(order_id: int, old: str | None, new: str | None) -> str | None:
    """Пересчёт метки заказа после действия или отмены (Ф08): old/new - метки (jr.LABEL_GOOD/LABEL_BAD/None)
    по состоянию лида ДО и ПОСЛЕ. Снимает прежнюю метку (счётчики lab_good/lab_bad/lab_miss уменьшаются, не ниже 0)
    и ставит новую, так что ошибочный клик и его отмена в статистике не остаются. Если быстрая оценка заказа
    набрана при других настройках (отпечаток сменился), счётчики не трогаются - снимается лишь пометка заказа.
    Возвращает 'good' / 'bad' / 'miss' для поставленной метки, 'removed' - только сняли, None - ничего не изменилось."""
    if old == new:
        return None
    try:
        with _triage_evidence_lock:
            scores = _load_scores()
            rec = scores.get(str(order_id))
            if not rec:
                return None
            labeled = len(rec) > 2 and bool(rec[2])
            if rec[1] != _evidence_signature():
                if labeled:
                    scores[str(order_id)] = [rec[0], rec[1]]
                    kv_set(TRIAGE_SCORES_KEY, json.dumps(scores))
                return None
            if labeled and old is None:
                return None   # метка есть, а какая - неизвестно: счётчики не трогаем и не удваиваем
            ev = triage_evidence()
            cut = int(rec[0]) <= TRIAGE_REJECT_MAX
            if labeled:
                if old == jr.LABEL_GOOD:
                    ev["lab_good"] = max(0, ev["lab_good"] - 1)
                    ev["lab_miss"] = max(0, ev["lab_miss"] - (1 if cut else 0))
                else:
                    ev["lab_bad"] = max(0, ev["lab_bad"] - 1)
            kind = "removed" if labeled else None
            if new == jr.LABEL_GOOD:
                ev["lab_good"] += 1
                ev["lab_miss"] += 1 if cut else 0
                kind = "miss" if cut else "good"
            elif new == jr.LABEL_BAD:
                ev["lab_bad"] += 1
                kind = "bad"
            if kind is None:
                return None
            _save_evidence(ev)
            scores[str(order_id)] = [rec[0], rec[1], 1] if new else [rec[0], rec[1]]
            kv_set(TRIAGE_SCORES_KEY, json.dumps(scores))
            return kind
    except Exception:
        logging.exception("[TRIAGE] не удалось пересчитать разметку")
        return None


def triage_allowed() -> tuple[bool, str]:
    """Можно ли включать отсекающий триаж: (да/нет, пояснение)."""
    ev = triage_evidence()
    if ev.get("expired"):
        return False, (f"наблюдения устарели (пауза больше {TRIAGE_EVIDENCE_MAX_IDLE_DAYS} дн. или набору больше "
                       f"{TRIAGE_EVIDENCE_MAX_AGE_DAYS} дн.): набор начат заново")
    if ev["stale"]:
        return False, "статистика набрана при другой модели, промпте или порогах: набор начат заново"
    if ev["n"] < TRIAGE_MIN_SAMPLE or ev["good"] < TRIAGE_MIN_GOOD:
        return False, (f"мало данных shadow: оценок {ev['n']} из {TRIAGE_MIN_SAMPLE}, "
                       f"подходящих {ev['good']} из {TRIAGE_MIN_GOOD}")
    if ev["miss"]:
        return False, f"shadow показал пропуски подходящих: {ev['miss']} из {ev['good']}"
    if ev["lab_miss"]:
        return False, f"разметка владельца показала пропуски: {ev['lab_miss']} из {ev['lab_good']} подходящих"
    if ev["lab_good"] < TRIAGE_MIN_LABELED:
        return False, (f"нет независимой разметки: подходящих по решению владельца {ev['lab_good']} "
                       f"из {TRIAGE_MIN_LABELED} (совпадение с полной оценкой полноту не доказывает)")
    appr = triage_approval()
    if not appr["valid"]:
        return False, appr["why"]
    return True, (f"пропусков нет: {ev['n']} сравнений с полной оценкой, по разметке владельца "
                  f"{ev['lab_good']} подходящих и {ev['lab_bad']} отклонённых")


def _audit_state() -> dict:
    """Состояние выборки владельца для ТЕКУЩИХ настроек: issued - все выданные заказы (уникальные), pending - выданная
    и ещё не подтверждённая партия, reviewed - подтверждённые, suitable - из них отмеченные подходящими.
    При смене настроек (отпечаток) состояние начинается с нуля."""
    sig = _evidence_signature()
    try:
        data = json.loads(kv_get(TRIAGE_AUDIT_KEY, "") or "{}")
    except (ValueError, TypeError):
        data = {}
    if not isinstance(data, dict) or data.get("sig") != sig or _evidence_aged(data.get("started"), data.get("updated")):
        return {"sig": sig, "issued": [], "pending": [], "code": "", "reviewed": [], "suitable": []}
    out = {"sig": sig, "started": str(data.get("started") or ""), "updated": str(data.get("updated") or "")}
    for k in ("issued", "pending", "reviewed", "suitable"):
        v = data.get(k)
        out[k] = [int(x) for x in v] if isinstance(v, list) else []
    out["code"] = str(data.get("code") or "")
    return out


def _stamp(state: dict) -> None:
    """Метки давности набора подтверждений владельца (started - начало набора, updated - последняя запись)."""
    now = _now().isoformat()
    state["started"] = state.get("started") or now
    state["updated"] = now


def _batch_code(sig: str, ids: list[int]) -> str:
    return hashlib.sha1(f"{sig}|{','.join(map(str, sorted(ids)))}".encode("utf-8")).hexdigest()[:4]


def triage_approval() -> dict:
    """Подтверждение владельца для ТЕКУЩИХ настроек триажа (отпечаток): valid - можно, why - почему нет.
    Считаются только уникальные заказы, выданные ботом и подтверждённые по коду партии."""
    st = _audit_state()
    checked, suitable = len(set(st["reviewed"])), len(set(st["suitable"]))
    base = {"checked": checked, "suitable": suitable, "issued": len(set(st["issued"])), "pending": len(st["pending"])}
    if suitable:
        return {**base, "valid": False,
                "why": f"выборка владельца нашла подходящих среди отсекаемых: {suitable} из {checked}"}
    if checked < TRIAGE_AUDIT_MIN:
        return {**base, "valid": False,
                "why": f"нет подтверждения владельца для этих настроек: просмотрено {checked} из {TRIAGE_AUDIT_MIN} "
                       f"уникальных заказов выборки (/triage_audit, затем /triage_approve)"}
    return {**base, "valid": True, "why": ""}


def approve_triage(code: str, suitable_ids: list[int] | None = None) -> str:
    """Подтвердить просмотр ВЫДАННОЙ партии выборки: code - код партии из /triage_audit, suitable_ids - заказы партии,
    которые владелец счёл подходящими (остальные считаются просмотренными и неподходящими). Произвольные числа не
    принимаются: партия должна быть выдана ботом при текущих настройках, id - из этой партии. Что владелец действительно
    открыл заказы, бот проверить не может: подтверждение остаётся его ответственностью"""
    suitable_ids = [int(x) for x in (suitable_ids or [])]
    with _triage_evidence_lock:
        st = _audit_state()
        if not st["pending"]:
            return "Нет выданной партии для текущих настроек: сначала /triage_audit."
        if str(code).strip().lower() != st["code"]:
            return "Код партии не совпадает с выданным в /triage_audit: подтверждение не принято."
        bad = [x for x in suitable_ids if x not in st["pending"]]
        if bad:
            return f"Заказы не из выданной партии: {bad}. Подтверждение не принято."
        st["reviewed"] = sorted(set(st["reviewed"]) | set(st["pending"]))
        st["suitable"] = sorted(set(st["suitable"]) | set(suitable_ids))
        n = len(st["pending"])
        st["pending"], st["code"] = [], ""
        _stamp(st)
        kv_set(TRIAGE_AUDIT_KEY, json.dumps(st))
    ok, why = triage_allowed()
    ap = triage_approval()
    return (f"Партия принята: {n} заказов, подходящих {len(suitable_ids)}. Всего уникальных просмотрено "
            f"{ap['checked']} из {TRIAGE_AUDIT_MIN}, подходящих {ap['suitable']}. "
            + ("Остальные условия выполнены, отсечение разрешено (если запрошен on)." if ok
               else f"Отсечение пока не разрешено: {why}."))


def triage_audit_sample(k: int = 20) -> dict:
    """Партия случайной выборки заказов, которые триаж отсёк бы при текущих настройках (быстрая оценка <= порога).
    Выдаются только ещё не выданные заказы: повторный вызов до подтверждения возвращает ту же партию и не увеличивает
    число уникальных наблюдений. Выборка - из последних TRIAGE_SCORES_MAX заказов, у которых было сравнение с полной
    оценкой (не из истёкших без оценки). Полная оценка модели владельцу не показывается (чтобы не подсказывала решение)."""
    import random
    with _triage_evidence_lock:
        st = _audit_state()
        if st["pending"]:
            ids = list(st["pending"])
        else:
            seen = set(st["issued"])
            cand = [int(oid) for oid, rec in _load_scores().items()
                    if rec and rec[1] == st["sig"] and int(rec[0]) <= TRIAGE_REJECT_MAX and int(oid) not in seen
                    and freelance_get(int(oid))]
            random.SystemRandom().shuffle(cand)
            ids = cand[:k]
            if ids:
                st["pending"] = ids
                st["issued"] = sorted(seen | set(ids))
                st["code"] = _batch_code(st["sig"], ids)
                _stamp(st)
                kv_set(TRIAGE_AUDIT_KEY, json.dumps(st))
        code = st["code"] if st["pending"] else ""
    rows = []
    for oid in ids:
        row = freelance_get(oid)
        if row:
            rows.append({"order_id": oid, "title": row.get("title") or "", "url": row.get("url") or ""})
    return {"code": code, "rows": rows}


def triage_report() -> str:
    """Состояние триажа для /status: запрошенный и фактический режим, основания, счётчики, устаревание."""
    ev = triage_evidence()
    ok, why = triage_allowed()
    eff = effective_triage_mode()
    lines = [f"Триаж: запрошен {TRIAGE_MODE}, применяется {eff}"]
    if TRIAGE_MODE == "on" and eff != "on":
        lines.append(f"  отсечение заблокировано: {why}")
    elif TRIAGE_MODE != "on" and ok:
        lines.append("  условия отсечения выполнены, но режим on не запрошен")
    if ev["stale"]:
        lines.append("  прежняя статистика устарела (модель, промпт или пороги изменились): счёт начат заново")
    lines.append(f"  сравнений с полной оценкой {ev['n']}/{TRIAGE_MIN_SAMPLE}, подходящих {ev['good']}/"
                 f"{TRIAGE_MIN_GOOD}, пропусков {ev['miss']}")
    lines.append(f"  отсекаемых {ev['cut']}: подходящих среди них {ev['miss']}; пропущенных среди подходящих "
                 f"{ev['miss']} из {ev['good']}; лишних оценок (триаж пропустил, полная отклонила) {ev['extra']}")
    lines.append(f"  контрольный срез on: отсекаемых {ev['ctl_cut']}, пропусков {ev['ctl_miss']}")
    lines.append(f"  отметки владельца: подходящих {ev['lab_good']}/{TRIAGE_MIN_LABELED}, отклонённых "
                 f"{ev['lab_bad']}, пропусков {ev['lab_miss']}")
    appr = triage_approval()
    lines.append(f"  выборка отсекаемых: выдано уникальных {appr['issued']}, просмотрено {appr['checked']}/"
                 f"{TRIAGE_AUDIT_MIN}, подходящих {appr['suitable']}, ждёт подтверждения {appr['pending']}")
    return "\n".join(lines)


def effective_triage_mode() -> str:
    """Режим триажа в этом цикле: запрошенный on без проверки пропусков сводится к shadow."""
    if TRIAGE_MODE != "on":
        return TRIAGE_MODE
    ok, why = triage_allowed()
    if not ok:
        if not _triage_warned["on"]:
            _triage_warned["on"] = True
            logging.warning(f"[TRIAGE] FREELANCE_TRIAGE=on не применён, остаётся shadow: {why}")
        return "shadow"
    return "on"


def _log_triage_shadow(sc: ScoredOrder, triage: int, result: dict, control: bool = False) -> None:
    """Сравнение быстрой и полной оценки (режим shadow): по этим строкам решаем, включать ли триаж."""
    try:
        full = int(result["fit"])
    except (KeyError, TypeError, ValueError):
        return
    miss = triage <= TRIAGE_REJECT_MAX and full >= FREELANCE_MIN_FIT
    _record_triage_evidence(miss, full >= FREELANCE_MIN_FIT, sc.order.order_id, triage, control)
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
        price = pricing.price_order(ai, parse_budget(sc.order.price_text))
        # старые price_*/market_*/budget_* не должны пережить пересчёт (например, устаревший рынок)
        for key in [k for k in ai if k.startswith(("price_", "market_", "budget", "hours_", "hourly_"))]:
            del ai[key]
        ai.update(price)
        topic = f"{sc.order.title or ''} {sc.order.body or ''}"
        ai["experience"] = [{"id": e["id"], "title": e["title"], "client_ok": e["client_ok"]}
                            for e in profile.relevant_experience(ai.get("category"), topic)]
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
        f"до восстановления квоты, сейчас в очереди: {len(res.unscored)}."
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


def save_order(sc: ScoredOrder, status: str, message_id: int | None = None,
               delivery: str | None = None) -> None:
    """Запись заказа с тремя независимыми состояниями (journal). status - прежний язык цикла:
    pending (ждёт оценки или доставки), sent/taken/dismissed (карточка доставлена), rejected (оценён,
    не доставляется отдельной карточкой). delivery задаёт способ доставки явно (сводка)."""
    o = sc.order
    ai = sc.ai or {}
    states: dict = {}
    if sc.fit is not None:
        low = sc.fit < FREELANCE_MIN_FIT or bool(ai.get("triage"))
        states["tech"] = jr.ASSESSED
        states["verdict"] = jr.V_TRIAGE if ai.get("triage") else (jr.V_LOW if low else jr.V_OK)
        if status == "pending":
            states["delivery"] = jr.D_NONE if low else jr.D_PENDING
    elif status == "pending":
        states["tech"] = jr.QUEUED
        states["queue_reason"] = sc.queue_reason or (jr.Q_ERROR if sc.attempts else jr.Q_WAIT)
    if delivery:
        states["delivery"] = delivery
    origin = sources.source_of(o.order_id)
    freelance_save(
        o.order_id, o.site, ai_scorer.mask_contacts(o.title or ""), o.url,
        ai_scorer.mask_contacts(o.price_text or ""),
        o.published_at.isoformat() if o.published_at else None, sc.dedup, sc.fit,
        json.dumps(sc.ai, ensure_ascii=False) if sc.ai else "",
        status, sc.attempts, message_id,
        category=ai.get("category"),
        budget=_db_budget(o),
        hours=ai.get("hours") or None,
        origin=origin, title_key=jr.title_key(o.site, o.title),
        content_hash=jr.content_hash(o.title, o.body) if o.body else None,
        snippet=_full_snippet(o) if o.body else None,
        pub_unreliable=o.published_at is None or jr.pub_unreliable(o.site, origin), **states,
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


def _save_retry(sc: ScoredOrder, status: str, delivery: str | None = None) -> bool:
    for i in range(SAVE_RETRIES):
        try:
            save_order(sc, status, delivery=delivery)
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
    for sc in res.rejected:
        if _save_retry(sc, "rejected"):
            done_ids.add(sc.order.order_id)
        else:
            failed = True
    # показанные сводкой «ещё N» - оценены хорошо, доставлены строкой сводки; отвергнутыми AI не считаются
    for sc in skipped:
        if _save_retry(sc, "rejected", delivery=jr.D_DIGEST):
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
            try:   # счётчик вакансионных заказов - вместе с курсором: повторный скан того же окна не удваивает
                freelance_count_excluded(datetime.now(MSK).date().isoformat(), res.excluded)
            except Exception:
                logging.exception("[FREELANCE] не удалось записать счётчик отсеянных площадок")
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
    if ai.get("budget_status"):
        line = _budget_line(ai)
        if line:
            parts.append(line)
        return " · ".join(parts)
    budget = _num(ai.get("budget"))     # записи прежней формулы (до версии цены 4): бюджет подгонял цену
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


BUDGET_SHORT = {
    "ok": "покрывает полный объём",
    "below_full": "ниже расчёта: предложить меньший объём или этапы",
    "below_min": "ниже минимума: предложить диагностику или меньший объём, цену не снижать",
    "open_low": "«от N» ниже расчёта: уточнить верхнюю границу",
    "none": "не указан (это не плохой профиль)",
    "uncertain_currency": "валюта неизвестна, сравнить нельзя",
    "uncertain_fx": "нет надёжного курса, сравнение не сделано",
    "suspicious": "значение испорчено, не учтено",
}


def _budget_line(ai: dict) -> str:
    """Бюджет клиента отдельно от цены: статус сравнения, валюта, курс (источник и дата) для иностранной валюты."""
    status = str(ai.get("budget_status") or "")
    if status == "hourly":
        text = str(ai.get("budget_note") or "почасовая ставка")
    else:
        text = BUDGET_SHORT.get(status, "")
    if not text:
        return ""
    line = f"бюджет клиента: {text}"
    fx_info = ai.get("budget_fx")
    if isinstance(fx_info, dict) and _num(ai.get("budget_rub")) > 0:
        line += (f" ({ai.get('budget_currency') or ''} ≈ {_money(ai.get('budget_rub'))} ₽ по курсу "
                 f"{_num(fx_info.get('rate')):g}, {str(fx_info.get('source') or '')[:30]}, "
                 f"{str(fx_info.get('date') or '')[:10]})")
    return line


def _dimensions_lines(ai: dict) -> list[str]:
    """Раздельные оценки подробной карточки: профиль, выполнимость, ясность и достаточность данных, срочность, часы."""
    lines = []
    reason = str(ai.get("profile_reason") or "").strip()
    if reason:
        lines.append(f"🎯 Профиль: {html.escape(reason[:200])}")
    parts = []
    for label, key in (("выполнимость", "feasibility"), ("ясность ТЗ", "clarity"), ("данных", "sufficiency")):
        value = str(ai.get(key) or "")
        if value:
            parts.append(f"{label}: {html.escape(value[:20])}")
    confidence = str(ai.get("confidence") or "")
    if confidence:
        parts.append(f"уверенность: {html.escape(confidence[:20])}")
    urgency = str(ai.get("urgency") or "")
    if urgency and urgency != "не указана":
        parts.append(f"срок: {html.escape(urgency[:20])}")
    if parts:
        lines.append("🧭 " + " · ".join(parts))
    low, high = _fmt_hours(ai.get("hours_low")), _fmt_hours(ai.get("hours_high"))
    if low and high:
        lines.append(f"⏱ Работа: {low}" + (f"–{high}" if high != low else "") + " ч (часы работы, не календарный срок)")
    experience = ai.get("experience")
    if isinstance(experience, list):
        titles = [str(x.get("title") or "")[:90] for x in experience if isinstance(x, dict) and x.get("title")]
        lines.append("📚 Подтверждённый опыт: " + html.escape("; ".join(titles))
                     if titles else "📚 Подтверждённого опыта по теме нет")
    return lines


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
    status = ai.get("budget_status")
    if status in ("below_min", "below_full"):
        return "бюджет ниже минимума" if status == "below_min" else "бюджет ниже расчёта"
    if status in ("uncertain_currency", "uncertain_fx", "suspicious"):
        return "бюджет не сопоставлен"
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


def _hours_text(hours: float) -> str:
    if hours < 1:
        return f"{max(1, round(hours * 60))} мин"
    if hours < 48:
        return f"{hours:.0f} ч" if hours >= 10 else f"{hours:.1f}".rstrip("0").rstrip(".") + " ч"
    return f"{hours / 24:.0f} дн"


def _age_line(sc: ScoredOrder, now: datetime | None = None) -> str:
    """Предупреждение о возрасте: устаревший заказ не выдаём за свежий, актуальность проверяют перед откликом."""
    o = sc.order
    if not o.published_at or jr.pub_unreliable(o.site, sources.source_of(o.order_id)):
        return "🕰 дата публикации неизвестна - проверьте, что заказ ещё открыт"
    age = ((now or datetime.now(timezone.utc)) - o.published_at).total_seconds() / 3600
    if age > FREELANCE_FRESH_HOURS:
        return f"🕰 опубликован {_hours_text(age)} назад - не свежий, проверьте, что заказ ещё открыт"
    return ""


def _lag_line(sc: ScoredOrder) -> str:
    """Через сколько после публикации бот впервые увидел заказ (задержка источника, не очередь AI)."""
    if not sc.order.published_at or sc.first_seen is None or jr.pub_unreliable(sc.order.site, sources.source_of(sc.order.order_id)):
        return ""
    lag = (sc.first_seen - sc.order.published_at).total_seconds() / 3600
    return f"⏱ обнаружен через {_hours_text(max(0.0, lag))} после публикации" if lag * 60 >= 10 else ""


def format_order(sc: ScoredOrder, compact: bool = False, detailed: bool = False) -> str:
    """HTML-сообщение о заказе. По умолчанию короткая карточка; detailed=True добавляет
    вилку цены, расчёт, риск, компоненты и сложность (кнопка «Подробнее»).
    Итог не длиннее MAX_MESSAGE_LEN: подробный упрощается до короткого, короткий - до plain."""
    o, ai = sc.order, sc.ai
    e = html.escape
    fit = sc.fit
    fire = "🔥 " if fit is not None and fit >= 9 else ""
    t = o.published_at.astimezone(MSK).strftime("%H:%M") if o.published_at else "дата неизвестна"
    money = f"💰 {e((o.price_text or 'не указана')[:100])} · 🕒 {t}"
    lines = [f"{fire}💼 <b>{e((o.title or '')[:200])}</b>   [{e(site_name(o.site))}]"]
    age = _age_line(sc)
    if age:
        lines.append(age)
    if sc.stale_note:
        lines.append(f"⚠️ {e(sc.stale_note)}")
    if detailed:
        lag = _lag_line(sc)
        if lag:
            lines.append(lag)
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
            if ai.get("price_vague") and _num(ai.get("price_low")) > 0 and _num(ai.get("price_high")) > 0:
                # размытое ТЗ: ориентир с вилкой и условиями, а не точная цена
                line = (f"💵 Ориентир: {_money(ai.get('price_low'))}–{_money(ai.get('price_high'))} ₽ "
                        f"(зависит от уточнений)")
            else:
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
            minimum = _num(ai.get("price_min"))
            if minimum > 0 and ai.get("price_v") == pricing.PRICE_VERSION:
                lines.append(f"   минимально приемлемая цена: {_money(minimum)} ₽")
            conditions = ai.get("price_conditions")
            if ai.get("price_vague") and isinstance(conditions, list) and conditions:
                lines.append("   условия оценки: " + e("; ".join(str(c)[:80] for c in conditions[:5])))
            lines.extend(_dimensions_lines(ai))
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
            if isinstance(questions, list):
                asked = [str(q).strip()[:200] for q in questions if str(q).strip()]
                if asked:
                    shown = asked[:3] if detailed else asked[:1]
                    lines.append("❓ Уточнить: " + e(" · ".join(shown)))
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
    t = o.published_at.astimezone(MSK).strftime("%H:%M") if o.published_at else "дата неизвестна"
    lines = [
        f"💼 {(o.title or '')[:200]}   [{site_name(o.site)}]",
        f"💰 {(o.price_text or 'не указана')[:100]} · 🕒 {t}",
    ]
    fit = sc.fit
    lines.append(f"🤖 {fit}/10" if fit is not None else "🤖 без AI")
    lines.append(f"🔗 {o.url}")
    return "\n".join(lines)[:MAX_MESSAGE_LEN]
