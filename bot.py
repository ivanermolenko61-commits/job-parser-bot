"""Telegram-бот: мониторинг вакансий Junior/стажёр по Python."""
import asyncio
import logging
import os
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, TelegramObject
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv

from config import CHECK_INTERVAL_MINUTES
from database import init_db, stats
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


# ---------- Middleware: доступ только для владельца ----------

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

async def check_vacancies():
    if not subscribed:
        logging.info("[BOT] Подписка отключена, пропуск проверки")
        return

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
        f"Слежу за новыми вакансиями <b>Junior/стажёр</b> по <b>Python/backend</b>.\n"
        f"Удалёнка в приоритете, но беру и офисные.\n\n"
        f"⏱ Проверка каждые <b>{CHECK_INTERVAL_MINUTES} мин</b>.\n\n"
        f"<b>Команды:</b>\n"
        f"/status — статистика\n"
        f"/check — проверить сейчас\n"
        f"/stop — приостановить\n"
        f"/start — возобновить",
        parse_mode="HTML",
    )

    await check_vacancies()


@dp.message(Command("stop"))
async def cmd_stop(message: Message):
    global subscribed
    subscribed = False
    await message.answer("⏸ Уведомления приостановлены. /start — возобновить.")


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
    await message.answer("\n".join(lines), parse_mode="HTML")


@dp.message(Command("check"))
async def cmd_check(message: Message):
    await message.answer("🔍 Проверяю вакансии…")
    await check_vacancies()
    await message.answer("✅ Проверка завершена.")


# ---------- Точка входа ----------

async def main():
    init_db()

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
        )
    except Exception:
        logging.exception("Не удалось отправить приветствие")

    await dp.start_polling(bot, drop_pending_updates=True)


if __name__ == "__main__":
    asyncio.run(main())