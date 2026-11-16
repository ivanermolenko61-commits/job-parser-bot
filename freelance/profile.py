"""Компактный профиль исполнителя: один источник для префильтра, быстрого триажа и основной оценки.

Профиль Ивана (подтверждён в базе знаний ai-knowledge, profile.md и projects/job-parser-bot.md): Google Таблицы
и Apps Script, отчёты и сверки, SQL/API -> Sheets, Telegram-боты, парсеры, Python-автоматизация,
процессы маркетплейсов. Сайты, лендинги, WP/Tilda, дизайн, ручное заполнение и вакансии в штат - вне профиля.
Код пишет ИИ; анализ, проверка на данных клиента, запуск и передача результата остаются работой исполнителя.

Каталог опыта (EXPERIENCE) - только то, что подтверждено файлами базы знаний (поле source). Он различает
внутреннее подтверждение и материалы, РАЗРЕШЁННЫЕ К ПОКАЗУ клиенту: рабочий опыт Ивана - внутренние данные
работодателя, и по умолчанию ничего клиенту не показывается. Иван сам разрешает показ через переменную
окружения FREELANCE_CLIENT_EXPERIENCE_IDS (id через запятую). Всю личную базу знаний в AI не передаём:
в запрос уходят только названия подходящих записей.
"""
import os
import re

# Что подходит и что нет - текст для промптов (оценка, триаж). Списки слов для префильтра - в filters.py,
# они выведены из этих же групп; менять профиль нужно в обоих местах и прогонять tests/test_filters.py.
FIT_TEXT = (
    "Хорошо подходят: Google Таблицы (формулы, Apps Script, импорт и синхронизация между таблицами, "
    "автоматические отчёты, дашборды, ускорение и починка таблиц), Excel; выгрузки из баз данных и по API в "
    "таблицы; Telegram-боты (в том числе с оплатой, админкой, мини-приложением); парсеры с выгрузкой в таблицу; "
    "скрипты и автоматизация на Python; интеграции по API (CRM, маркетплейсы Wildberries/Ozon, банки, "
    "мессенджеры); учёт, сверки и отчёты для селлеров маркетплейсов; нейросети и ИИ-агенты внутри таблиц, ботов "
    "и автоматизаций; описания бизнес-проблем без названия технологии («вручную сводим выгрузки», "
    "«теряются заявки»)."
)
NOT_FIT_TEXT = (
    "Не подходят: сайты, лендинги, интернет-магазины, WordPress/Tilda, вёрстка и правки сайтов; дизайн и "
    "графика, тексты, SMM, продвижение, физические и офлайн-услуги, 1С, Битрикс, мобильные приложения, игры, "
    "установка ПО, обучение, ручное заполнение карточек и таблиц, вакансии «в штат», размытые или "
    "подозрительные заказы. Если сайт - только источник данных (парсер сайта) или форма сайта лишь отправляет "
    "заявки в таблицу, бот или CRM, это наш профиль."
)
SHORT_FIT_TEXT = (
    "Подходят: Google Таблицы, формулы, Apps Script, Excel, отчёты, выгрузки из баз и API в таблицы, "
    "Telegram-боты, парсеры, скрипты и автоматизация, интеграции по API (CRM, маркетплейсы), ИИ-агенты. "
    "Не подходят: сайты, лендинги, вёрстка, WordPress/Tilda, дизайн, тексты, SMM, офлайн и ручная работа, "
    "1С, обучение, объявления исполнителей о своих услугах."
)

# Опыт: id, краткое название (его и видит AI), темы для подбора, источник подтверждения в базе знаний.
# Формулировки намеренно общие: конкретных клиентов, цифр и результатов в каталоге нет и быть не должно.
EXPERIENCE = (
    {"id": "sheets_import", "title": "импорт и синхронизация данных между Google Таблицами через Apps Script, "
     "в том числе обход лимита времени выполнения", "topics": ("sheets", "script", "integration"),
     "words": ("импорт", "синхрониз", "apps script", "таблиц", "перенос"),
     "source": "skills/gas-sheets-import/SKILL.md"},
    {"id": "sheets_status_sync", "title": "перенос статусов между листами по составному ключу (Apps Script)",
     "topics": ("sheets", "script"), "words": ("статус", "сверк", "лист", "ключ"),
     "source": "skills/gas-status-sync/SKILL.md"},
    {"id": "sheets_formulas", "title": "ускорение и починка формул Google Таблиц на больших данных",
     "topics": ("sheets",), "words": ("формул", "тормоз", "медленн", "впр", "xlookup", "arrayformula"),
     "source": "skills/gsheets-formula-tuning/SKILL.md"},
    {"id": "sheets_column_shift", "title": "привязка скриптов Apps Script к названиям столбцов, чтобы вставка "
     "столбцов не ломала расчёты", "topics": ("sheets", "script"), "words": ("столбц", "сдвиг", "колонк"),
     "source": "skills/gas-column-shift-fix/SKILL.md"},
    {"id": "wb_stock_days", "title": "расчёт обеспеченности запаса в днях по заказам Wildberries (ФБС/ФБО) "
     "в Google Таблицах", "topics": ("sheets", "script", "integration"),
     "words": ("wildberries", "wb", "остатк", "запас", "поставк", "фбо", "фбс", "склад"),
     "source": "skills/gas-wb-stock-days/SKILL.md"},
    {"id": "stock_consumption", "title": "учёт расхода и прихода материалов со средним расходом в Apps Script",
     "topics": ("sheets", "script"), "words": ("учёт", "учет", "склад", "расход", "приход"),
     "source": "skills/gas-stock-consumption/SKILL.md"},
    {"id": "db_to_sheets", "title": "выгрузки из баз данных (SQL) в Google Таблицы по расписанию",
     "topics": ("sheets", "integration", "script"), "words": ("sql", "база данных", "выгрузк", "postgres"),
     "source": "profile.md (Окружение: выгрузки БД -> Google Sheets), knowledge/work-db-to-sheets.md"},
    {"id": "tg_bots", "title": "Telegram-боты на Python (aiogram), в том числе мини-приложение, оплата, "
     "парсеры и AI-оценка", "topics": ("tg_bot", "parser"),
     "words": ("бот", "telegram", "телеграм", "парс", "aiogram", "mini app", "мини-апп"),
     "source": "projects/calisthenics-online.md, projects/job-parser-bot.md, skills/job-bot-fix/SKILL.md"},
)


def client_visible_ids() -> set[str]:
    """Id опыта, разрешённого к показу клиенту (FREELANCE_CLIENT_EXPERIENCE_IDS). По умолчанию пусто."""
    raw = os.environ.get("FREELANCE_CLIENT_EXPERIENCE_IDS", "")
    return {x.strip() for x in raw.split(",") if x.strip()}


def relevant_experience(category: str | None, text: str, limit: int = 3, min_score: int = 2) -> list[dict]:
    """Подтверждённый опыт, подходящий заказу: по категории оценки и словам заказа. Возвращает записи каталога
    (с полем client_ok: можно ли ссылаться на них в отклике клиенту). Ничего не придумывает - только каталог."""
    low = (text or "").lower().replace("ё", "е")
    scored = []
    for item in EXPERIENCE:
        score = (2 if category in item["topics"] else 0) + sum(
            1 for w in item["words"] if re.search(rf"(?<!\w){re.escape(w.replace('ё', 'е'))}", low))
        if score >= min_score:
            scored.append((score, item))
    scored.sort(key=lambda t: -t[0])
    visible = client_visible_ids()
    return [{**item, "client_ok": item["id"] in visible} for _, item in scored[:limit]]
