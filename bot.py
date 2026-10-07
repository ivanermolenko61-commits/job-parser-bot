"""Telegram-бот: мониторинг вакансий Junior/стажёр по разработке."""
import asyncio
import html
import logging
import os
from datetime import datetime
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
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
    FREELANCE_MAX_PER_CYCLE,
    FREELANCE_MESSAGE_TTL_HOURS,
    FREELANCE_MIN_FIT,
    MAX_VACANCY_AGE_DAYS,
)
from database import (
    cleanup_old,
    freelance_expired_messages,
    freelance_recent,
    freelance_set_status,
    freelance_stats,
    get_recent_vacancies,
    init_db,
    kv_get,
    kv_set,
    reset_db,
    stats,
)
from freelance import ai_scorer
from freelance.alot_client import site_name
from freelance.pipeline import (
    LAST_ID_KEY,
    MSK,
    PREPARE_TIMEOUT_SEC,
    finish_cycle,
    format_order,
    format_order_plain,
    mark_sent_status,
    prepare_cycle,
    save_sent,
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

# По умолчанию показываем до 300 вакансий (практически всё, что есть в БД)
DEFAULT_LIST_LIMIT = 300
# Жёсткий лимит — защита от слишком длинных списков
MAX_LIST_LIMIT = 500
MSG_CHAR_LIMIT = 3500
# Источники health-монитора, относящиеся к фрилансу (в /status вакансий не показываем)
FREELANCE_HEALTH_SOURCES = {"alot", "gemini", "memory"}  # memory - не парсер вакансий

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
        sent_ok: list = []
        for v in new_vacancies:
            try:
                await bot.send_message(
                    chat_id=MY_CHAT_ID,
                    text=v.format_message(),
                    parse_mode="HTML",
                )
                sent_ok.append(v)
                await asyncio.sleep(0.5)
            except Exception:
                logging.exception(f"[BOT] Не удалось отправить {v.url}")

        if sent_ok:
            mark_vacancies_sent(sent_ok)
            logging.info(f"[BOT] Успешно отправлено: {len(sent_ok)}")
        if len(sent_ok) < len(new_vacancies):
            logging.warning(
                f"[BOT] Не отправлено: {len(new_vacancies) - len(sent_ok)} "
                f"(будут повторены в следующую проверку)"
            )

        return True


# ---------- Фриланс (alot.pro) ----------

_freelance_lock = asyncio.Lock()


def _freelance_subscribed_sync() -> bool:
    return kv_get("freelance_subscribed", "1") == "1"


async def _freelance_subscribed() -> bool:
    return await asyncio.to_thread(_freelance_subscribed_sync)


async def freelance_menu_kb() -> InlineKeyboardMarkup:
    toggle = "⏸ Пауза" if await _freelance_subscribed() else "▶️ Включить"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Последние заказы", callback_data="fl_recent")],
        [InlineKeyboardButton(text="🔍 Проверить сейчас", callback_data="fl_check")],
        [
            InlineKeyboardButton(text=toggle, callback_data="fl_toggle"),
            InlineKeyboardButton(text="📊 Статус", callback_data="fl_status"),
        ],
    ])


