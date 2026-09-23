"""Настройки бота: ключевые слова, фильтры, интервал."""
import os

from dotenv import load_dotenv

load_dotenv()

# --- Telegram ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
MY_CHAT_ID = int(os.getenv("MY_CHAT_ID", "0"))

# --- Поиск вакансий ---

# Запросы для лёгких парсеров (requests + BeautifulSoup):
# Habr, DreamJob. Дёшево по RAM — можно не экономить.
QUERIES = [
    "Python",
    "стажер",
    "junior",
    "trainee",
    "разработчик",
]

# Запросы для Playwright-парсеров (hh.ru, GeekJob).
# Каждый запрос — это полная загрузка SPA в Chromium (~250-450 МБ RAM).
# 2 запроса вместо 5 дают ~60% экономии памяти на этих парсерах,
# а сами сайты и так хорошо фильтруют по тексту запроса.
PLAYWRIGHT_QUERIES = [
    "разработчик",
    "junior",
]

# Сколько страниц забирать на каждый запрос (1 страница = 25 карточек)
MAX_PAGES_PER_QUERY = 1

# Удалёнка — приоритет, но не строго
REMOTE_PRIORITY = True

# Пауза между запросами (сек) — защита от бана
REQUEST_DELAY = 1.0

# --- Интервал проверки ---
CHECK_INTERVAL_MINUTES = 15

# --- База данных ---
DB_PATH = "vacancies.db"