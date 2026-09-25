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
PLAYWRIGHT_QUERIES = [
    "разработчик",
    "junior",
    "python",
    "стажер",
]

# Сколько страниц забирать на каждый запрос (1 страница = 25 карточек)
MAX_PAGES_PER_QUERY = 1

# Удалёнка — приоритет, но не строго
REMOTE_PRIORITY = True

# Пауза между запросами (сек) — защита от бана
REQUEST_DELAY = 1.0

# --- Фильтр свежести ---
# Вакансии старше этого возраста не отправляются и не попадают в БД.
# Проверяется по published_at (дате публикации с сайта).
# Если у вакансии нет даты публикации — она пропускается как есть.
MAX_VACANCY_AGE_DAYS = 5

# Сколько дней помнить, что вакансия уже отправлялась. Дольше, чем хранятся
# сами вакансии: у DreamJob нет даты публикации, и без этой памяти вакансия
# приходила бы повторно после очистки БД, пока висит на сайте.
SENT_HISTORY_DAYS = 60

# --- Интервал проверки ---
CHECK_INTERVAL_MINUTES = 15

# --- База данных ---
DB_PATH = "vacancies.db"