def _order_kb(order_id: int, url: str | None, taken: bool = False) -> InlineKeyboardMarkup:
    """Кнопки заказа: открыть, «Взял» / «Не подходит» (после «Взял» - «В работе»)."""
    rows = []
    if url:
        rows.append([InlineKeyboardButton(text="🔗 Открыть", url=url)])
    if taken:
        rows.append([InlineKeyboardButton(text="✅ В работе", callback_data=f"fl_noop:{order_id}")])
    else:
        rows.append([
            InlineKeyboardButton(text="✅ Взял", callback_data=f"fl_take:{order_id}"),
            InlineKeyboardButton(text="❌ Не подходит", callback_data=f"fl_skip:{order_id}"),
        ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _sort_key_for_cap(sc) -> tuple:
    # Без AI считаем «средним» (5), чтобы не вытеснять оценённые
    return (sc.fit if sc.fit is not None else 5, sc.order.order_id)


async def _send_order(sc):
    """Отправляет один заказ, при отказе Telegram пробует упрощённые варианты.

    Возвращает message_id. TelegramBadRequest на всех вариантах пробрасывается
    (заказ уйдёт в pending), TelegramRetryAfter и сетевые ошибки - тоже.
    """
    o = sc.order
    variants = []
    try:
        variants.append((format_order(sc), "HTML", _order_kb(o.order_id, o.url)))
    except Exception:
        logging.exception(f"[FREELANCE] ошибка форматирования {o.url}")
    variants.append((format_order_plain(sc), None, _order_kb(o.order_id, o.url)))
    # последний шанс: без кнопки-ссылки (URL мог оказаться невалидным для Telegram)
    variants.append((format_order_plain(sc), None, _order_kb(o.order_id, None)))
    last_exc: Exception | None = None
    for text, parse_mode, markup in variants:
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
    запоминается в памяти, и finish_cycle запишет его вместо «sent»."""
    if await asyncio.to_thread(freelance_set_status, order_id, status):
        return
    if await asyncio.to_thread(mark_sent_status, order_id, status):
        return
    # запись могла появиться между двумя проверками
    await asyncio.to_thread(freelance_set_status, order_id, status)


async def _deliver_freelance(res, progress: dict | None = None) -> dict:
    """Отправляет заказы цикла и сразу сохраняет каждый успешно отправленный.

    Ни один заказ не теряется: ошибка форматирования или отказ Telegram приводят к
    запасному формату (обычный текст), а если и он не прошёл, как и сетевой сбой
    или лимиты Telegram, - к retry (статус pending, повтор в следующем цикле).
    """
    ranked = sorted(res.to_send, key=_sort_key_for_cap, reverse=True)
    chosen = sorted(ranked[:FREELANCE_MAX_PER_CYCLE], key=lambda sc: sc.order.order_id)
    extra = ranked[FREELANCE_MAX_PER_CYCLE:]

    # progress заполняется по ходу: при неожиданном сбое вызывающий знает, что уже ушло
    progress = progress if progress is not None else {}
    sent_ok = progress.setdefault("sent", [])
    retry = progress.setdefault("retry", [])
    skipped = progress.setdefault("skipped", [])
    flood = False
    for sc in chosen:
        if flood:
            retry.append(sc)
            continue
        try:
            message_id = await _send_order(sc)
        except TelegramRetryAfter as e:
            logging.warning(f"[FREELANCE] flood control, ждём {e.retry_after} с")
            await asyncio.sleep(min(e.retry_after, 30))
            retry.append(sc)
            flood = True
            continue
        except Exception:
            logging.exception(f"[FREELANCE] Не удалось отправить {sc.order.url}")
            retry.append(sc)
            continue
        sent_ok.append(sc)
        # запись с повторами; при неудаче заказ остаётся в памяти (_sent_ids) и
        # дописывается в finish_cycle, поэтому дубля не будет
        saved = await asyncio.to_thread(save_sent, sc, message_id)
        if not saved:
            logging.error(f"[FREELANCE] отправленный заказ не записан в БД: {sc.order.url}")
        await asyncio.sleep(0.5)

    if extra:
        extra_sorted = sorted(extra, key=_sort_key_for_cap, reverse=True)
        header = f"➕ Ещё {len(extra)} подходящих заказов (не показаны из-за лимита):"
        items = []
        for sc in extra_sorted:
            fit = f"{sc.fit}/10 " if sc.fit is not None else ""
            items.append((sc, (
                f'• {fit}<a href="{html.escape(sc.order.url or "")}">'
                f"{html.escape((sc.order.title or '')[:80])}</a>"
            )))
        # Все заказы сводки, порезанные на сообщения по лимиту; обработанными
        # отмечаем только реально показанные
        groups, cur, cur_len = [], [], len(header)
        for sc, line in items:
            if cur and cur_len + len(line) + 1 > MSG_CHAR_LIMIT:
                groups.append(cur)
                cur, cur_len = [], 0
            cur.append((sc, line))
            cur_len += len(line) + 1
        if cur:
            groups.append(cur)
        failed_from = None
        for gi, group in enumerate(groups):
            text = "\n".join(([header] if gi == 0 else []) + [line for _, line in group])
            try:
                await bot.send_message(
                    chat_id=MY_CHAT_ID, text=text, parse_mode="HTML",
                    disable_web_page_preview=True,
                )
                skipped.extend(sc for sc, _ in group)
            except Exception:
                logging.exception("[FREELANCE] Не удалось отправить сводку")
                failed_from = gi
                break
        if failed_from is not None:
            # непоказанные заказы не теряем: pending, повтор в следующем цикле
            for group in groups[failed_from:]:
                retry.extend(sc for sc, _ in group)
    return {"sent": sent_ok, "skipped": skipped, "retry": retry}


async def check_freelance(manual: bool = False) -> dict | None:
    """Проверка фриланс-заказов. None — пропущена (пауза или уже идёт)."""
    result = None
    try:
        result = await _check_freelance_impl(manual)
        return result
    finally:
        if result is not None:  # цикл реально выполнялся
            try:
                await asyncio.to_thread(memwatch.trim)
                memwatch.log_mem("фриланс")
            except Exception:
                logging.exception("[MEM] trim после фриланса не удался")


async def _check_freelance_impl(manual: bool = False) -> dict | None:
    if not manual and not await _freelance_subscribed():
        logging.info("[FREELANCE] пауза, пропуск")
        return None
    if _freelance_lock.locked():
        logging.info("[FREELANCE] проверка уже идёт, пропуск")
        return None

    async with _freelance_lock:
        try:
            res = await asyncio.wait_for(
                asyncio.to_thread(prepare_cycle), timeout=PREPARE_TIMEOUT_SEC
            )
        except Exception:
            logging.exception("[FREELANCE] ошибка проверки")
            return {"error": "сбой проверки", "sent": 0, "extra": 0}

        for alert in monitor.pop_alerts():
            try:
                await bot.send_message(chat_id=MY_CHAT_ID, text=alert, parse_mode="HTML")
            except Exception:
                logging.exception("[BOT] Не удалось отправить health-оповещение")

        if res.error:
            return {"error": res.error, "sent": 0, "extra": 0}

        progress: dict = {}
        try:
            d = await _deliver_freelance(res, progress)
        except Exception:
            # неожиданный сбой: все неотправленные уходят в pending, а не теряются
            logging.exception("[FREELANCE] _deliver_freelance упал")
            handled = {sc.order.order_id for key in ("sent", "skipped", "retry")
                       for sc in progress.get(key, [])}
            d = {
                "sent": progress.get("sent", []),
                "skipped": progress.get("skipped", []),
                "retry": progress.get("retry", []) + [
                    sc for sc in res.to_send if sc.order.order_id not in handled],
            }
        try:
            await asyncio.to_thread(
                finish_cycle, res, d["sent"], d["skipped"], d["retry"]
            )
        except Exception:
            logging.exception("[FREELANCE] finish_cycle упал")
            return {"error": "не удалось сохранить итог цикла", "sent": len(d["sent"]), "extra": 0}
        return {"error": "", "sent": len(d["sent"]), "extra": len(d["skipped"]),
                "failed": len(d["retry"]), "scanned": res.raw_count}


def _ai_state_text(st: dict) -> str:
    """Строка статуса AI: активная Gemini-модель, запасные, Yandex, последняя оценка."""
    if not st["enabled"]:
        return "выключен (нет ключей), заказы идут без оценки"
    parts = []
    if st["gemini"]:
        models = st["models"]
        if models:
            parts.append(f"Gemini: {html.escape(models[0])} (запасные: {len(models) - 1})")
        else:
            parts.append("Gemini: все модели недоступны")
        if st["paused"]:
            paused = ", ".join(
                f"{html.escape(m)} {sec // 3600} ч {sec % 3600 // 60} мин" if sec >= 3600
                else f"{html.escape(m)} {sec} с"
                for m, sec in st["paused"].items()
            )
            parts.append(f"на паузе (лимит): {paused}")
    if st["yandex"]:
        parts.append("+ Yandex lite")
    text = "; ".join(parts)
    last = st["last_ok"]
    if last:
        try:
            at = datetime.fromisoformat(last["at"]).astimezone(MSK).strftime("%d.%m %H:%M")
        except ValueError:
            at = last["at"]
        text += f"\nПоследняя оценка: {at} ({html.escape(last['model'])})"
    else:
        text += "\nПоследняя оценка: ещё не было"
    if st["config_error"]:
        text += f"\n🔴 Ошибка настройки: {html.escape(st['config_error'])}"
    return text


def _freelance_status_text() -> str:
    """Собирает статус (синхронно, с чтением БД) — вызывать через to_thread."""
    st = freelance_stats()
    last_id = kv_get(LAST_ID_KEY, "—")
    last_raw = kv_get("freelance_last_raw", "—")
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
        f"Заказов в последнем скане: {last_raw}",
        f"Прошли префильтр за сутки: {st['seen_24h']}",
        f"Отправлено за сутки: {st['sent_24h']}",
        f"Последняя успешная проверка: {last_ok or 'ещё не было'}",
        f"🤖 AI: {ai_state}",
        f"Порог fit: {FREELANCE_MIN_FIT}, интервал: {FREELANCE_INTERVAL_MINUTES} мин",
    ]
    streak = monitor.streak.get("alot", 0)
    if streak:
        reason = html.escape(monitor.last_reason.get("alot", ""))
        lines.append(f"🔴 alot.pro: неудачных проверок подряд: {streak} ({reason})")
    else:
        lines.append("🟢 alot.pro: работает")
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
        elif r["error"]:
            text = f"⚠️ Ошибка проверки: {html.escape(r['error'])}"
        else:
            text = f"✅ Готово. Новых заказов: {r['sent']}"
            if r["extra"]:
                text += f" (+{r['extra']} в сводке)"
            if r.get("failed"):
                text += f", не отправлено: {r['failed']} (повтор в следующий раз)"
    except Exception:
        logging.exception("[FREELANCE] fl_check")
        text = "⚠️ Проверка завершилась ошибкой, подробности в логе."
    await call.message.answer(text, parse_mode="HTML")


def _order_id_from(data: str) -> int | None:
    try:
        return int(data.split(":", 1)[1])
    except (IndexError, ValueError):
        return None


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
        markup = call.message.reply_markup if call.message else None
        for row in (markup.inline_keyboard if markup else []):
            for btn in row:
                if btn.url:
                    url = btn.url
        await call.message.edit_reply_markup(reply_markup=_order_kb(order_id, url, taken=True))
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


@dp.callback_query(F.data.startswith("fl_noop:"))
async def cb_fl_noop(call: CallbackQuery):
    await call.answer("Заказ уже в работе")


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
            await asyncio.to_thread(freelance_set_status, row["order_id"], "expired")
            await asyncio.sleep(0.1)
        if rows:
            logging.info(f"[FREELANCE] очистка: найдено {len(rows)}, удалено {deleted}")
    except Exception:
        logging.exception("[FREELANCE] cleanup_freelance_messages")


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
    global subscribed
    subscribed = True

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
        f"/reset — очистить базу и загрузить заново\n"
        f"/stop — приостановить\n\n"
        f"Пользуйся кнопками внизу 👇",
        parse_mode="HTML",
        reply_markup=main_reply_kb(),
    )

    asyncio.create_task(check_vacancies())


@dp.message(Command("stop"))
async def cmd_stop(message: Message):
    global subscribed
    subscribed = False
    await message.answer(
        "⏸ Уведомления приостановлены.\n\nНажмите «▶️ Возобновить» или /start.",
        reply_markup=main_reply_kb(),
    )


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
    parsers ={k: v for k, v in monitor.streaks().items() if k not in FREELANCE_HEALTH_SOURCES}
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


# ---------- Точка входа ----------

async def main():
    init_db()

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
    scheduler.add_job(
        check_freelance,
        "interval",
        minutes=FREELANCE_INTERVAL_MINUTES,
    )
    scheduler.add_job(
        cleanup_freelance_messages,
        "interval",
        hours=1,
    )
    scheduler.start()
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