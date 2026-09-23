"""Telegram-бот: мониторинг вакансий Junior/стажёр по разработке."""
import asyncio
import logging
import os
from datetime import datetime
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    TelegramObject,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv

from config import CHECK_INTERVAL_MINUTES
from database import (
    cleanup_old,
    get_recent_vacancies,
    init_db,
    reset_db,
    stats,
)
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

# Сколько вакансий показывать в /list по умолчанию
DEFAULT_LIST_LIMIT = 30

# Максимальный лимит (защита от слишком длинных списков)
MAX_LIST_LIMIT = 100

# Сколько символов помещается в одно сообщение Telegram (запас от 4096)
MSG_CHAR_LIMIT = 3500


# ---------- Клавиатура ----------

def main_reply_kb():
    """Reply-клавиатура внизу экрана."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📋 Список"), KeyboardButton(text="📊 Статус")],
            [KeyboardButton(text="🔍 Проверить сейчас")],
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


async def check_vacancies():
    """Запускает парсеры и отправляет новые вакансии. Защищена от параллельных запусков."""
    if not subscribed:
        logging.info("[BOT] Подписка отключена, пропуск проверки")
        return

    if _check_lock.locked():
        logging.info("[BOT] Проверка уже идёт, пропуск")
        return

    async with _check_lock:
        logging.info("[BOT] Проверка новых вакансий...")

        try:
            new_vacancies = fetch_new_vacancies()
        except Exception:
            logging.exception("[BOT] Ошибка при получении вакансий")
            return

        if not new_vacancies:
            logging.info("[BOT] Новых вакансий нет")
            return

        logging.info(f"[BOT] Найдено новых: {len(new_vacancies)}")

        sent_count = 0
        for v in new_vacancies:
            try:
                await bot.send_message(
                    chat_id=MY_CHAT_ID,
                    text=v.format_message(),
                    parse_mode="HTML",
                )
                sent_count += 1
                await asyncio.sleep(0.5)
            except Exception:
                logging.exception(f"[BOT] Не удалось отправить {v.url}")

        if sent_count:
            mark_vacancies_sent(new_vacancies[:sent_count])


# ---------- Команды ----------

@dp.message(CommandStart())
async def cmd_start(message: Message):
    global subscribed
    subscribed = True

    await message.answer(
        f"👋 Привет!\n\n"
        f"Слежу за новыми вакансиями <b>Junior/стажёр</b> по разработке.\n"
        f"Удалёнка в приоритете, но беру и офисные.\n\n"
        f"⏱ Проверка каждые <b>{CHECK_INTERVAL_MINUTES} мин</b>.\n\n"
        f"<b>Команды:</b>\n"
        f"/list — последние {DEFAULT_LIST_LIMIT} вакансий\n"
        f"/list 50 — последние 50\n"
        f"/status — статистика\n"
        f"/check — проверить сейчас\n"
        f"/reset — очистить базу и загрузить заново\n"
        f"/stop — приостановить\n\n"
        f"Пользуйся кнопками внизу 👇",
        parse_mode="HTML",
        reply_markup=main_reply_kb(),
    )

    # Фоновая проверка — не блокирует ответ
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
    lines.append(f"📬 Подписка: {'включена' if subscribed else 'выключена'}")
    await message.answer("\n".join(lines), parse_mode="HTML", reply_markup=main_reply_kb())


@dp.message(Command("list"))
async def cmd_list(message: Message):
    """Показывает последние N вакансий. /list 50 — последние 50."""
    # Разбор аргумента
    text = message.text or ""
    parts = text.split(maxsplit=1)
    limit = DEFAULT_LIST_LIMIT

    if len(parts) > 1:
        try:
            limit = int(parts[1].strip())
            if limit <= 0:
                raise ValueError
            if limit > MAX_LIST_LIMIT:
                limit = MAX_LIST_LIMIT
        except ValueError:
            await message.answer(
                f"⚠️ Не понял число: <code>{parts[1]}</code>\n"
                f"Пример: <code>/list 50</code>",
                parse_mode="HTML",
            )
            return

    vacancies = get_recent_vacancies(limit=limit)

    if not vacancies:
        await message.answer(
            "📭 В базе пока пусто.\n\n"
            "Нажмите «🔍 Проверить сейчас» или отправьте /check.",
            reply_markup=main_reply_kb(),
        )
        return

    lines = [f"📋 <b>Последние {len(vacancies)} вакансий</b> (свежие сверху):\n"]

    for v in vacancies:
        published = v.get("published_at") or ""
        found = v.get("found_at", "")[:16]

        if published:
            time_str = f"🕒 Опубликовано: {_human_date(published)}"
        else:
            time_str = f"🕒 Найдено: {_human_date(found)}"

        lines.append(
            f"{time_str}\n"
            f"• <b>{v['title']}</b>\n"
            f"  {v['company']}\n"
            f"  <a href='{v['url']}'>Открыть вакансию</a>\n"
        )

    full_text = "\n".join(lines)

    # Разбиваем на несколько сообщений, если не влезает
    for chunk in _split_messages(full_text):
        await message.answer(chunk, parse_mode="HTML")

    # Клавиатуру добавляем только к последнему сообщению
    await message.answer("— Конец списка —", reply_markup=main_reply_kb())


@dp.message(Command("check"))
async def cmd_check(message: Message):
    await message.answer("🔍 Проверяю вакансии…")
    await check_vacancies()
    await message.answer("✅ Проверка завершена.", reply_markup=main_reply_kb())


@dp.message(Command("reset"))
async def cmd_reset(message: Message):
    """Очищает базу и заново загружает вакансии с правильными датами."""
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
    await cmd_list(message)


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

    deleted = cleanup_old(days=30)
    logging.info(f"Очистка БД: удалено {deleted} записей старше 30 дней")

    scheduler.add_job(
        lambda: logging.info(
            f"Очистка БД: удалено {cleanup_old(days=30)} записей"
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