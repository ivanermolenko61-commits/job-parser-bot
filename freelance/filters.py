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
    "telegram", "телеграм", "тг", "tg", "whatsapp", "вотсап",
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

# Широкие стоп-слова: срабатывают на «менеджер по продажам», но не должны глушить заказ
# на разработку («бот для менеджера», «парсер отзывов»). Если в названии есть явный признак
# разработки (DEV_INTENT_WORDS), такое слово заказ не отсеивает.
SOFT_EXCLUDE_WORDS = ("менеджер", "аналитик", "отзыв", "перевод")
DEV_INTENT_WORDS = [
    "бот", "бота", "боты", "ботов", "боту", "ботом", "чат-бот", "чатбот", "тг", "tg",
    "парсер", "парсинг", "скрипт", "python", "питон", "api", "разработ", "программист",
]

_INCLUDE_RE = _compile(INCLUDE_WORDS)
_EXCLUDE_RE = _compile(EXCLUDE_WORDS)
_DEV_INTENT_RE = _compile(DEV_INTENT_WORDS)

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


def reject_reason(order) -> str | None:
    """Почему префильтр отсеял заказ (None - прошёл). Причина нужна для лога: иначе
    потерянный заказ нельзя отличить от заказа, которого не было в выдаче alot.
    Коды: excluded_site, reports, service_offer, stop_word:<слово>, no_include."""
    if order.site in EXCLUDED_SITES:
        return "excluded_site"
    if order.is_suspicious:
        return "reports"
    title = order.title or ""
    if is_service_offer(title):
        return "service_offer"
    # «Вёрстка по готовому дизайну» - заказ на разработку: дизайн тут дан, а не заказывается
    stop_text = _DESIGN_GIVEN_RE.sub(" ", title)
    hits = [m.group(0).lower() for m in _EXCLUDE_RE.finditer(stop_text)]
    hard = [w for w in hits if not w.startswith(SOFT_EXCLUDE_WORDS)]
    if hard:
        return f"stop_word:{hard[0]}"
    if hits and not _DEV_INTENT_RE.search(title):
        return f"stop_word:{hits[0]}"
    if DEV_CATEGORIES.intersection(order.categories):
        return None
    if _INCLUDE_RE.search(f"{title}\n{(order.body or '')[:600]}"):
        return None
    return "no_include"


def is_candidate(order) -> bool:
    """Проходит ли заказ префильтр (бюджет не фильтруем)."""
    return reject_reason(order) is None


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
