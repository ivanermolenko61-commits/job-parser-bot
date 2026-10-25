"""Дешёвый префильтр фриланс-заказов (до обращения к YandexGPT)."""
import re

from parsers.filters import _compile

# Вакансионные биржи — не фриланс
EXCLUDED_SITES = {
    "hhru", "habrcareer", "geekjobru", "trudvsemru", "superjobru",
    "getmatchru", "remotejobru", "kadrofru", "joblabru", "workspaceru",
    "hirehiru",
}

# Категории alot.pro, относящиеся к разработке
DEV_CATEGORIES = {
    "WEB", "PROG_OTHER", "MOBILE", "SYSADMIN", "PROG_GAMES",
    "TESTING", "INTERFACE_DESIGN",
}

# Слова-признаки подходящего заказа (в title или начале body).
# Короткие (<=3 символов) _compile ищет целым словом, длинные — по началу слова.
INCLUDE_WORDS = [
    "бот", "бота", "боты", "ботов", "боту", "ботом", "чат-бот", "чатбот",
    "telegram", "телеграм", "whatsapp", "вотсап",
    "сайт", "лендинг", "landing", "интернет-магазин", "веб-приложен",
    "парсер", "парсинг", "скрапинг", "скрипт",
    "python", "питон", "django", "fastapi", "flask", "javascript", "react", "node",
    "wordpress", "вордпресс", "tilda", "тильд", "opencart",
    "автоматизац", "api", "интеграц", "вёрстк", "верстк", "html", "css",
    "google таблиц", "гугл таблиц", "google sheets", "excel", "макрос",
    "crm", "mini app", "мини-апп", "расширение для браузер", "плагин",
    "приложени", "разработ", "программист",
]

# Явно не наше (проверяется по title)
EXCLUDE_WORDS = [
    "1с", "1c", "1 с", "битрикс", "bitrix",
    "заполнить карточки", "заполнение карточек", "наполнение карточек",
    "отзыв", "репетитор", "установить программу", "установка программ",
    "вакансия", "в штат", "полный день", "полная занятость",
    "курсовая", "курсовую", "дипломная", "дипломную", "реферат", "контрольная",
    "копирайт", "рерайт", "перевод", "озвуч", "монтаж видео", "логотип",
    "менеджер", "smm", "смм", "аналитик", "установить лицензию", "установить office",
    "дизайн", "листовк", "seo", "autocad", "баннер", "презентац",
    "накрутка", "подписчик", "лайки", "ремонт", "грузчик", "курьер",
]

_INCLUDE_RE = _compile(INCLUDE_WORDS)
_EXCLUDE_RE = _compile(EXCLUDE_WORDS)

# Дизайн уже есть у заказчика: эти обороты не считаем словом «дизайн» в EXCLUDE_WORDS
_DESIGN_GIVEN_RE = re.compile(
    r"(?:по|из|с|на основе)\s+(?:готов\w+|имеющ\w+|утвержд\w+|своему|моему|нашему)\s+(?:дизайн|макет)\w*"
    r"|(?:готов\w+|имеющ\w+|утвержд\w+)\s+дизайн\w*"
    r"|дизайн\s+(?:уже\s+)?(?:готов|готова|готово|готовы|есть|имеется|утверждён|утвержден|утверждена)\b",
    re.IGNORECASE,
)

# Объявление исполнителя о своих услугах: глагол 1-го лица ед.ч. в начале названия
_SERVICE_OFFER_RE = re.compile(
    r"^[^a-zа-я0-9]*(?:создам|сделаю|разработаю|настрою|напишу|выполню|"
    r"сверстаю|помогу|доработаю|запущу)(?![a-zа-я])"
)


def is_service_offer(title: str) -> bool:
    """Название — реклама исполнителя («Создам бота…»), а не заказ клиента."""
    t = (title or "").lower().replace("ё", "е")
    return bool(_SERVICE_OFFER_RE.match(t))


def is_candidate(order) -> bool:
    """Проходит ли заказ префильтр (бюджет не фильтруем)."""
    if order.site in EXCLUDED_SITES:
        return False
    if order.is_suspicious:
        return False
    title = order.title or ""
    if is_service_offer(title):
        return False
    # «Вёрстка по готовому дизайну» - заказ на разработку: дизайн тут дан, а не заказывается
    if _EXCLUDE_RE.search(_DESIGN_GIVEN_RE.sub(" ", title)):
        return False
    if DEV_CATEGORIES.intersection(order.categories):
        return True
    return bool(_INCLUDE_RE.search(f"{title}\n{(order.body or '')[:600]}"))


def priority(order) -> int:
    """Очередь AI-оценки, когда квоты мало (меньше - важнее):
    1 - категория разработки и ключевое слово в названии,
    2 - что-то одно из двух,
    3 - заказ прошёл префильтр только по словам в тексте.
    На 60 заказах с эталонной оценкой: в группе 1 - 10 из 17 подходящих (fit ≥ 5), в группе 3 - 1 из 17."""
    dev = bool(DEV_CATEGORIES.intersection(order.categories))
    in_title = bool(_INCLUDE_RE.search(order.title or ""))
    if dev and in_title:
        return 1
    if dev or in_title:
        return 2
    return 3


def norm_dedup_key(order) -> str:
    """Ключ дубля: сайт + название + начало текста (YouDo плодит одинаковые заказы)."""
    def n(s: str) -> str:
        return re.sub(r"[^a-zа-я0-9]+", " ", (s or "").lower().replace("ё", "е")).strip()
    return f"{order.site}|{n(order.title)}|{n((order.body or '')[:100])}"
