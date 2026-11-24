"""Дешёвый префильтр фриланс-заказов (до обращения к YandexGPT)."""
import re

from freelance.language import UKRAINIAN_REASON, is_ukrainian
from parsers.filters import _compile

# Вакансионные биржи — не фриланс
EXCLUDED_SITES = {
    "hhru", "habrcareer", "geekjobru", "trudvsemru", "superjobru",
    "getmatchru", "remotejobru", "kadrofru", "joblabru", "workspaceru",
    "hirehiru",
    # Англоязычные доски удалённых вакансий (полная занятость, не заказы). По базе сервера
    # 30.10-01.11.2026: 241 из 554 прошедших префильтр, средний fit 0.1, отправлено 3 -
    # занимали ~40% квоты AI, пока заказы FL.ru/Kwork/Freelancehunt истекали без оценки
    "trulyremotework", "himalayas", "weworkremotelycom",
}

# Категории alot.pro, относящиеся к разработке
DEV_CATEGORIES = {
    "WEB", "PROG_OTHER", "MOBILE", "SYSADMIN", "PROG_GAMES",
    "TESTING", "INTERFACE_DESIGN",
}

# ИИ-агенты и ИИ-автоматизация входят в профиль (нейросети внутри ботов и автоматизаций). Одно слово
# «агент» не берём: «агент по недвижимости» - не наш заказ; живая выборка 2026-11-13 потеряла
# «Создать ai агента» без этих оборотов
AGENT_WORDS = ["ai агент", "ai-агент", "ии агент", "ии-агент", "ai agent", "ai-agent", "нейро-агент"]

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
    "таблиц", "apps script", "формул", "vba", "отчет", "отчёт", "дашборд",
    "wildberries", "вайлдберриз", "ozon", "озон", "маркетплейс", "n8n", "sql",
    "нейросет", "gpt",
    *AGENT_WORDS,
]

# Профиль исполнителя - таблицы, боты, автоматизация; сайты не делает. Заказ, где в названии
# сайт, а ни в названии, ни в начале текста нет ничего из профиля, отсеивается до AI
# (по базе сервера 30.10-01.11.2026 сайты - больше половины заказов, ушедших на оценку).
# Сайт как источник данных («парсер сайта») или форма сайта в таблицу/бот/CRM - проходят.
SITE_WORDS = [
    "сайт", "лендинг", "landing", "интернет-магазин", "веб-приложен",
    "wordpress", "вордпресс", "tilda", "тильд", "opencart", "вёрстк", "верстк",
    "сверстать", "html", "css",
]
STRENGTH_WORDS = [
    "бот", "бота", "боты", "ботов", "боту", "ботом", "чат-бот", "чатбот",
    "telegram", "телеграм", "тг", "tg", "whatsapp", "вотсап",
    "парсер", "парсинг", "спарсить", "скрапинг", "выгрузк", "скрипт", "python", "питон",
    "автоматизац", "api", "интеграц", "crm", "amocrm", "амо",
    "таблиц", "google sheets", "excel", "эксел", "apps script", "формул", "макрос",
    "отчет", "отчёт", "дашборд", "wildberries", "вайлдберриз", "ozon", "озон",
    "маркетплейс", "n8n", "sql", "нейросет", "gpt",
    *AGENT_WORDS,
]

_INCLUDE_RE = _compile(INCLUDE_WORDS)
_SITE_RE = _compile(SITE_WORDS)
_STRENGTH_RE = _compile(STRENGTH_WORDS)
_PRIORITY_RE = _compile([word for word in STRENGTH_WORDS
                        if word not in {"wildberries", "вайлдберриз", "ozon", "озон", "маркетплейс"}])
_MARKETPLACE_VISUAL_RE = re.compile(r"инфографик|(?:карточк|товарн\w*\s+карт).{0,90}"
                                    r"(?:маркетплейс|wildberries|вайлдберриз|\bwb\b|ozon|озон)", re.I)
_VISUAL_AUTOMATION_RE = _compile(["автоматизац", "автоматизир", "генератор", "массовая генерац",
                                 "генерация карточек по api", "интеграц", "api", "скрипт", "парсер"])

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
# на разработку («бот для менеджера», «парсер отзывов») и заказ на автоматизацию с таблицами
# («Аналитика продаж WB в Google Таблицах», «Автоматизация отчёта для менеджера в Google Sheets»,
# «Перевод данных из Excel в Google Sheets»). Если в названии есть явный признак разработки
# (DEV_INTENT_WORDS) либо инструмент или результат автоматизации (RESULT_WORDS), а в названии
# и начале текста нет признаков найма (EMPLOYMENT_WORDS), такое слово заказ не отсеивает.
SOFT_EXCLUDE_WORDS = ("менеджер", "аналитик", "отзыв", "перевод")
DEV_INTENT_WORDS = [
    "бот", "бота", "боты", "ботов", "боту", "ботом", "чат-бот", "чатбот", "тг", "tg",
    "парсер", "парсинг", "скрипт", "python", "питон", "api", "разработ", "программист",
    *AGENT_WORDS,
]
# Результат автоматизации и инструменты данных - по смыслу задачи, не только по названию технологии.
# Название - сознательная формулировка заказчика, поэтому там работает широкий список (RESULT_TITLE_WORDS);
# в тексте широкие слова шумят («автоматическая актуализация» у сайта, «синхронизация» в видеомонтаже -
# проверено на живой выборке 2026-11-13), поэтому там - только узкий (RESULT_BODY_WORDS)
RESULT_BODY_WORDS = [
    "google таблиц", "гугл таблиц", "google sheets", "гугл sheets", "google-таблиц", "в таблиц",
    "apps script", "appscript", "sql", "power bi", "дашборд", "vba", "формул", "макрос",
    "выгрузк", "импорт данных", "перенос данных", "перенести данные", "переносить данные",
    "сбор данных", "собирать данные", "формирование отчет", "формирование отчёт",
    "формировать отчет", "формировать отчёт", "обработка данных", "обработк данных",
]
RESULT_TITLE_WORDS = RESULT_BODY_WORDS + [
    "таблиц", "excel", "эксел", "автоматизац", "автоматизир", "автоматическ", "выгруж", "синхрониз",
    "подтягив", "импорт из", "собирал", "сводн", "интеграц",
]
# Признаки найма в штат и набора персонала: заказ-аналитик «в отдел, оклад» и «ищем менеджеров» -
# не заказы
EMPLOYMENT_WORDS = [
    "в штат", "вакансия", "на постоянн", "полная занятость", "полный день", "оклад", "трудоустро",
    "зарплат", "з/п", "график работы", "резюме", "full-time", "fulltime", "full time", "в офис",
    "испытательный срок", "соцпакет", "дмс", "тк рф", "опыт работы от",
    "ищем", "требуется", "требуются", "приглашаем", "набираем", "в команду",
]

