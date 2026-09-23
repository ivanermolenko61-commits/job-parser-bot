"""Telegram-бот: мониторинг вакансий Junior/стажёр по Python/backend."""
import asyncio
import logging
import os
from datetime import datetime

from aiogram import Bot, Dispatcher
from aiogram.filters import Command, CommandStart
from aiogram.types import Message
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

# Подписка: True = пользователь получает уведомления
subscribed = True


# ---------- Проверка вакансий ----------

async def check_vacancies():
    """Забирает новые вакансии и отправляет их в Telegram."""
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
                disable_web_page_preview=False,
            )
            sent_count += 1
            # Небольшая задержка, чтобы не флудить Telegram
            await asyncio.sleep(0.5)
        except Exception:
            logging.exception(f"[BOT] Не удалось отправить {v.url}")

    # Помечаем в БД только те, что реально ушли
    if sent_count:
        mark_vacancies_sent(new_vacancies[:sent_count])


# ---------- Команды ----------

@dp.message(CommandStart())
async def cmd_start(message: Message):
    global subscribed
    subscribed = True

    await message.answer(
        f"👋 Привет!\n\n"
        f"Я слежу за новыми вакансиями <b>Junior/стажёр</b> по <b>Python/backend</b> "
        f"с удалённой работой.\n\n"
        f"⏱ Проверка каждые <b>{CHECK_INTERVAL_MINUTES} минут</b>.\n"
        f"Как только появится новая — пришлю ссылку.\n\n"
        f"<b>Команды:</b>\n"
        f"/status — статистика и текущие фильтры\n"
        f"/check — проверить вакансии прямо сейчас\n"
        f"/stop — приостановить уведомления\n"
        f"/start — возобновить",
        parse_mode="HTML",
    )

    # Запустим первую проверку сразу
    await check_vacancies()


@dp.message(Command("stop"))
async def cmd_stop(message: Message):
    global subscribed
    subscribed = False

    await message.answer(
        "⏸ Уведомления приостановлены.\n\n"
        "Чтобы возобновить — отправьте /start."
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

    await message.answer("\n".join(lines), parse_mode="HTML")


@dp.message(Command("check"))
async def cmd_check(message: Message):
    await message.answer("🔍 Проверяю вакансии…")
    await check_vacancies()
    await message.answer("✅ Проверка завершена.")


# ---------- Точка входа ----------

async def main():
    # Инициализация БД
    init_db()

    # Планировщик
    scheduler.add_job(
        check_vacancies,
        "interval",
        minutes=CHECK_INTERVAL_MINUTES,
    )
    scheduler.start()
    logging.info(f"Планировщик запущен: проверка каждые {CHECK_INTERVAL_MINUTES} мин")

    logging.info(f"Бот запущен. Разрешён только user_id={MY_CHAT_ID}")

    # Уведомим владельца при старте
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