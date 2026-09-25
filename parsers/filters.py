"""Общие фильтры вакансий для всех парсеров.

Раньше у каждого парсера были свои копии списков слов, и они разошлись:
«чпу» было только в DreamJob, «битрикс» — только в hh и т.д. Теперь один
источник правды.

Как сравниваются слова (функция _has):
  • короткие слова (≤ 3 символа: go, qa, 1с, nx, hr...) — только целым словом,
    иначе «go» находилось бы внутри «google», а «qa» — внутри других слов;
  • длинные — как начало слова (основа): «разработк» найдёт «разработки»,
    «разработку». Граница слева обязательна: «продаж» не найдётся в «распродажа».
"""
import re

# --- Не наши профессии: вакансия отсекается, даже если там есть «разработчик» ---
EXCLUDE_WORDS = [
    # Менеджмент, продукт, проекты (фразами — одиночное «проектов» резало
    # «Разработчик LLM-проектов», а «продукт» — «в продуктовую команду»)
    "менеджер", "manager", "product owner", "продакт", "владелец продукт",
    "project manager", "руководитель проект", "управлени проект",
    "аналитик", "analyst",
    "дизайнер", "designer",
    "исследователь", "researcher", "research", "ресерчер",
    "qa", "тестировщик", "тестирован", "testing", "test engineer", "quality assurance",
    "devops", "sre", "administrator", "администратор",
    "техподдержк", "технической поддержк", "поддержки пользовател", "support",
    "сопровождени",
    "маркетолог", "marketing", "hr", "рекрутер", "recruiter",
    "sales", "продаж",
    "бухгалтер", "accountant", "юрист", "юрисконсульт", "логист",
    "документами", "документооборот", "делопроизвод",
    "data scientist", "дата-сайентист", "data science",
    "тренер", "coach", "cvm", "cmo",
    "безопасност", "security", "appsec", "infosec",
    "методолог", "seo", "igaming", "гейминг",
    "преподавател", "учитель", "педагог", "репетитор", "воспитател",
    "водитель", "курьер", "продавец", "кассир", "риелтор", "риэлтор",
    # Платформы, которые Ивану не нужны
    "1с", "1c", "1 с", "битрикс", "bitrix", "wordpress",
    # Инженерия «железа» и производства — «программист» там не про веб/бэкенд
    "чпу", "cnc", "станк", "nx", "cad", "cam", "solidworks", "компас-3d",
    "плк", "plc", "асу тп", "асутп", "scada", "кипиа", "контроллер",
    "электроник", "схемотехн", "fpga", "плис", "микроэлектрон",
    "конструктор", "технолог", "техник",
    "сварщик", "электрик", "монтажник",
    "кондитер", "повар", "медицинск", "врач", "медсестр",
]

# --- Уровень выше джуна: отсекается, если в названии НЕТ маркера джуна ---
SENIORITY_WORDS = [
    "senior", "lead", "principal", "middle", "head of", "team lead", "tech lead",
    "teamlead", "тимлид", "director", "architect", "архитектор",
    "руководитель", "ведущий", "ведущая", "ведущее", "старший", "главный",
]

# --- Маркеры джуна: «Junior/Middle», «Стажёр» — наши ---
JUNIOR_WORDS = [
    "junior", "джун", "стажер", "стажёр", "стажировк", "intern", "trainee",
    "младший", "начинающ", "ученик", "без опыта", "entry",
]

# --- IT-слова: хотя бы одно должно быть в названии ---
INCLUDE_IT_WORDS = [
    "python", "java", "go", "golang", "javascript", "typescript",
    "c++", "c#", ".net", "php", "ruby", "swift", "kotlin", "scala", "rust",
    "django", "fastapi", "flask",
    "developer", "разработчик", "программист", "разработк", "software engineer",
    "backend", "back-end", "бэкенд", "frontend", "front-end", "фронтенд",
    "fullstack", "full-stack", "фулстек",
    "ml engineer", "ml-инженер", "ai engineer", "ai-инженер", "llm engineer",
    "data engineer", "data-инженер", "дата-инженер", "дата инженер",
    "инженер данных", "инженер по данным",
    "dba", "embedded", "bios", "bsp", "ios", "android",
]

# Уровень в явном виде (для проверки ALLOWED_LEVELS)
LEVEL_PATTERN = re.compile(
    r"(Junior|Middle|Senior|Lead|Intern|Стажёр|Стажер|Trainee)", re.IGNORECASE,
)

_WORD_CHAR = r"a-zа-яё0-9"


def _compile(words):
    """Одна регулярка на весь список — быстрее, чем проверять слова по одному."""
    parts = []
    for w in words:
        esc = re.escape(w)
        if len(w) <= 3:
            parts.append(rf"(?<![{_WORD_CHAR}]){esc}(?![{_WORD_CHAR}])")
        else:
            parts.append(rf"(?<![{_WORD_CHAR}]){esc}")
    return re.compile("|".join(parts), re.IGNORECASE)


_EXCLUDE_RE = _compile(EXCLUDE_WORDS)
_SENIOR_RE = _compile(SENIORITY_WORDS)
_JUNIOR_RE = _compile(JUNIOR_WORDS)
_IT_RE = _compile(INCLUDE_IT_WORDS)


def is_junior_marked(text: str) -> bool:
    return bool(_JUNIOR_RE.search(text or ""))


def is_relevant_title(title: str) -> bool:
    """Подходит ли вакансия по названию: разработка, уровень до джуна включительно."""
    t = (title or "").lower()
    if _EXCLUDE_RE.search(t):
        return False
    # «Junior/Middle Python» — наша; «Middle Python» — нет
    if _SENIOR_RE.search(t) and not _JUNIOR_RE.search(t):
        return False
    return bool(_IT_RE.search(t))


def is_allowed_level(text: str) -> bool:
    """Уровень из названия (или мета-строки Хабра): пусто или джун — ок.

    Раньше бралось только ПЕРВОЕ найденное слово уровня, и «Middle/Junior»
    отсекалось, а «Junior/Middle» проходило. Теперь маркер джуна где угодно — ок.
    """
    if is_junior_marked(text):
        return True
    return LEVEL_PATTERN.search(text or "") is None


# --- География: вакансии не из России ---
GEO_BLACKLIST = [
    "ташкент", "алматы", "астана", "нур-султан",
    "тбилиси", "баку", "ереван", "бишкек", "минск", "брест",
    "гомель", "витебск", "гродно", "могилёв", "могилев",
    "душанбе", "ашхабад", "кишинёв", "кишинев", "киев",
    "львов", "харьков", "одесса", "днепр",
    "казахстан", "узбекистан", "грузия", "азербайджан",
    "армения", "киргизия", "кыргызстан", "беларусь",
    "белоруссия", "таджикистан", "туркменистан", "молдова", "украина",
]


def is_russian_location(location: str) -> bool:
    if not location:
        return True
    loc = location.lower()
    return not any(city in loc for city in GEO_BLACKLIST)
