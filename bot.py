"""Telegram-бот: мониторинг вакансий Junior/стажёр по разработке."""
import asyncio
import html
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    ForceReply,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    TelegramObject,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv

from config import (
    CHECK_INTERVAL_MINUTES,
    FREELANCE_INTERVAL_MINUTES,
    FREELANCE_HTML_SECONDS,
    FREELANCE_MESSAGE_TTL_HOURS,
    FREELANCE_MIN_FIT,
    FREELANCE_STAGE_SECONDS,
    MAX_VACANCY_AGE_DAYS,
)
from database import (
    cleanup_old,
    freelance_actuals_rows,
    freelance_deal_rows,
    freelance_expired_messages,
    freelance_claim_offers,
    freelance_claim_delivery,
    freelance_live_daily_metrics,
    freelance_confirm_offers,
    freelance_get,
    freelance_expire_live,
    freelance_finish_delivery,
    freelance_last_deal_price,
    freelance_lead_events,
    freelance_lead_can_undo,
    freelance_lead_get,
    freelance_leads_active,
    freelance_leads_closed,
    freelance_mark_reminded,
    freelance_recent,
    freelance_release_offers,
    freelance_reminders_due,
    freelance_set_actuals,
    freelance_set_deal_price,
    freelance_mark_card_deleted,
    freelance_set_status,
    freelance_day_summary,
    freelance_stats,
    freelance_unprocessed,
    get_recent_vacancies,
    init_db,
    kv_get,
    kv_set,
    reset_db,
    stats,
)
from freelance import ai_scorer, capacity, deal_wait, pricing, report, sources, yandex_budget
from freelance import journal as jr
from freelance import lead_view as lv
from freelance.draft import draft_messages
from freelance import manual as manual_orders
from freelance import leads
from freelance.alot_client import site_name
from freelance.pipeline import (
    AI_WORKERS,
    LAST_ID_KEY,
    MSK,
    TRIAGE_SHADOW_MAX_PER_DAY,
    approve_triage,
    triage_audit_sample,
    triage_report,
    collect_live,
    format_order,
    format_order_plain,
    mark_sent_status,
    live_fresh,
    live_rows,
    reassess_live,
    record_triage_label,
    report_live_ai,
    score_live,
    scored_from_row,
)
import memwatch
from health import monitor
from parser_manager import fetch_new_vacancies, mark_vacancies_sent

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)

bot = Bot(token=os.getenv("BOT_TOKEN"))
dp = Dispatcher()

MY_CHAT_ID = int(os.getenv("MY_CHAT_ID", "0"))

scheduler = AsyncIOScheduler()

subscribed = True
VACANCIES_SUBSCRIBED_KEY = "vacancies_subscribed"


def _set_subscribed(value: bool) -> None:
    """Включает/ставит на паузу проверку вакансий и запоминает выбор в БД,
    чтобы /stop переживал рестарт контейнера."""
    global subscribed
    subscribed = value
    try:
        kv_set(VACANCIES_SUBSCRIBED_KEY, "1" if value else "0")
    except Exception:
        logging.exception("[BOT] не удалось сохранить состояние подписки на вакансии")


def _load_subscribed() -> None:
    """Читает сохранённое состояние подписки на вакансии (по умолчанию включена)."""
    global subscribed
    try:
        subscribed = kv_get(VACANCIES_SUBSCRIBED_KEY, "1") != "0"
    except Exception:
        logging.exception("[BOT] не удалось прочитать состояние подписки, считаю включённой")
        subscribed = True

# По умолчанию показываем до 300 вакансий (практически всё, что есть в БД)
DEFAULT_LIST_LIMIT = 300
# Жёсткий лимит — защита от слишком длинных списков
MAX_LIST_LIMIT = 500
MSG_CHAR_LIMIT = 3500
# Источники health-монитора, относящиеся к фрилансу (в /status вакансий не показываем)
FREELANCE_HEALTH_SOURCES = {*sources.ALL_SOURCES, "alot_feed", "gemini", "memory"}

# Срок хранения вакансий в БД (дней)
CLEANUP_DAYS = 5


# ---------- Клавиатура ----------

def main_reply_kb():
    """Reply-клавиатура внизу экрана."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📋 Список"), KeyboardButton(text="📊 Статус")],
            [KeyboardButton(text="🔍 Проверить сейчас")],
            [KeyboardButton(text="💼 Фриланс")],
            [KeyboardButton(text="⏸ Пауза"), KeyboardButton(text="▶️ Возобновить")],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


# ---------- Форматирование даты ----------

def _human_date(iso_str: str) -> str:
    """Преобразует ISO-дату в '23.09 в 14:27'."""
    if not iso_str:
        return ""

    cleaned = iso_str.replace("+03:00", "").replace("Z", "").strip()

    formats = [
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
    ]

    for fmt in formats:
        try:
            dt = datetime.strptime(cleaned[:19], fmt)
            return dt.strftime("%d.%m в %H:%M")
        except ValueError:
            continue

    return iso_str


def _human_date_short(date_str: str) -> str:
    """Только дата: '2026-09-23' → '23.09'."""
    if not date_str:
        return ""
    try:
        dt = datetime.strptime(date_str[:10], "%Y-%m-%d")
        return dt.strftime("%d.%m")
    except ValueError:
        return date_str


# ---------- Разбивка длинных сообщений ----------

def _split_messages(text: str, limit: int = MSG_CHAR_LIMIT) -> list[str]:
    """Разбивает длинный текст на части по границам строк."""
    if len(text) <= limit:
        return [text]

    parts = []
    current = []

    for line in text.split("\n"):
        if sum(len(l) + 1 for l in current) + len(line) + 1 > limit:
            parts.append("\n".join(current))
            current = [line]
        else:
            current.append(line)

    if current:
        parts.append("\n".join(current))

    return parts


# ---------- Middleware ----------

class WhitelistMiddleware(BaseMiddleware):
    def __init__(self, allowed_user_id: int):
        self.allowed_user_id = allowed_user_id

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user and user.id == self.allowed_user_id:
            return await handler(event, data)
        logging.warning(
            f"Отклонён доступ: user_id={user.id if user else 'unknown'}"
        )
        return None


dp.message.middleware(WhitelistMiddleware(MY_CHAT_ID))
dp.callback_query.middleware(WhitelistMiddleware(MY_CHAT_ID))


# ---------- Проверка вакансий ----------

_check_lock = asyncio.Lock()


async def check_vacancies() -> bool:
    """Запускает парсеры в отдельном потоке и отправляет новые вакансии.

    Возвращает True, если проверка реально выполнена.
    Возвращает False, если проверка пропущена (уже идёт или подписка off).
    """
    if not subscribed:
        logging.info("[BOT] Подписка отключена, пропуск проверки")
        return False

    if _check_lock.locked():
        logging.info("[BOT] Проверка уже идёт, пропуск")
        return False

    async with _check_lock:
        logging.info("[BOT] Проверка новых вакансий...")

        try:
            # Запасной предохранитель: даже если что-то зависло мимо таймаутов
            # парсеров, блокировка проверки освободится
            new_vacancies = await asyncio.wait_for(
                asyncio.to_thread(fetch_new_vacancies), timeout=2400  # > суммы таймаутов парсеров (~2000 с); поток при этом не прерывается
            )
        except Exception:
            logging.exception("[BOT] Ошибка при получении вакансий")
            return True

        # Сообщения «парсер сломался / снова работает» — до вакансий,
        # и даже если новых вакансий нет (как раз тогда они и важны)
        for alert in monitor.pop_alerts():
            try:
                await bot.send_message(chat_id=MY_CHAT_ID, text=alert, parse_mode="HTML")
            except Exception:
                logging.exception("[BOT] Не удалось отправить health-оповещение")

        if not new_vacancies:
            logging.info("[BOT] Новых вакансий нет")
            return True

        logging.info(f"[BOT] Найдено новых: {len(new_vacancies)}")

        # Важно: собираем РЕАЛЬНО отправленные вакансии в отдельный список,
        # а не режем исходный по счётчику. Иначе при сбое на середине списка
        # упавшая вакансия помечается отправленной (и теряется), а успешные
        # после неё — не помечаются (и придут повторно).
        # Каждую вакансию записываем в историю сразу после отправки: перезапуск
        # или сбой записи посреди пачки не повторит уже доставленные.
        # Остаточный риск: процесс убит ровно между send_message и записью в БД
        # (или запись не удалась дважды) - такая вакансия придёт повторно.
        sent_ok: list = []
        mark_failed: list = []
        for v in new_vacancies:
            try:
                await bot.send_message(
                    chat_id=MY_CHAT_ID,
                    text=v.format_message(),
                    parse_mode="HTML",
                )
            except Exception:
                logging.exception(f"[BOT] Не удалось отправить {v.url}")
                continue
            sent_ok.append(v)
            if not await _mark_sent_with_retry(v):
                mark_failed.append(v)
            await asyncio.sleep(0.5)

        if mark_failed:
            logging.error(
                f"[BOT] Отправлено, но не записано в историю: {len(mark_failed)} "
                f"(могут прийти повторно)"
            )
        if sent_ok:
            logging.info(f"[BOT] Успешно отправлено: {len(sent_ok)}")
        if len(sent_ok) < len(new_vacancies):
            logging.warning(
                f"[BOT] Не отправлено: {len(new_vacancies) - len(sent_ok)} "
                f"(будут повторены в следующую проверку)"
            )

        return True


async def _mark_sent_with_retry(vacancy) -> bool:
    """Записывает одну отправленную вакансию в историю; две попытки. True - записано."""
    for attempt in (1, 2):
        try:
            await asyncio.to_thread(mark_vacancies_sent, [vacancy])
            return True
        except Exception:
            logging.exception(
                f"[BOT] запись отправленной вакансии {vacancy.url} не удалась (попытка {attempt}/2)"
            )
            await asyncio.sleep(0.5)
    return False


# ---------- Фриланс (alot.pro) ----------

_freelance_lock = asyncio.Lock()


def _freelance_subscribed_sync() -> bool:
    return kv_get("freelance_subscribed", "1") == "1"


async def _freelance_subscribed() -> bool:
    return await asyncio.to_thread(_freelance_subscribed_sync)


async def freelance_menu_kb() -> InlineKeyboardMarkup:
    toggle = "⏸ Пауза" if await _freelance_subscribed() else "▶️ Включить"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📥 Входящие", callback_data="fl_inbox")],
        [InlineKeyboardButton(text="Оценить свой заказ", callback_data="fl_add")],
        [InlineKeyboardButton(text="📋 Последние заказы", callback_data="fl_recent")],
        [InlineKeyboardButton(text="🔍 Проверить сейчас", callback_data="fl_check")],
        [
            InlineKeyboardButton(text=toggle, callback_data="fl_toggle"),
            InlineKeyboardButton(text="📊 Статус", callback_data="fl_status"),
        ],
        [InlineKeyboardButton(text="📊 Точность цен", callback_data="fl_accuracy")],
        [InlineKeyboardButton(text="🔌 Источники", callback_data="fl_src")],
    ])


def _kb(rows) -> InlineKeyboardMarkup:
    """Клавиатура из описания freelance.lead_view (Btn: либо callback_data, либо url)."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=b.text, url=b.url) if b.url else InlineKeyboardButton(text=b.text, callback_data=b.data)
         for b in row] for row in rows])


