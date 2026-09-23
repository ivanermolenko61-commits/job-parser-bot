"""Настройки бота: ключевые слова, фильтры, интервал."""
import os

from dotenv import load_dotenv

load_dotenv()

# --- Telegram ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
MY_CHAT_ID = int(os.getenv("MY_CHAT_ID", "0"))

# --- Поиск вакансий ---

# Ключевые слова (для поиска на Хабр Карьере)
KEYWORDS = ["Python", "Junior", "стажер", "стажёр", "trainee", "backend", "разработчик"]

# Только удалённая работа
REMOTE_ONLY = True

# Пауза между запросами (сек) — защита от бана
REQUEST_DELAY = 1.0

# --- Интервал проверки ---
CHECK_INTERVAL_MINUTES = 15

# --- База данных ---
DB_PATH = "vacancies.db"