_EXCLUDE_RE = _compile(EXCLUDE_WORDS)
_DEV_INTENT_RE = _compile(DEV_INTENT_WORDS)
_RESULT_BODY_RE = _compile(RESULT_BODY_WORDS)
_RESULT_TITLE_RE = _compile(RESULT_TITLE_WORDS)
_EMPLOYMENT_RE = _compile(EMPLOYMENT_WORDS)


def _result_signal(title: str, body: str = "") -> bool:
    """Признак профиля/результата автоматизации: широкий список в названии, узкий в начале текста."""
    return bool(_RESULT_TITLE_RE.search(title) or _RESULT_BODY_RE.search((body or "")[:600]))

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


def _soft_stop_overridden(title: str, body: str) -> bool:
    """Широкое стоп-слово («менеджер», «аналитик», «перевод», «отзыв») не отсеивает заказ, если в
    названии разработка или инструмент/результат автоматизации, а найма в штат не видно."""
    if _DEV_INTENT_RE.search(title):
        return True
    return bool(_RESULT_TITLE_RE.search(title)) and not _EMPLOYMENT_RE.search(f"{title}\n{(body or '')[:600]}")


def reject_reason(order) -> str | None:
    """Почему префильтр отсеял заказ (None - прошёл). Причина нужна для лога и журнала: иначе
    потерянный заказ нельзя отличить от заказа, которого не было в выдаче alot.
    Коды: excluded_site, reports, service_offer, stop_word:<слово>, site_only, no_include."""
    if order.site in EXCLUDED_SITES:
        return "excluded_site"
    if order.is_suspicious:
        return "reports"
    title = order.title or ""
    if is_ukrainian(title, order.body):
        return UKRAINIAN_REASON
    if is_service_offer(title):
        return "service_offer"
    head = f"{title}\n{(order.body or '')[:600]}"
    visual = _MARKETPLACE_VISUAL_RE.search(title)
    if visual and not _VISUAL_AUTOMATION_RE.search(f"{title}\n{order.body or ''}"):
        return "marketplace_graphics"
    # «Вёрстка по готовому дизайну» - заказ на разработку: дизайн тут дан, а не заказывается
    stop_text = _DESIGN_GIVEN_RE.sub(" ", title)
    hits = [m.group(0).lower() for m in _EXCLUDE_RE.finditer(stop_text)]
    hard = [w for w in hits if not w.startswith(SOFT_EXCLUDE_WORDS)]
    if hard:
        return f"stop_word:{hard[0]}"
    if hits and not _soft_stop_overridden(title, order.body):
        return f"stop_word:{hits[0]}"
    if _SITE_RE.search(title) and not (_STRENGTH_RE.search(head) or _result_signal(title, order.body)):
        return "site_only"
    if DEV_CATEGORIES.intersection(order.categories):
        return None
    if visual or _INCLUDE_RE.search(head) or _result_signal(title, order.body):
        return None
    return "no_include"


def is_candidate(order) -> bool:
    """Проходит ли заказ префильтр (бюджет не фильтруем)."""
    return reject_reason(order) is None


# Категории alot.pro, которые сами по себе говорят о профиле. WEB сюда не входит: сайты - не профиль
# (преимущество общей категории WEB в очереди AI вытесняло таблицы и ботов)
PROFILE_CATEGORIES = {"PROG_OTHER"}


def priority(order) -> int:
    """Очередь AI-оценки, когда квоты мало (меньше - важнее). Приоритет - по профилю
    (таблицы, боты, парсеры, скрипты, автоматизация), а не по общей категории разработки:
    1 - признак профиля или результата автоматизации в названии,
    2 - признак профиля только в начале текста либо категория профиля (PROG_OTHER),
    3 - прошёл префильтр иначе (например, категория WEB без слов профиля)."""
    title = order.title or ""
    head = f"{title}\n{(order.body or '')[:600]}"
    if _PRIORITY_RE.search(title) or _RESULT_TITLE_RE.search(title):
        return 1
    if _PRIORITY_RE.search(head) or _RESULT_BODY_RE.search((order.body or "")[:600]) \
            or PROFILE_CATEGORIES.intersection(order.categories):
        return 2
    return 3


def norm_dedup_key(order) -> str:
    """Ключ дубля: сайт + название + начало текста (YouDo плодит одинаковые заказы)."""
    def n(s: str) -> str:
        return re.sub(r"[^a-zа-я0-9]+", " ", (s or "").lower().replace("ё", "е")).strip()
    return f"{order.site}|{n(order.title)}|{n((order.body or '')[:100])}"