def _order_kb(order_id: int, url: str | None, taken: bool = False,
              detailed: bool = False, actions: bool = True) -> InlineKeyboardMarkup:
    """Кнопки СТАРОГО вида («Взял» / «Не подходит», после «Взял» - «В работе»): нужны обработчикам
    fl_take/fl_skip для сообщений, отправленных до Ф08. Новые карточки - lv.order_card_buttons."""
    if detailed:
        more = InlineKeyboardButton(text="🔼 Свернуть", callback_data=f"fl_less:{order_id}")
    else:
        more = InlineKeyboardButton(text="ℹ️ Подробнее", callback_data=f"fl_more:{order_id}")
    top = [more]
    if url:
        top.insert(0, InlineKeyboardButton(text="🔗 Открыть", url=url))
    rows = [top]
    if not actions:
        pass                         # закрытый заказ (dismissed/expired/rejected): без «Взял»
    elif taken:
        rows.append([InlineKeyboardButton(text="✅ В работе", callback_data=f"fl_noop:{order_id}")])
    else:
        rows.append([
            InlineKeyboardButton(text="✅ Взял", callback_data=f"fl_take:{order_id}"),
            InlineKeyboardButton(text="❌ Не подходит", callback_data=f"fl_skip:{order_id}"),
        ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _send_order(sc, still_fresh: Callable[[], bool]):
    """Отправляет один заказ, при отказе Telegram пробует упрощённые варианты.

    Возвращает message_id. TelegramBadRequest на всех вариантах пробрасывается
    (заказ уйдёт в pending), TelegramRetryAfter и сетевые ошибки - тоже.
    """
    o = sc.order
    variants = []
    try:
        variants.append((format_order(sc), "HTML", _kb(lv.order_card_buttons(o.order_id, o.url))))
    except Exception:
        logging.exception(f"[FREELANCE] ошибка форматирования {o.url}")
    variants.append((format_order_plain(sc), None, _kb(lv.order_card_buttons(o.order_id, o.url))))
    # последний шанс: без кнопки-ссылки (URL мог оказаться невалидным для Telegram)
    variants.append((format_order_plain(sc), None, _kb(lv.order_card_buttons(o.order_id, None))))
    last_exc: Exception | None = None
    for text, parse_mode, markup in variants:
        if not still_fresh():
            return None
        try:
            msg = await bot.send_message(
                chat_id=MY_CHAT_ID, text=text, parse_mode=parse_mode,
                reply_markup=markup, disable_web_page_preview=True,
            )
            return msg.message_id
        except TelegramBadRequest as e:
            logging.warning(f"[FREELANCE] Telegram отклонил формат заказа {o.url}: {e}")
            last_exc = e
    assert last_exc is not None
    raise last_exc


async def _set_order_status(order_id: int, status: str) -> None:
    """Статус заказа в БД; если записи ещё нет (сбой записи после отправки), статус
    запоминается в памяти, и finish_cycle запишет его вместо «sent».
    Решение владельца («Взял» / «Не подходит») заодно служит независимой разметкой для проверки триажа."""
    if status in ("taken", "dismissed"):
        # Ф08: старые кнопки идут тем же путём, что и новые (событие, отмена, пересчёт метки триажа)
        stage, reason = (jr.LEAD_AGREED, None) if status == "taken" else (jr.LEAD_LOST, jr.LOST_LEGACY)
        res = await asyncio.to_thread(leads.set_stage, order_id, stage, reason)
        if res.get("ok"):
            return
        await asyncio.to_thread(record_triage_label, order_id, status == "taken")   # записи ещё нет: прежний путь
    if await asyncio.to_thread(freelance_set_status, order_id, status):
        return
    if await asyncio.to_thread(mark_sent_status, order_id, status):
        return
    # запись могла появиться между двумя проверками
    await asyncio.to_thread(freelance_set_status, order_id, status)


_live_ai_tasks: dict[int, asyncio.Task] = {}
_live_ai_pump_lock = asyncio.Lock()
_live_delivery_lock = asyncio.Lock()
_live_delivery_acks: dict[int, tuple[str, int]] = {}
_live_health_lock = asyncio.Lock()
_live_flood_until = 0.0


async def _collect_freelance(source: str, manual: bool = False):
    if manual or await _freelance_subscribed():
        return await asyncio.to_thread(collect_live, source)
    return None


async def check_freelance(manual: bool = False) -> dict | None:
    """Ручная проверка запускает сбор, затем подбирает оценку и доставку очереди."""
    if _freelance_lock.locked():
        return None
    async with _freelance_lock:
        results = await asyncio.gather(*(_collect_freelance(n, manual) for n in sources.ALL_SOURCES))
        await _pump_freelance_ai(force=manual)
        await _deliver_freelance(force=manual)
        passes = [r for r in results if r is not None and not r.busy]
        return {"scanned": sum(r.raw_count for r in passes),
                "new": sum(r.intake.get("new", 0) for r in passes),
                "errors": [r.error for r in passes if r.error]}


async def _score_freelance_row(row: dict, force: bool) -> None:
    oid = row["order_id"]
    try:
        res = await asyncio.to_thread(score_live, row)
        if force and res.ai_ok:
            asyncio.create_task(_deliver_freelance(force=True))
        async with _live_health_lock:
            await asyncio.to_thread(report_live_ai, res)
    except Exception:
        logging.exception("[FREELANCE] не удалось обработать заказ %s", oid)
    finally:
        _live_ai_tasks.pop(oid, None)


async def _pump_freelance_ai(force: bool = False) -> None:
    async with _live_ai_pump_lock:
        slots = AI_WORKERS - len(_live_ai_tasks)
        if slots <= 0:
            return
        only_manual = not force and not await _freelance_subscribed()
        rows = await asyncio.to_thread(live_rows, "score", slots, only_manual, tuple(_live_ai_tasks))
        for row in rows:
            _live_ai_tasks[row["order_id"]] = asyncio.create_task(_score_freelance_row(row, force))


async def _confirm_live_card(oid: int, token: str, message_id: int) -> None:
    try:
        saved = await asyncio.to_thread(freelance_finish_delivery, oid, token, message_id)
    except Exception:
        logging.exception("[FREELANCE] Telegram подтвердил карточку %s, запись повторится", oid)
        return
    if not saved:
        logging.error("[FREELANCE] Telegram подтвердил карточку %s, попытка не найдена в журнале", oid)
    _live_delivery_acks.pop(oid, None)


async def _deliver_freelance(force: bool = False) -> None:
    global _live_flood_until
    if _live_delivery_lock.locked():
        return
    async with _live_delivery_lock:
        for oid, (token, message_id) in tuple(_live_delivery_acks.items()):
            await _confirm_live_card(oid, token, message_id)
        if asyncio.get_running_loop().time() < _live_flood_until:
            return
        only_manual = not force and not await _freelance_subscribed()
        rows = await asyncio.to_thread(live_rows, "deliver", 8, only_manual, tuple(_live_delivery_acks))
        for row in rows:
            oid = row["order_id"]
            row = await asyncio.to_thread(freelance_claim_delivery, oid)
            if row is None:
                continue
            token = row["delivery_token"]
            sc = scored_from_row(row)
            if sc.fit is None or sc.fit < FREELANCE_MIN_FIT or (sc.ai or {}).get("triage"):
                await asyncio.to_thread(freelance_finish_delivery, oid, token, None)
                continue
            origin = sources.source_of(oid)
            allowed = origin == "manual" or await asyncio.to_thread(sources.is_enabled, origin)
            if not allowed:
                await asyncio.to_thread(freelance_finish_delivery, oid, token, None)
                continue
            try:
                async with asyncio.timeout(25):
                    message_id = await _send_order(sc, lambda: live_fresh(row, allow_manual=True))
            except TelegramRetryAfter as e:
                delay = max(1, int(e.retry_after))
                _live_flood_until = asyncio.get_running_loop().time() + delay
                await asyncio.to_thread(freelance_finish_delivery, oid, token, None, delay)
                return
            except Exception:
                logging.exception("[FREELANCE] Не удалось отправить заказ %s", oid)
                await asyncio.to_thread(freelance_finish_delivery, oid, token, None)
                continue
            if message_id is not None:
                _live_delivery_acks[oid] = (token, message_id)
                await _confirm_live_card(oid, token, message_id)
            else:
                await asyncio.to_thread(freelance_finish_delivery, oid, token, None)


async def _maintain_freelance() -> None:
    await asyncio.to_thread(freelance_expire_live)
    for alert in monitor.pop_alerts():
        try:
            async with asyncio.timeout(10):
                await bot.send_message(chat_id=MY_CHAT_ID, text=alert, parse_mode="HTML")
        except Exception:
            logging.exception("[BOT] Не удалось отправить health-оповещение")
    if await _freelance_subscribed() and not _live_ai_tasks:
        res = await asyncio.to_thread(reassess_live)
        for message in res.reassess_notes:
            await bot.send_message(chat_id=MY_CHAT_ID, text=message, parse_mode="HTML",
                                   disable_web_page_preview=True)
    await asyncio.to_thread(memwatch.trim)


async def _refresh_freelance_ai_catalog() -> None:
    await asyncio.to_thread(ai_scorer.refresh_live_catalog)


def _ai_state_text(st: dict) -> str:
    """Строка статуса AI: активная Gemini-модель, запасные, провайдеры, грант Яндекса, последняя оценка."""
    if not st["enabled"]:
        return "выключен (нет ключей), карточки ждут полной оценки"
    def fmt_sec(sec: int) -> str:
        return f"{sec // 3600} ч {sec % 3600 // 60} мин" if sec >= 3600 else f"{sec} с"

    providers = st.get("providers", {})
    lines = []
    for name in st.get("chain", ["gemini"]):
        if name == "gemini":
            if not st["gemini"]:
                continue
            models = st["models"]
            if models:
                line = f"Gemini: {html.escape(models[0])} (запасные: {len(models) - 1})"
            else:
                line = "Gemini: все модели недоступны"
            if st["paused"]:
                paused = ", ".join(f"{html.escape(m)} {fmt_sec(sec)}" for m, sec in st["paused"].items())
                line += f"; на паузе (лимит): {paused}"
        else:
            p = providers.get(name)
            if not p or not p["has_key"]:
                continue
            if p["dead"]:
                reason = p.get("paid_reason") or "платный вызов"
                line = f"{name}: выключен до рестарта ({html.escape(reason)})"
            elif p["blocked_sec"]:
                line = f"{name}: ключ отклонён, пауза {fmt_sec(p['blocked_sec'])}"
            else:
                line = f"{name}: {len(p['models'])} мод., сегодня {p['today']}/{p['limit']}"
                if p["paused"]:
                    paused = ", ".join(f"{html.escape(m)} {fmt_sec(sec)}" for m, sec in p["paused"].items())
                    line += f"; на паузе: {paused}"
                if p["disabled"]:
                    line += f"; недоступны: {html.escape(', '.join(p['disabled']))}"
            if name == "yandex":
                paid = p.get("paid")
                if paid and paid.get("known"):
                    line += (f" · учтено за день {paid['day_rub']:.2f}/15 ₽, за месяц {paid['month_rub']:.2f}/500 ₽, "
                             f"за пилот {paid['total_rub']:.2f}/100 ₽ (в резерве {paid['held_rub']:.2f} ₽)")
                    if paid.get("reason"):
                        line += f" · {html.escape(paid['reason'])}"
                    elif not paid.get("active"):
                        line += " · пилот вне срока"
                else:
                    line += " · платный пилот выключен" if not paid else " · учёт недоступен"
        lines.append(line)
    text = "\n  • ".join([""] + lines) if lines else "нет активных провайдеров"
    last = st["last_ok"]
    if last:
        try:
            at = datetime.fromisoformat(last["at"]).astimezone(MSK).strftime("%d.%m %H:%M")
        except ValueError:
            at = last["at"]
        text += f"\nПоследняя оценка: {at} ({html.escape(last['model'])})"
    else:
        text += "\nПоследняя оценка: ещё не было"
    for name, reason in (st.get("key_errors") or {}).items():
        text += f"\n🔴 Ошибка настройки ({html.escape(name)}): {html.escape(reason)}"
    return text


def _freelance_status_text() -> str:
    """Собирает статус (синхронно, с чтением БД) — вызывать через to_thread."""
    st = freelance_stats()
    last_id = kv_get(LAST_ID_KEY, "—")
    last_raw = sum(int(kv_get(f"freelance_raw_{n}", "0") or 0)
                   for n in sources.ALL_SOURCES if sources.is_enabled(n))
    last_ok = kv_get("freelance_last_ok")
    if last_ok:
        try:
            last_ok = datetime.fromisoformat(last_ok).astimezone(MSK).strftime("%d.%m %H:%M")
        except ValueError:
            pass
    ai_state = _ai_state_text(ai_scorer.status())
    lines = [
        "📊 <b>Фриланс: статус</b>\n",
        f"Подписка: {'включена' if _freelance_subscribed_sync() else 'на паузе'}",
        f"last_id: <code>{last_id}</code>",
        f"Заказов просмотрено в последних обходах: {last_raw}",
        f"Увидено в журнале за сутки: {st['seen_24h']}",
        f"Отправлено за сутки: {st['sent_24h']}",
        f"Последняя успешная проверка: {last_ok or 'ещё не было'}",
        f"🤖 AI: {ai_state}",
        f"Порог fit: {FREELANCE_MIN_FIT}, интервал: {FREELANCE_INTERVAL_MINUTES} мин",
    ]
    try:   # ёмкость AI после отключения Yandex и состояние отсекающего триажа
        fc = capacity.forecast(ai_scorer.status(), capacity.demand_per_day(7) or 0.0,
                               TRIAGE_SHADOW_MAX_PER_DAY, datetime.now(MSK).date(), yandex_budget.until(),
                               overhead=capacity.measure_overhead(7))
        cover = f", покрытие {fc['coverage'] * 100:.0f}%" if fc["coverage"] is not None else ""
        lines.append(f"📉 Без Yandex ({fc['yandex_until']}): ≈{fc['net_without_yandex']:.0f} оценок/сутки "
                     f"при спросе ≈{fc['demand_per_day']:.0f}{cover}")
        lines.append(html.escape(triage_report()))
    except Exception:
        logging.exception("[FREELANCE] не удалось посчитать ёмкость для статуса")
    for name, title, on, forced in sources.states():
        if not on:
            lines.append(f"⚪ {title}: выключен" + (" (в настройках сервера)" if forced else ""))
            continue
        streak = monitor.streak.get(name, 0)
        if streak:
            reason = html.escape(monitor.last_reason.get(name, ""))
            lines.append(f"🔴 {title}: неудачных проверок подряд: {streak} ({reason})")
        else:
            lines.append(f"🟢 {title}: работает")
    if sources.is_enabled(sources.ALOT) and monitor.streak.get("alot_feed", 0) >= monitor.threshold:
        lines.append(f"🔴 Выдача alot.pro отстаёт: {html.escape(monitor.last_reason.get('alot_feed', ''))}")
    lines.append("\n" + report.format_funnel(report.load_rows(1), report.load_excluded(1), 1))
    return "\n".join(lines)


@dp.message(F.text == "💼 Фриланс")
async def btn_freelance(message: Message):
    await message.answer("💼 <b>Фриланс</b>", parse_mode="HTML",
                         reply_markup=await freelance_menu_kb())


@dp.callback_query(F.data == "fl_recent")
async def cb_fl_recent(call: CallbackQuery):
    await call.answer()
    try:
        orders = await asyncio.to_thread(freelance_recent, 10)
        if not orders:
            await call.message.answer("📭 Отправленных заказов пока нет.")
            return
        lines = ["📋 <b>Последние заказы</b>\n"]
        for o in orders:
            fit = f"{o['fit']}/10 · " if o["fit"] is not None else ""
            lines.append(
                f'• {fit}<a href="{html.escape(o["url"] or "")}">'
                f'{html.escape((o["title"] or "")[:90])}</a> '
                f'[{html.escape(site_name(o["site"] or ""))}] {html.escape(o["price_text"] or "")}'
            )
        for chunk in _split_messages("\n".join(lines)):
            await call.message.answer(chunk, parse_mode="HTML", disable_web_page_preview=True)
    except Exception:
        logging.exception("[FREELANCE] fl_recent")
        await call.message.answer("⚠️ Не удалось получить список заказов.")


@dp.callback_query(F.data == "fl_check")
async def cb_fl_check(call: CallbackQuery):
    await call.answer("Проверяю…")
    try:
        r = await check_freelance(manual=True)
        if r is None:
            text = "⏳ Проверка уже идёт."
        else:
            text = f"✅ Сбор завершён. В очередь оценки добавлено: {r['new']}. Подходящие заказы придут после оценки."
            if r["errors"]:
                text += "\n⚠️ " + html.escape(", ".join(r["errors"]))
    except Exception:
        logging.exception("[FREELANCE] fl_check")
        text = "⚠️ Проверка завершилась ошибкой, подробности в логе."
    await call.message.answer(text, parse_mode="HTML")


def _order_id_from(data: str) -> int | None:
    try:
        return int(data.split(":", 1)[1])
    except (IndexError, ValueError):
        return None


# ожидания цены сделки после «Взял»: (chat_id, id вопроса) -> (order_id, время)
_pending_deals: deal_wait.Pending = {}


def _deal_answer_filter(message: Message):
    """Ответ с ценой сделки (или /skip) на наш вопрос. Прочие сообщения и команды не трогаем."""
    if not message.text or not _pending_deals:
        return False
    text = message.text.strip()
    is_skip = text.split("@")[0].lower() == "/skip"
    if not is_skip and deal_wait.parse_deal(text) is None:
        return False
    reply_id = message.reply_to_message.message_id if message.reply_to_message else None
    key = deal_wait.find(_pending_deals, message.chat.id, reply_id)
    if key:
        return {"deal_key": key}
    if deal_wait.ambiguous(_pending_deals, message.chat.id, reply_id):
        return {"deal_key": None}  # несколько вопросов: просим reply
    return False


@dp.message(F.text, _deal_answer_filter)
async def msg_deal_price(message: Message, deal_key: tuple[int, int] | None):
    if deal_key is None:
        await message.answer("Ответьте (reply) на вопрос о нужном заказе.")
        return
    pending = _pending_deals.pop(deal_key, None)
    if pending is None:
        return
    order_id = pending[0]
    text = message.text.strip()
    if text.startswith("/"):
        # /skip: прежняя цена (обнулённая отменой/шагом назад) возвращается, если новой ещё нет
        try:
            prev = await asyncio.to_thread(freelance_last_deal_price, order_id)
            row = await asyncio.to_thread(freelance_get, order_id) or {}
            if prev and not row.get("deal_price") and row.get("lead") in jr.LEAD_PRICE_STAGES:
                if await asyncio.to_thread(freelance_set_deal_price, order_id, prev, require_price_stage=True):
                    await message.answer(f"Ок, оставил прежнюю цену: {lv.money(prev)}.")
                    return
        except Exception:
            logging.exception("[FREELANCE] deal_price skip")
        await message.answer("Ок, цену не записываю.")
        return
    try:
        ok = await asyncio.to_thread(freelance_set_deal_price, order_id, deal_wait.parse_deal(text), require_price_stage=True)
        await message.answer("✅ Записано." if ok else "⚠️ Заказ не найден или этап сделки изменился. Цену не записал.")
    except Exception:
        logging.exception("[FREELANCE] deal_price")
        await message.answer("⚠️ Не удалось записать цену, подробности в логе.")


def _accuracy_text() -> str:
    rows = []
    for r in freelance_deal_rows():
        try:
            ai = json.loads(r["ai_json"] or "{}")
        except ValueError:
            ai = {}
        rows.append({"category": r["category"], "deal_price": r["deal_price"],
                     "price_mid": ai.get("price_mid") if isinstance(ai, dict) else 0})
    rep = pricing.accuracy_report(rows)
    calibration = pricing.hours_calibration(freelance_actuals_rows())
    if not rep["total"]:
        head = ("📊 <b>Точность цен</b>\n\nСделок с ценой и расчётом пока нет. "
                "Нажимайте «Взял» и отвечайте, за сколько договорились.")
        return "\n".join([head] + _calibration_lines(calibration))     # часы по фактам не зависят от цен сделок
    lines = [f"📊 <b>Точность цен</b>\nСделок с расчётом: {rep['total']}\n"]
    for cat, c in sorted(rep["by_category"].items(), key=lambda kv: -kv[1]["n"]):
        direction = "завышаем" if c["bias"] > 0 else "занижаем"
        lines.append(f"• {html.escape(cat)}: {c['n']} шт., ошибка {c['mape']:.0f}% "
                     f"({direction} в среднем на {abs(c['bias']):.0f}%)")
    return "\n".join(lines + _calibration_lines(calibration))


def _calibration_lines(calibration: dict) -> list[str]:
    """Строки калибровки часов по фактическим данным (/actual): по категории, где точек не меньше порога."""
    if not calibration:
        return ["", "⏱ Часы: фактических данных пока мало (нужно не меньше "
                    f"{pricing.MIN_POINTS} заказов категории; запись: /actual номер часы [расходы])."]
    lines = ["", "⏱ Часы по фактам:"]
    for cat, c in sorted(calibration.items(), key=lambda kv: -kv[1]["n"]):
        word = "занижаем" if c["ratio"] > 1 else "завышаем"
        lines.append(f"• {html.escape(cat)}: {c['n']} шт., факт/оценка {c['ratio']:.2f} ({word})")
    return lines


@dp.message(Command("actual"))
async def cmd_actual(message: Message):
    """/actual <номер заказа> <часы> [расходы, ₽] - фактические часы и расходы по заказу (для калибровки оценок)."""
    parts = (message.text or "").split()[1:]
    try:
        order_id = int(parts[0])
        hours = float(parts[1].replace(",", "."))
        expenses = float(parts[2].replace(",", ".")) if len(parts) > 2 else None
    except (IndexError, ValueError):
        await message.answer("Формат: /actual номер_заказа часы [расходы_₽]\nНапример: /actual 12345 6.5 800")
        return
    try:
        ok = await asyncio.to_thread(freelance_set_actuals, order_id, hours, expenses)
    except Exception:
        logging.exception("[FREELANCE] actual")
        await message.answer("⚠️ Не удалось записать, подробности в логе.")
        return
    await message.answer("✅ Записано." if ok else "⚠️ Заказ не найден или значения неверны (нужны неотрицательные числа).")


@dp.callback_query(F.data == "fl_accuracy")
async def cb_fl_accuracy(call: CallbackQuery):
    await call.answer()
    try:
        text = await asyncio.to_thread(_accuracy_text)
        await call.message.answer(text, parse_mode="HTML")
    except Exception:
        logging.exception("[FREELANCE] fl_accuracy")
        await call.message.answer("⚠️ Не удалось собрать статистику.")


@dp.callback_query(F.data.startswith("fl_take:"))
async def cb_fl_take(call: CallbackQuery):
    try:
        order_id = _order_id_from(call.data)
        if order_id is None:
            await call.answer("Некорректная кнопка")
            return
        await _set_order_status(order_id, "taken")
        await call.answer("Отмечено: в работе")
        # url берём из кнопки «Открыть» исходного сообщения
        url = None
        detailed = False   # карточка развёрнута, если на ней кнопка «Свернуть»
        markup = call.message.reply_markup if call.message else None
        for row in (markup.inline_keyboard if markup else []):
            for btn in row:
                if btn.url:
                    url = btn.url
                if (btn.callback_data or "").startswith("fl_less:"):
                    detailed = True
        await call.message.edit_reply_markup(
            reply_markup=_order_kb(order_id, url, taken=True, detailed=detailed))
        # спрашиваем итоговую цену (для ставки и точности оценок)
        deal_wait.purge(_pending_deals)
        first = ((call.message.text or "").splitlines() or [""])[0]
        title = html.escape(first.replace("🔥", "").replace("💼", "").strip()[:60])
        question = await call.message.answer(
            f"💰 За сколько договорились: {title}?\nОтветьте числом в ₽ или /skip",
            parse_mode="HTML",
            reply_markup=ForceReply(input_field_placeholder="Цена в рублях"),
        )
        deal_wait.add(_pending_deals, call.message.chat.id, question.message_id, order_id)
    except Exception:
        logging.exception("[FREELANCE] fl_take")
        try:
            await call.answer("Ошибка, см. лог")
        except Exception:
            pass


@dp.callback_query(F.data.startswith("fl_skip:"))
async def cb_fl_skip(call: CallbackQuery):
    try:
        order_id = _order_id_from(call.data)
        if order_id is None:
            await call.answer("Некорректная кнопка")
            return
        await _set_order_status(order_id, "dismissed")
        deal_wait.drop_order(_pending_deals, order_id)
        await call.answer("Убрано")
        try:
            await call.message.delete()
        except TelegramBadRequest:
            # сообщение слишком старое для удаления: хотя бы уберём кнопки
            await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logging.exception("[FREELANCE] fl_skip")
        try:
            await call.answer("Ошибка, см. лог")
        except Exception:
            pass


async def _render_order_card(call: CallbackQuery, order_id: int, detailed: bool,
                             notice: str | None = None) -> None:
    """Перерисовывает сообщение с заказом из БД: текст и кнопки по текущему этапу лида.
    notice - ответ на нажатие кнопки (одно нажатие - один ответ)."""
    row = await asyncio.to_thread(freelance_get, order_id)
    if row is None:
        await call.answer("Заказ не найден", show_alert=False)
        return
    sc = scored_from_row(row)
    text = format_order(sc, detailed=detailed)
    # plain-формат (откат при слишком длинном тексте) - без HTML-разметки
    parse_mode = None if text == format_order_plain(sc) else "HTML"
    lead = row.get("lead")
    actions = row.get("status") in ("pending", "sent", "taken") or lead in jr.LEAD_ACTIVE
    kb = _kb(lv.order_card_buttons(order_id, sc.order.url or None, lead=lead, detailed=detailed, actions=actions))
    await call.answer(notice)
    try:
        await call.message.edit_text(text, parse_mode=parse_mode, reply_markup=kb,
                                     disable_web_page_preview=True)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise


async def _toggle_order_detail(call: CallbackQuery, detailed: bool):
    """Разворачивает/сворачивает карточку заказа (заказ читаем из БД)."""
    try:
        order_id = _order_id_from(call.data)
        if order_id is None:
            await call.answer("Некорректная кнопка")
            return
        await _render_order_card(call, order_id, detailed)
    except Exception:
        logging.exception("[FREELANCE] fl_more/fl_less")
        try:
            await call.answer("Ошибка, см. лог")
        except Exception:
            pass


@dp.callback_query(F.data.startswith("fl_more:"))
async def cb_fl_more(call: CallbackQuery):
    await _toggle_order_detail(call, True)


@dp.callback_query(F.data.startswith("fl_less:"))
async def cb_fl_less(call: CallbackQuery):
    await _toggle_order_detail(call, False)


@dp.callback_query(F.data.startswith("fl_noop:"))
async def cb_fl_noop(call: CallbackQuery):
    await call.answer("Заказ уже в работе")

# ---------- Ф08: этапы сделки, карточка лида, «Входящие», напоминания ----------

NOT_SAVED_YET = "Заказ ещё не записан в базу, повторите через минуту"


def _is_detailed(call: CallbackQuery) -> bool:
    """Карточка заказа развёрнута, если на ней кнопка «Свернуть»."""
    markup = call.message.reply_markup if call.message else None
    return any((b.callback_data or "").startswith("fl_less:")
               for row in (markup.inline_keyboard if markup else []) for b in row)


async def _answer_error(call: CallbackQuery, where: str) -> None:
    logging.exception(f"[FREELANCE] {where}")
    try:
        await call.answer("Ошибка, см. лог")
    except Exception:
        pass


async def _order_stage_action(call: CallbackQuery, stage: str, notice: str) -> None:
    """«Сохранить» / «Откликнулся» на карточке заказа: этап, событие, метка триажа; карточка перерисовывается."""
    try:
        order_id = _order_id_from(call.data)
        if order_id is None:
            await call.answer("Некорректная кнопка")
            return
        res = await asyncio.to_thread(leads.set_stage, order_id, stage)
        if not res.get("ok"):
            await call.answer(NOT_SAVED_YET, show_alert=True)
            return
        if res.get("changed"):
            deal_wait.drop_order(_pending_deals, order_id)
        await _render_order_card(call, order_id, _is_detailed(call), notice)
    except Exception:
        await _answer_error(call, f"fl_{stage}")


@dp.callback_query(F.data.startswith("fl_save:"))
async def cb_fl_save(call: CallbackQuery):
    await _order_stage_action(call, jr.LEAD_SAVED, "Сохранено во «Входящих»")


@dp.callback_query(F.data.startswith("fl_apply:"))
async def cb_fl_apply(call: CallbackQuery):
    await _order_stage_action(call, jr.LEAD_APPLIED, "Отмечено: откликнулись")


@dp.callback_query(F.data.startswith("fl_np:"))
async def cb_fl_not_mine(call: CallbackQuery):
    """«Не подходит» на новой карточке: не удаляет её, а сворачивает в одну строку с «↩️ Вернуть»."""
    try:
        order_id = _order_id_from(call.data)
        if order_id is None:
            await call.answer("Некорректная кнопка")
            return
        res = await asyncio.to_thread(leads.set_stage, order_id, jr.LEAD_LOST, jr.LOST_LEGACY)
        if not res.get("ok"):
            await call.answer(NOT_SAVED_YET, show_alert=True)
            return
        deal_wait.drop_order(_pending_deals, order_id)
        row = await asyncio.to_thread(freelance_get, order_id) or {"order_id": order_id}
        await call.answer("Убрано")
        try:
            await call.message.edit_text(lv.collapsed_text(row), parse_mode="HTML",
                                         reply_markup=_kb(lv.collapsed_buttons(order_id)))
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e):
                raise
    except Exception:
        await _answer_error(call, "fl_np")


@dp.callback_query(F.data.startswith("fl_back:"))
async def cb_fl_back(call: CallbackQuery):
    """«↩️ Вернуть» на свёрнутой карточке: отменяет «Не подходит» и возвращает карточку заказа."""
    try:
        order_id = _order_id_from(call.data)
        if order_id is None:
            await call.answer("Некорректная кнопка")
            return
        last = (await asyncio.to_thread(freelance_lead_events, order_id, 1) or [None])[0]
        if last and last["kind"] == "stage" and last["to_lead"] == jr.LEAD_LOST and not last["undone"]:
            await asyncio.to_thread(leads.undo, order_id)
            notice = "Возвращено"
        else:
            notice = "Состояние уже изменено, показываю текущее"
        await _render_order_card(call, order_id, False, notice)
    except Exception:
        await _answer_error(call, "fl_back")



def _lead_card_data(order_id: int) -> tuple[str, InlineKeyboardMarkup] | None:
    """Текст и кнопки карточки лида ТОЛЬКО из БД (после TTL и рестарта работает так же). None - заказа нет."""
    lead = freelance_lead_get(order_id)
    if lead is None:
        return None
    events = freelance_lead_events(order_id, lv.EVENT_LIMIT)
    text = lv.lead_card_text(lead, events, freelance_last_deal_price(order_id))
    note = manual_orders.latest_note(order_id)
    if note:
        text += "\nПоследний ручной ввод: " + html.escape(note[:200])
    return text, _kb(lv.lead_card_buttons(lead, events, undo_available=freelance_lead_can_undo(order_id)))


async def _show_lead_card(target: Message | CallbackQuery, order_id: int, edit: bool) -> bool:
    """Показывает карточку лида: edit=True - правит сообщение кнопки, иначе новое сообщение."""
    data = await asyncio.to_thread(_lead_card_data, order_id)
    msg = target.message if isinstance(target, CallbackQuery) else target
    if data is None:
        if isinstance(target, CallbackQuery):
            await target.answer("Заказ не найден")
        else:
            await msg.answer("Заказ не найден.")
        return False
    text, kb = data
    if edit:
        try:
            await msg.edit_text(text, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e):
                raise
    else:
        await msg.answer(text, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)
    return True


def _ids_from(data: str) -> tuple[int, str] | None:
    """'fl_xx:<id>:<значение>' -> (id, значение); иначе None."""
    chunks = data.split(":", 2)
    try:
        return int(chunks[1]), chunks[2]
    except (IndexError, ValueError):
        return None


async def _ask_deal_price(message: Message, order_id: int, title: str) -> None:
    """Вопрос «За сколько договорились?» после перехода в «Договорились»; ответ ловит msg_deal_price."""
    deal_wait.purge(_pending_deals)
    prev = await asyncio.to_thread(freelance_last_deal_price, order_id)
    hint = f" (было {lv.money(prev)}, /skip — оставить прежнюю)" if prev else " или /skip"
    question = await message.answer(
        f"💰 За сколько договорились: {html.escape(title[:60])}?\nОтветьте числом в ₽{hint}",
        parse_mode="HTML", reply_markup=ForceReply(input_field_placeholder="Цена в рублях"))
    deal_wait.add(_pending_deals, message.chat.id, question.message_id, order_id)


@dp.message(F.text.regexp(r"^/lead_(\d+)(@\w+)?\s*$").as_("lead_match"))
async def cmd_lead(message: Message, lead_match):
    """/lead_<id> - карточка лида из БД."""
    try:
        await _show_lead_card(message, int(lead_match.group(1)), edit=False)
    except Exception:
        logging.exception("[FREELANCE] /lead")
        await message.answer("⚠️ Не удалось показать карточку, подробности в логе.")


@dp.callback_query(F.data.startswith("fl_draft:"))
async def cb_fl_draft(call: CallbackQuery):
    if call.message is None or call.message.chat.id != MY_CHAT_ID:
        await call.answer()
        return
    oid = _order_id_from(call.data)
    row = await asyncio.to_thread(freelance_get, oid) if oid is not None else None
    if row is None:
        await call.answer("Заказ не найден")
        return
    await call.answer()
    draft, private = draft_messages(row)
    await call.message.answer(private, parse_mode=None)
    await call.message.answer(draft, parse_mode=None)


@dp.callback_query(F.data.startswith("fl_lead:"))
async def cb_fl_lead(call: CallbackQuery):
    """Кнопка «📋 Лид» на карточке заказа: карточка лида отдельным сообщением."""
    try:
        order_id = _order_id_from(call.data)
        if order_id is None:
            await call.answer("Некорректная кнопка")
            return
        await call.answer()
        await _show_lead_card(call, order_id, edit=False)
    except Exception:
        await _answer_error(call, "fl_lead")


@dp.callback_query(F.data.startswith("fl_card:"))
async def cb_fl_card(call: CallbackQuery):
    """«◀️ Назад» из выбора причины/срока и «📋 Лид» из напоминания: карточка лида на месте сообщения."""
    try:
        order_id = _order_id_from(call.data)
        if order_id is None:
            await call.answer("Некорректная кнопка")
            return
        await call.answer()
        await _show_lead_card(call, order_id, edit=True)
    except Exception:
        await _answer_error(call, "fl_card")


@dp.callback_query(F.data.startswith("fl_st:"))
async def cb_fl_stage(call: CallbackQuery):
    """Кнопки этапов на карточке лида. «Договорились» спрашивает цену, как прежний «Взял»."""
    try:
        parsed = _ids_from(call.data)
        if parsed is None or parsed[1] not in jr.LEAD_STAGES or parsed[1] == jr.LEAD_LOST:
            await call.answer("Некорректная кнопка")
            return
        order_id, stage = parsed
        res = await asyncio.to_thread(leads.set_stage, order_id, stage)
        if not res.get("ok"):
            await call.answer("Заказ не найден")
            return
        if res.get("changed"):
            deal_wait.drop_order(_pending_deals, order_id)
        await call.answer(f"Этап: {lv.stage_label(stage)}")
        await _show_lead_card(call, order_id, edit=True)
        if stage == jr.LEAD_AGREED and res.get("changed"):
            row = await asyncio.to_thread(freelance_get, order_id) or {}
            await _ask_deal_price(call.message, order_id, row.get("title") or f"заказ {order_id}")
    except Exception:
        await _answer_error(call, "fl_st")


@dp.callback_query(F.data.startswith("fl_lost:"))
async def cb_fl_lost(call: CallbackQuery):
    """«❌ Отказ»: выбор причины."""
    try:
        order_id = _order_id_from(call.data)
        if order_id is None:
            await call.answer("Некорректная кнопка")
            return
        await call.answer()
        await call.message.edit_reply_markup(reply_markup=_kb(lv.lost_reason_buttons(order_id)))
    except Exception:
        await _answer_error(call, "fl_lost")


@dp.callback_query(F.data.startswith("fl_lr:"))
async def cb_fl_lost_reason(call: CallbackQuery):
    """Причина отказа выбрана: lead=lost, действие и напоминание снимаются, метка триажа пересчитывается."""
    try:
        parsed = _ids_from(call.data)
        if parsed is None or not parsed[1].isdigit() or int(parsed[1]) >= len(jr.LOST_REASONS):
            await call.answer("Некорректная кнопка")
            return
        order_id, reason = parsed[0], jr.LOST_REASONS[int(parsed[1])]
        res = await asyncio.to_thread(leads.set_stage, order_id, jr.LEAD_LOST, reason)
        if not res.get("ok"):
            await call.answer("Заказ не найден")
            return
        deal_wait.drop_order(_pending_deals, order_id)
        await call.answer(f"Отказ: {reason}")
        await _show_lead_card(call, order_id, edit=True)
    except Exception:
        await _answer_error(call, "fl_lr")


@dp.callback_query(F.data.startswith("fl_rm:"))
async def cb_fl_remind_menu(call: CallbackQuery):
    """«⏰ Напомнить»: выбор срока."""
    try:
        order_id = _order_id_from(call.data)
        if order_id is None:
            await call.answer("Некорректная кнопка")
            return
        await call.answer()
        await call.message.edit_reply_markup(reply_markup=_kb(lv.remind_buttons(order_id)))
    except Exception:
        await _answer_error(call, "fl_rm")


@dp.callback_query(F.data.startswith("fl_rc:"))
async def cb_fl_remind_choice(call: CallbackQuery):
    try:
        parsed = _ids_from(call.data)
        if parsed is None or parsed[1] not in leads.REMIND_CHOICES:
            await call.answer("Некорректная кнопка")
            return
        res = await asyncio.to_thread(leads.set_remind, parsed[0], parsed[1])
        if not res.get("ok"):
            await call.answer("Напоминание есть только у активного лида", show_alert=True)
            return
        await call.answer("Срок обновлён")
        await _show_lead_card(call, parsed[0], edit=True)
    except Exception:
        await _answer_error(call, "fl_rc")


@dp.callback_query(F.data.startswith("fl_undo:"))
async def cb_fl_undo(call: CallbackQuery):
    """«↩️ Отменить последнее»: возврат состояния и метки триажа; повторное нажатие идёт на шаг дальше."""
    try:
        order_id = _order_id_from(call.data)
        if order_id is None:
            await call.answer("Некорректная кнопка")
            return
        res = await asyncio.to_thread(leads.undo, order_id)
        if res.get("ok") and res.get("undone_kind") == "stage":
            deal_wait.drop_order(_pending_deals, order_id)
        await call.answer("Отменено" if res.get("ok") else "Нечего отменять")
        await _show_lead_card(call, order_id, edit=True)
    except Exception:
        await _answer_error(call, "fl_undo")


@dp.message(Command("next"))
async def cmd_next(message: Message):
    """/next <id> <текст> - своё следующее действие по лиду; без текста действие убирается."""
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer("Формат: /next номер_заказа текст действия\nНапример: /next 12345 выслать смету")
        return
    order_id = int(parts[1])
    try:
        res = await asyncio.to_thread(leads.set_next, order_id, parts[2] if len(parts) > 2 else None)
    except Exception:
        logging.exception("[FREELANCE] /next")
        await message.answer("⚠️ Не удалось записать, подробности в логе.")
        return
    if not res.get("ok"):
        await message.answer("Заказ не найден." if res.get("reason") == "not_found"
                             else "Действие задаётся у активного лида (сохранён → договорились).")
        return
    await _show_lead_card(message, order_id, edit=False)


async def _inbox_text() -> str:
    active, unprocessed, closed = await asyncio.gather(
        asyncio.to_thread(freelance_leads_active), asyncio.to_thread(freelance_unprocessed),
        asyncio.to_thread(freelance_leads_closed, 7))
    return lv.inbox_text(active, unprocessed, closed)


async def _send_inbox(message: Message) -> None:
    try:
        text = await _inbox_text()
        for chunk in _split_messages(text):
            await message.answer(chunk, parse_mode="HTML", disable_web_page_preview=True)
    except Exception:
        logging.exception("[FREELANCE] inbox")
        await message.answer("⚠️ Не удалось собрать «Входящие», подробности в логе.")


@dp.callback_query(F.data == "fl_inbox")
async def cb_fl_inbox(call: CallbackQuery):
    await call.answer()
    await _send_inbox(call.message)


@dp.message(Command("inbox"))
async def cmd_inbox(message: Message):
    await _send_inbox(message)


_reminder_lock = asyncio.Lock()


async def send_lead_reminders():
    """Раз в 5 минут: активные лиды с наступившим сроком - напоминание ТОЛЬКО в MY_CHAT_ID. reminded_at
    ставится после успешной отправки: при ошибке Telegram напоминание уйдёт в следующий раз; пропущенное за
    время простоя уходит после рестарта один раз. Закрытые лиды (done/lost) напоминаний не получают."""
    if _reminder_lock.locked():
        return
    async with _reminder_lock:
        try:
            for row in await asyncio.to_thread(freelance_reminders_due):
                try:
                    await bot.send_message(chat_id=MY_CHAT_ID, text=lv.reminder_text(row), parse_mode="HTML",
                                           reply_markup=_kb(lv.reminder_buttons(row["order_id"])),
                                           disable_web_page_preview=True)
                except TelegramRetryAfter as e:
                    logging.warning(f"[FREELANCE] flood control при напоминании, ждём {e.retry_after} с")
                    return
                except Exception:
                    logging.exception(f"[FREELANCE] напоминание по заказу {row['order_id']} не отправлено")
                    continue
                await asyncio.to_thread(freelance_mark_reminded, row["order_id"], row["remind_at"])
                await asyncio.sleep(0.1)
        except Exception:
            logging.exception("[FREELANCE] send_lead_reminders")


OFFER_HOUR_MSK = 10
OFFER_DAY_KEY = "freelance_offer_day"   # дата (МСК) последней отправленной подборки
OFFER_LIMIT = 10
_offer_lock = asyncio.Lock()


async def send_lead_offers():
    """Раз в сутки (10:00 МСК) подборка необработанных интересных заказов, до 10. Каждый заказ попадает в
    автоподборку один раз (offered_at). Пустая подборка и пауза фриланса - ничего не отправляют."""
    async with _offer_lock:
        claimed, rows = None, []
        try:
            if not await _freelance_subscribed():
                return
            today = datetime.now(MSK).date().isoformat()
            if await asyncio.to_thread(kv_get, OFFER_DAY_KEY) == today:
                return
            claimed, rows = await asyncio.to_thread(freelance_claim_offers, OFFER_LIMIT, fresh_only=True)
            fresh = [r for r in rows if live_fresh(r)]
            if fresh:
                for r in fresh:
                    if r["pub_unreliable"] or not r["published_at"]:
                        r["title"] += " · возраст неизвестен"
                await bot.send_message(chat_id=MY_CHAT_ID, text=lv.offers_text(fresh), parse_mode="HTML",
                                       disable_web_page_preview=True)
                await asyncio.to_thread(freelance_confirm_offers, claimed, [r["order_id"] for r in fresh], today)
            if len(fresh) != len(rows):
                await asyncio.to_thread(freelance_release_offers, claimed,
                                        [r["order_id"] for r in rows if r not in fresh])
        except Exception:
            logging.exception("[FREELANCE] send_lead_offers")
            if claimed and rows:   # не ушло: заказы вернуть в подборку, день не отмечаем (повтор в следующий раз)
                try:
                    await asyncio.to_thread(freelance_release_offers, claimed, [r["order_id"] for r in rows])
                except Exception:
                    logging.exception("[FREELANCE] не удалось вернуть заказы в подборку")


async def catch_up_lead_offers():
    """После рестарта: если 10:00 МСК уже прошло, а подборка за сегодня не уходила - отправить."""
    if datetime.now(MSK).hour >= OFFER_HOUR_MSK:
        await send_lead_offers()


async def cleanup_freelance_messages():
    """Раз в час удаляет из чата сообщения с заказами старше TTL (кроме «Взял»)."""
    try:
        rows = await asyncio.to_thread(freelance_expired_messages, FREELANCE_MESSAGE_TTL_HOURS)
        deleted = 0
        for row in rows:
            try:
                await bot.delete_message(chat_id=MY_CHAT_ID, message_id=row["message_id"])
                deleted += 1
            except TelegramBadRequest as e:
                # «message to delete not found», «can't be deleted» (старше 48 ч): уже не нужно
                logging.info(f"[FREELANCE] сообщение {row['message_id']} не удалено: {e}")
            except TelegramRetryAfter as e:
                logging.warning(f"[FREELANCE] flood control при очистке, ждём {e.retry_after} с")
                await asyncio.sleep(min(e.retry_after, 30))
                break  # остальные - в следующий час
            except Exception:
                logging.exception(f"[FREELANCE] не удалось удалить сообщение {row['message_id']}")
                continue  # статус не меняем: повторим в следующий час
            # карточка удалена по сроку: лид и техническая обработка заказа не меняются
            if row["duplicate_card"]:
                await asyncio.to_thread(freelance_mark_card_deleted, row["order_id"], row["message_id"])
            else:
                await asyncio.to_thread(freelance_mark_card_deleted, row["order_id"])
            await asyncio.sleep(0.1)
        if rows:
            logging.info(f"[FREELANCE] очистка: найдено {len(rows)}, удалено {deleted}")
    except Exception:
        logging.exception("[FREELANCE] cleanup_freelance_messages")


SUMMARY_HOUR_MSK = 21
SUMMARY_DAY_KEY = "freelance_summary_day"   # дата (МСК) последней отправленной сводки
_summary_lock = asyncio.Lock()


async def send_freelance_daily_summary():
    """Раз в сутки (21:00 МСК) короткая сводка: тишина в чате отличима от поломки бота.
    Одна сводка в календарный день (МСК): дату отправки помним в БД, поэтому повторный
    вызов (cron + догоняющая отправка после рестарта) второй раз не пишет."""
    async with _summary_lock:
        await _send_freelance_daily_summary_locked()


async def catch_up_freelance_summary():
    """После рестарта: если 21:00 МСК уже прошло, а сводка за сегодня не уходила - отправить."""
    if datetime.now(MSK).hour >= SUMMARY_HOUR_MSK:
        await send_freelance_daily_summary()


def _ai_usage_line(st: dict) -> str:
    """Расход AI-запросов за сутки по провайдерам и число быстрых оценок. Счётчики
    держатся с последнего сброса (03:00 МСК, Yandex - 00:00), в 21:00 это почти весь день."""
    parts = []
    for name, p in (st.get("providers") or {}).items():
        if p.get("enabled") and p.get("limit"):
            parts.append(f"{name} {p.get('today', 0)}/{p['limit']}")
    triage = int(st.get("triage_today") or 0)
    if triage:
        parts.append(f"быстрых оценок {triage}")
    return ("AI за сутки: " + ", ".join(parts)) if parts else ""


async def _send_freelance_daily_summary_locked():
    try:
        if not await _freelance_subscribed():
            return  # фриланс на паузе: сводка не нужна
        today = datetime.now(MSK).date().isoformat()
        if await asyncio.to_thread(kv_get, SUMMARY_DAY_KEY) == today:
            return  # за сегодня уже отправляли
        d = await asyncio.to_thread(freelance_day_summary, near_miss=0)
        passed = d["total"] - d["filtered"]
        text = (
            "📋 <b>Фриланс за сутки</b>\n"
            f"Увидено заказов: {d['total'] + d['excluded']} (вакансионные доски: {d['excluded']}, "
            f"отсеяно правилами: {d['filtered']}, дубли: {d['duplicate']})\n"
            f"Прошли фильтр и дедуп: {passed - d['duplicate']}\n"
            f"Из увиденных оценено: {d['assessed']} (подходят {d['good']}, отклонены AI {d['low_fit']})\n"
            f"Из увиденных доставлено: карточек {d['card']}, в сводке «ещё N» {d['digest']}, "
            f"ждут доставки {d['delivery_pending']}\n"
            f"Ждут оценки AI: {d['queued']}, устарели: {d['stale']}"
        )
        live = await asyncio.to_thread(freelance_live_daily_metrics)
        text += (f"\nЗа 24 ч оценок {live['scored']}, автоматических карточек {live['cards']}. "
                 f"До 5 мин: {live['under_five']}/{live['known']} с датой публикации. "
                 f"Без подтверждённой даты: {live['cards'] - live['known']}.")
        medians = live['medians']
        stages = (("поиск", "discovery"), ("ожидание и оценка", "analysis"),
                  ("доставка", "delivery"), ("всего от публикации", "total"))
        lag = [f"{label} {medians[key]} с" for label, key in stages if medians[key] is not None]
        if lag:
            text += "\nМедиана для доставленных: " + ", ".join(lag)
        usage = _ai_usage_line(await asyncio.to_thread(ai_scorer.status))
        if usage:
            text += "\n" + usage.replace("AI за сутки:", "Запросы AI с последнего сброса:", 1)
        await bot.send_message(chat_id=MY_CHAT_ID, text=text, parse_mode="HTML",
                               disable_web_page_preview=True)
        await asyncio.to_thread(kv_set, SUMMARY_DAY_KEY, today)
    except Exception:
        logging.exception("[FREELANCE] send_freelance_daily_summary")


@dp.callback_query(F.data == "fl_toggle")
async def cb_fl_toggle(call: CallbackQuery):
    try:
        now_on = await _freelance_subscribed()
        await asyncio.to_thread(kv_set, "freelance_subscribed", "0" if now_on else "1")
        await call.answer("Пауза" if now_on else "Включено")
        await call.message.edit_reply_markup(reply_markup=await freelance_menu_kb())
    except Exception:
        logging.exception("[FREELANCE] fl_toggle")
        await call.answer("Ошибка, см. лог")


@dp.callback_query(F.data == "fl_status")
async def cb_fl_status(call: CallbackQuery):
    await call.answer()
    try:
        text = await asyncio.to_thread(_freelance_status_text)
    except Exception:
        logging.exception("[FREELANCE] fl_status")
        text = "⚠️ Не удалось собрать статус."
    await call.message.answer(text, parse_mode="HTML")


SOURCES_HELP = (
    "🔌 <b>Источники заказов</b>\n"
    "alot.pro собирает заказы с разных бирж. Прямые источники могут находить их раньше. "
    "Повторы отсекаются до оценки AI.\n"
    "Нажмите на источник, чтобы переключить."
)


def _sources_kb() -> InlineKeyboardMarkup:
    rows = []
    for name, title, on, forced in sources.states():
        if forced:
            label, data = f"⛔ {title}: выключен в настройках сервера", "fl_src_noop"
        else:
            label, data = (f"🟢 {title}: включён" if on else f"⚪ {title}: выключен"), f"fl_src_t:{name}"
        rows.append([InlineKeyboardButton(text=label, callback_data=data)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@dp.callback_query(F.data == "fl_src")
async def cb_fl_sources(call: CallbackQuery):
    await call.answer()
    await call.message.answer(SOURCES_HELP, parse_mode="HTML", reply_markup=_sources_kb())


@dp.callback_query(F.data == "fl_src_noop")
async def cb_fl_sources_noop(call: CallbackQuery):
    await call.answer("Выключен переменной окружения на сервере", show_alert=True)


@dp.callback_query(F.data.startswith("fl_src_t:"))
async def cb_fl_sources_toggle(call: CallbackQuery):
    try:
        name = call.data.split(":", 1)[1]
        if name not in sources.ALL_SOURCES:
            await call.answer("Неизвестный источник")
            return
        now_on = await asyncio.to_thread(sources.is_enabled, name)
        ok = await asyncio.to_thread(sources.set_enabled, name, not now_on)
        if not ok:
            await call.answer("Этот источник выключен на сервере", show_alert=True)
            return
        await call.answer(f"{sources.TITLES[name]}: {'выключен' if now_on else 'включён'}")
        await call.message.edit_reply_markup(reply_markup=_sources_kb())
    except Exception:
        logging.exception("[FREELANCE] fl_src_t")
        await call.answer("Ошибка, см. лог")


# ---------- Общая логика /list ----------

async def _send_list(message: Message, limit: int):
    """Отправляет список вакансий в Telegram."""
    vacancies = get_recent_vacancies(limit=limit)

    if not vacancies:
        await message.answer(
            "📭 В базе пока пусто.\n\n"
            "Нажмите «🔍 Проверить сейчас» или отправьте /check.",
            reply_markup=main_reply_kb(),
        )
        return

    lines = [f"📋 <b>Все вакансии в базе ({len(vacancies)} шт.)</b>\n"]

    for v in vacancies:
        published = v.get("published_at") or ""
        found = v.get("found_at", "")[:10]

        if published:
            time_str = f"🕒 Опубликовано: {_human_date(published)}"
        elif found:
            time_str = f"🕒 Найдено: {_human_date_short(found)}"
        else:
            time_str = ""

        # Экранируем: «&» или «<» в названии иначе ломают HTML-разметку
        block = (
            f"• <b>{html.escape(v['title'] or '')}</b>\n"
            f"  {html.escape(v['company'] or '')}\n"
            f"  <a href=\"{html.escape(v['url'] or '')}\">Открыть вакансию</a>\n"
        )
        if time_str:
            block = f"{time_str}\n{block}"

        lines.append(block)

    full_text = "\n".join(lines)

    for chunk in _split_messages(full_text):
        await message.answer(chunk, parse_mode="HTML")

    await message.answer("— Конец списка —", reply_markup=main_reply_kb())


# ---------- Команды ----------

@dp.message(CommandStart())
async def cmd_start(message: Message):
    await asyncio.to_thread(_set_subscribed, True)

    await message.answer(
        f"👋 Привет!\n\n"
        f"Слежу за новыми вакансиями <b>Junior/стажёр</b> по разработке.\n"
        f"Удалёнка в приоритете, но беру и офисные.\n\n"
        f"⏱ Проверка каждые <b>{CHECK_INTERVAL_MINUTES} мин</b>.\n"
        f"🗑 Вакансии старше <b>{CLEANUP_DAYS} дней</b> не отправляются "
        f"и удаляются из базы.\n\n"
        f"<b>Команды:</b>\n"
        f"/list — все вакансии из базы\n"
        f"/list 50 — только первые 50\n"
        f"/status — статистика\n"
        f"/check — проверить сейчас\n"
        f"/aitest — проверка AI-провайдеров\n"
        f"/reset — очистить базу и загрузить заново\n"
        f"/stop — приостановить\n\n"
        f"Пользуйся кнопками внизу 👇",
        parse_mode="HTML",
        reply_markup=main_reply_kb(),
    )

    asyncio.create_task(check_vacancies())


@dp.message(Command("triage_audit"))
async def cmd_triage_audit(message: Message):
    """Партия случайных заказов, которые быстрый триаж отсёк бы: просмотрите, нет ли среди них подходящих."""
    batch = await asyncio.to_thread(triage_audit_sample, 20)
    rows = batch["rows"]
    if not rows:
        await message.answer("Нет новых заказов, которые триаж отсёк бы при текущих настройках (или все уже выданы).")
        return
    lines = [f"Партия заказов, которые триаж отсёк бы (код {batch['code']}). Откройте и оцените каждый сами. "
             f"Подтверждение: /triage_approve {batch['code']} [id подходящих через пробел]. "
             "Повторный запрос выдаёт ту же партию."]
    for r in rows:
        lines.append(f"- {r['order_id']}: {html.escape(r['title'][:80])} {html.escape(r['url'])}")
    for chunk in _split_messages("\n".join(lines)):
        await message.answer(chunk, parse_mode="HTML", disable_web_page_preview=True)


@dp.message(Command("triage_approve"))
async def cmd_triage_approve(message: Message):
    """/triage_approve <код партии> [id подходящих]: итог просмотра выданной партии отсекаемых заказов."""
    parts = (message.text or "").split()
    if len(parts) < 2 or not all(x.isdigit() for x in parts[2:]):
        await message.answer("Формат: /triage_approve <код партии> [id подходящих заказов партии]")
        return
    await message.answer(await asyncio.to_thread(approve_triage, parts[1], [int(x) for x in parts[2:]]))


@dp.message(Command("funnel"))
async def cmd_funnel(message: Message):
    """Воронка и задержки по источникам за N дней (по умолчанию 1): /funnel 7."""
    parts = (message.text or "").split()
    days = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 1
    days = max(1, min(days, 30))
    try:
        rows = await asyncio.to_thread(report.load_rows, days)
        excluded = await asyncio.to_thread(report.load_excluded, days)
        for chunk in _split_messages(report.format_funnel(rows, excluded, days)):
            await message.answer(chunk, parse_mode="HTML")
    except Exception:
        logging.exception("[FREELANCE] funnel")
        await message.answer("⚠️ Не удалось собрать воронку, подробности в логе.")


@dp.message(Command("export"))
async def cmd_export(message: Message):
    """CSV журнала увиденных заказов за N дней (по умолчанию 7): /export 14. Названия обезличены,
    описаний нет. Нужен, чтобы по каждому заказу проверить, куда он делся."""
    parts = (message.text or "").split()
    days = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 7
    days = max(1, min(days, 30))
    try:
        rows = await asyncio.to_thread(report.load_rows, days)
        data = report.export_csv(rows).encode("utf-8-sig")   # BOM: Excel открывает кириллицу без танцев
        name = f"freelance_journal_{datetime.now(MSK):%Y%m%d}_{days}d.csv"
        await message.answer_document(BufferedInputFile(data, filename=name),
                                      caption=f"Журнал за {days} дн.: {len(rows)} заказов")
    except Exception:
        logging.exception("[FREELANCE] export")
        await message.answer("⚠️ Не удалось выгрузить журнал, подробности в логе.")


@dp.message(Command("stop"))
async def cmd_stop(message: Message):
    await asyncio.to_thread(_set_subscribed, False)
    await message.answer(
        "⏸ Уведомления приостановлены.\n\nНажмите «▶️ Возобновить» или /start.",
        reply_markup=main_reply_kb(),
    )


@dp.message(Command("aitest"))
async def cmd_aitest(message: Message):
    """Проверка AI-провайдеров: каждый оценивает один тестовый заказ (1 запрос на провайдера).
    Доступ только у MY_CHAT_ID - через whitelist-middleware, как у остальных команд."""
    await message.answer("🧪 Проверяю AI-провайдеры, это может занять пару минут…")
    try:
        results = await asyncio.to_thread(ai_scorer.probe)
    except Exception:
        logging.exception("[AI] /aitest упал")
        await message.answer("❌ Не удалось выполнить проверку, подробности в логе.")
        return
    lines = ["🧪 <b>Проверка AI</b>\n"]
    for r in results:
        name = html.escape(r["provider"])
        if not r["configured"]:
            lines.append(f"➖ {name} — {html.escape(r['error'])}")
        elif r["ok"]:
            lines.append(f"✅ {html.escape(r['model'])} — {r['seconds']:.1f} с, fit {r['fit']}")
        else:
            lines.append(f"❌ {html.escape(r['model'] or name)} — {html.escape(r['error'][:300])}")
    await message.answer("\n".join(lines), parse_mode="HTML")


@dp.message(Command("status"))
async def cmd_status(message: Message):
    s = stats()
    lines = ["📊 <b>Статистика</b>\n"]
    lines.append(f"Всего вакансий в базе: <b>{s['total']}</b>")
    if s["by_source"]:
        lines.append("\n<b>По источникам:</b>")
        for source, cnt in s["by_source"].items():
            lines.append(f"• {source}: {cnt}")
    if s["last_found"]:
        lines.append(f"\n🕒 Последняя: {s['last_found']}")
    lines.append(f"\n⏱ Интервал проверки: {CHECK_INTERVAL_MINUTES} мин")
    lines.append(f"🗑 Хранение: {CLEANUP_DAYS} дней (от даты публикации)")
    lines.append(
        f"🆕 Фильтр свежести: не старше {MAX_VACANCY_AGE_DAYS} дней"
    )
    lines.append(f"📬 Подписка: {'включена' if subscribed else 'выключена'}")
    mem = memwatch.mem_used_mb()
    if mem is not None:
        lines.append(f"🧠 Память: {mem} МБ из {memwatch.MEM_LIMIT_MB} (пик {memwatch.peak_mb()})")
    parsers ={k: v for k, v in monitor.streaks().items() if k not in FREELANCE_HEALTH_SOURCES and not k.startswith(("ai_key_", "ai_paid_", "yandex_grant_"))}
    if parsers:
        lines.append("\n<b>Парсеры:</b>")
        for source, streak in sorted(parsers.items()):
            if streak == 0:
                lines.append(f"🟢 {source}: работает")
            else:
                reason = html.escape(monitor.last_reason.get(source, ""))
                lines.append(f"🔴 {source}: не работает, неудачных проверок подряд: {streak} ({reason})")
    if _check_lock.locked():
        lines.append("🔄 Проверка сейчас идёт")
    await message.answer("\n".join(lines), parse_mode="HTML", reply_markup=main_reply_kb())


@dp.message(Command("list"))
async def cmd_list(message: Message):
    """Показывает вакансии. /list — все, /list 50 — первые 50."""
    text = message.text or ""
    parts = text.split(maxsplit=1)
    limit = DEFAULT_LIST_LIMIT

    if len(parts) > 1:
        arg = parts[1].strip()
        if arg.isdigit():
            limit = int(arg)
            if limit <= 0:
                limit = DEFAULT_LIST_LIMIT
            if limit > MAX_LIST_LIMIT:
                limit = MAX_LIST_LIMIT

    await _send_list(message, limit)


@dp.message(Command("check"))
async def cmd_check(message: Message):
    """Запускает проверку и сообщает результат корректно."""
    await message.answer("🔍 Проверяю вакансии…")

    performed = await check_vacancies()

    if performed:
        await message.answer(
            "✅ Проверка завершена.",
            reply_markup=main_reply_kb(),
        )
    else:
        await message.answer(
            "⏳ Проверка уже идёт — дождитесь её окончания.\n"
            "Новые вакансии придут отдельными сообщениями.",
            reply_markup=main_reply_kb(),
        )


@dp.message(Command("reset"))
async def cmd_reset(message: Message):
    """Очищает базу и заново загружает вакансии."""
    if _check_lock.locked():
        await message.answer(
            "⏳ Проверка уже идёт. Дождитесь её окончания и попробуйте /reset снова.",
            reply_markup=main_reply_kb(),
        )
        return

    deleted = reset_db()
    logging.info(f"[BOT] База очищена: удалено {deleted} записей")

    await message.answer(
        f"🗑 База очищена (удалено {deleted} записей).\n\n"
        f"Загружаю вакансии заново…",
        reply_markup=main_reply_kb(),
    )

    await check_vacancies()
    await message.answer(
        "✅ Готово. Проверьте /list — теперь с реальными датами публикации.",
        reply_markup=main_reply_kb(),
    )


# ---------- Reply-кнопки ----------

@dp.message(F.text == "📋 Список")
async def btn_list(message: Message):
    await _send_list(message, DEFAULT_LIST_LIMIT)


@dp.message(F.text == "📊 Статус")
async def btn_status(message: Message):
    await cmd_status(message)


@dp.message(F.text == "🔍 Проверить сейчас")
async def btn_check(message: Message):
    await cmd_check(message)


@dp.message(F.text == "⏸ Пауза")
async def btn_pause(message: Message):
    await cmd_stop(message)


@dp.message(F.text == "▶️ Возобновить")
async def btn_resume(message: Message):
    await cmd_start(message)


MANUAL_PROMPT = "Пришли текст, подпись или пересылку с описанием задачи. Если есть ссылка на исходное объявление, добавь её. /cancel — отменить."
MANUAL_WAIT = "freelance_manual_wait"


async def cb_manual_start(call: CallbackQuery):
    await call.answer()
    await asyncio.to_thread(kv_set, MANUAL_WAIT, "new")
    await call.message.answer(MANUAL_PROMPT)


async def cmd_manual_cancel(message: Message):
    await asyncio.to_thread(kv_set, MANUAL_WAIT, "")
    await message.answer("Ручной ввод завершён.")


async def _store_manual(message, text, wait):
    entities = message.entities or message.caption_entities or []
    urls = [e.url for e in entities if e.url]
    origin = message.forward_origin
    channel = getattr(origin, "chat", None)
    if channel and channel.username and getattr(origin, "message_id", None):
        urls.append(f"https://t.me/{channel.username}/{origin.message_id}")
    oid, short = await asyncio.to_thread(manual_orders.store, message.message_id, text, urls,
                                         int(wait) if wait and wait.isdigit() else None)
    await asyncio.to_thread(kv_set, MANUAL_WAIT, str(oid) if short else "")
    if short:
        await message.answer(f"Сохранил /lead_{oid}. Пришли описание результата и исходных данных, чтобы дополнить этот заказ.")
    else:
        await message.answer(f"Заказ в журнале: /lead_{oid}. Оценка доступна в карточке после обработки очереди.")


async def cmd_manual_add(message: Message):
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) == 2:
        await _store_manual(message, parts[1], "")
        return
    reply = message.reply_to_message
    if reply and (reply.text or reply.caption):
        await _store_manual(reply, reply.text or reply.caption, "")
        return
    await asyncio.to_thread(kv_set, MANUAL_WAIT, "new")
    await message.answer(MANUAL_PROMPT)


async def _manual_input(message: Message):
    text = message.text or message.caption or ""
    if not text or text.startswith("/"):
        return False
    wait = await asyncio.to_thread(kv_get, MANUAL_WAIT, "")
    return {"manual_wait": wait or ""} if wait or message.forward_origin else False


async def msg_manual_input(message: Message, manual_wait: str):
    await _store_manual(message, message.text or message.caption, manual_wait)


# Личный журнал и состояние диалога доступны только в чате владельца.
dp.callback_query.register(cb_manual_start, F.data == "fl_add", F.message.chat.id == MY_CHAT_ID)
dp.message.register(cmd_manual_cancel, Command("cancel"), F.chat.id == MY_CHAT_ID)
dp.message.register(cmd_manual_add, Command("add"), F.chat.id == MY_CHAT_ID)
dp.message.register(msg_manual_input, F.chat.id == MY_CHAT_ID, _manual_input)


# ---------- Точка входа ----------

async def main():
    init_db()
    _load_subscribed()

    deleted = cleanup_old(days=CLEANUP_DAYS)
    logging.info(
        f"Очистка БД: удалено {deleted} записей старше {CLEANUP_DAYS} дней"
    )

    scheduler.add_job(
        lambda: logging.info(
            f"Очистка БД: удалено {cleanup_old(days=CLEANUP_DAYS)} записей"
        ),
        "cron",
        hour=3,
        minute=0,
    )

    scheduler.add_job(
        check_vacancies,
        "interval",
        minutes=CHECK_INTERVAL_MINUTES,
    )
    for source in sources.ALL_SOURCES:
        interval = (FREELANCE_INTERVAL_MINUTES * 60 if source in (sources.ALOT, sources.FLRU)
                    else FREELANCE_HTML_SECONDS)
        scheduler.add_job(_collect_freelance, "interval", seconds=interval, args=(source,),
                          next_run_time=datetime.now(timezone.utc), coalesce=True, max_instances=1)
    for stage in (_pump_freelance_ai, _deliver_freelance):
        scheduler.add_job(stage, "interval", seconds=FREELANCE_STAGE_SECONDS, coalesce=True, max_instances=1)
    scheduler.add_job(_maintain_freelance, "interval", seconds=30, coalesce=True, max_instances=1)
    scheduler.add_job(_refresh_freelance_ai_catalog, "interval", minutes=5,
                      next_run_time=datetime.now(timezone.utc), coalesce=True, max_instances=1)
    scheduler.add_job(
        cleanup_freelance_messages,
        "interval",
        hours=1,
    )
    scheduler.add_job(
        send_freelance_daily_summary,
        "cron",
        hour=SUMMARY_HOUR_MSK,
        minute=0,
        timezone=MSK,
        misfire_grace_time=1800,  # задержка планировщика; после рестарта - catch_up_freelance_summary
        coalesce=True,
    )
    scheduler.add_job(
        send_lead_reminders,
        "interval",
        minutes=5,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        send_lead_offers,
        "cron",
        hour=f"{OFFER_HOUR_MSK}-23",
        minute="*/10",
        timezone=MSK,
        misfire_grace_time=1800,  # после рестарта - catch_up_lead_offers
        coalesce=True,
    )
    scheduler.start()
    asyncio.create_task(catch_up_freelance_summary())
    asyncio.create_task(catch_up_lead_offers())
    logging.info(f"Планировщик запущен: проверка каждые {CHECK_INTERVAL_MINUTES} мин")
    logging.info(f"Бот запущен. Разрешён только user_id={MY_CHAT_ID}")

    try:
        await bot.send_message(
            chat_id=MY_CHAT_ID,
            text="🤖 Бот запущен. Слежу за новыми вакансиями.",
            reply_markup=main_reply_kb(),
        )
    except Exception:
        logging.exception("Не удалось отправить приветствие")

    await dp.start_polling(bot, drop_pending_updates=True)


if __name__ == "__main__":
    asyncio.run(main())
