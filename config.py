"""Настройки бота: ключевые слова, фильтры, интервал."""
import math
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
    # Добавлены 2026-09-25 после прогона по живым сайтам: находили вакансии,
    # которых не было в основных запросах (Python-разработчик в СОГАЗ,
    # Junior Fullstack Python/Django, Разработчик Python (FastAPI) и др.)
    "backend",
    "django",
    "fastapi",
    "младший разработчик",
]

# Запросы для Playwright-парсеров (hh.ru, GeekJob).
# Каждый запрос — это полная загрузка SPA в Chromium (~250-450 МБ RAM).
PLAYWRIGHT_QUERIES = [
    "разработчик",
    "junior",
    "python",
    "стажер",
    "backend",  # страница переиспользуется, поэтому RAM почти не растёт, только время
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

# За сколько дней искать дубль «название+компания» с другой площадки.
# Короче SENT_HISTORY_DAYS: одинаковые вакансии крупных компаний (разные
# команды/города) не должны глохнуть на два месяца.
DUPLICATE_WINDOW_DAYS = 7

# --- Интервал проверки ---
CHECK_INTERVAL_MINUTES = 15

# --- База данных ---
# На хостинге можно вынести базу на постоянный том: DB_PATH=/app/data/vacancies.db
DB_PATH = os.getenv("DB_PATH", "vacancies.db")
# --- Фриланс (alot.pro) ---
# Якорь для поиска верхней границы id (заведомо старше текущих заказов)
ALOT_SEED_ID = 17328000
FREELANCE_INTERVAL_MINUTES = 5
# Минимальная AI-оценка (0-10), с которой заказ отправляется
FREELANCE_MIN_FIT = 5
# Заказы старше этого возраста не отправляются (API alot.pro не отдаёт статус
# заказа, поэтому «мёртвые», уже взятые заказы отсекаем свежестью)
FREELANCE_MAX_AGE_HOURS = 6
# Через сколько часов бот сам удаляет из чата неразобранные сообщения с заказами
FREELANCE_MESSAGE_TTL_HOURS = 24
# Максимум сообщений за один цикл, остальное — сводкой «ещё N»
FREELANCE_MAX_PER_CYCLE = 15

# --- Цена фриланс-заказа (считает код, см. freelance/pricing.py) ---
def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return float(default)


# Ставка за час работы с Claude Code, ₽
def _rate_per_hour() -> float:
    value = _env_float("FREELANCE_RATE_PER_HOUR", 1000)
    return value if math.isfinite(value) and value > 0 else 1000.0


# Минимальная ставка, ₽/ч: ниже не опускается ни ставка по сделкам, ни рынок, ни бюджет заказчика
def _min_rate_per_hour() -> float:
    value = _env_float("FREELANCE_MIN_RATE_PER_HOUR", 775)
    return value if math.isfinite(value) and value >= 0 else 775.0


def _price_spread() -> float:
    value = _env_float("FREELANCE_PRICE_SPREAD", 0.15)
    return value if 0 <= value <= 0.5 else 0.15


def _min_price(cat: str, default: float) -> float:
    value = _env_float(f"FREELANCE_MIN_PRICE_{cat.upper()}", default)
    return value if math.isfinite(value) and value >= 0 else float(default)


FREELANCE_MIN_RATE_PER_HOUR = _min_rate_per_hour()
FREELANCE_RATE_PER_HOUR = max(_rate_per_hour(), FREELANCE_MIN_RATE_PER_HOUR)
# Ширина вилки вокруг рекомендуемой цены (0.15 = ±15%)
FREELANCE_PRICE_SPREAD = _price_spread()
# Минимальная цена по категории, ₽ (ниже не работаем); env: FREELANCE_MIN_PRICE_<КАТЕГОРИЯ>
_MIN_PRICE_DEFAULTS = {
    "tg_bot": 3000, "parser": 1500, "landing": 3000, "site_fix": 1000, "wp": 1500,
    "script": 1500, "integration": 2500, "sheets": 1500, "other": 1500,
}
FREELANCE_MIN_PRICE = {
    cat: _min_price(cat, v)
    for cat, v in _MIN_PRICE_DEFAULTS.items()
}
