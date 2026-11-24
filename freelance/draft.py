"""Черновик для копирования и отдельная личная памятка из сохранённой оценки."""
from __future__ import annotations

import json
import math

from freelance import pricing
from freelance.profile import relevant_experience


# Клиенту показываем публичную тему, выбранный подход и разрешённый опыт.
# Свободный текст AI может содержать личные заметки; он остаётся в памятке.
APPROACH = {
    "sheets": ("Соберу расчёт на примере ваших данных и сверю итог с контрольным значением.",
               "Какой результат должна показать таблица на одном примере?"),
    "script": ("Разберу текущую последовательность действий, автоматизирую повторяющиеся шаги и проверю результат.",
               "Можете показать один пример исходных данных и нужного результата?"),
    "integration": ("Проверю обмен на одной записи, затем добавлю обработку повторов и ошибок.",
                    "Какие системы должны обмениваться данными и что запускает передачу?"),
    "tg_bot": ("Соберу основной сценарий бота и проверю его от первого сообщения до результата.",
               "Какое действие пользователь должен завершить в первом рабочем сценарии?"),
    "parser": ("Проверю доступный способ получения данных на одном источнике и подготовлю пробную выгрузку.",
               "Какие поля нужно получить и как часто обновлять выгрузку?"),
}
DEFAULT = ("Предлагаю согласовать результат на одном примере и по нему проверить готовую работу.",
           "По какому примеру вы будете принимать результат?")


def _value(value):
    try:
        n = float(value)
    except (TypeError, ValueError, OverflowError):
        return ""
    return f"{n:g}" if math.isfinite(n) and n > 0 else ""


def _estimate(ai, field, unit):
    lo, hi = (_value(ai.get(f"{field}_{edge}")) for edge in ("low", "high"))
    if lo and hi and float(lo) <= float(hi):
        return (lo if lo == hi else f"{lo}–{hi}") + " " + unit
    single = _value(ai.get("price_mid" if field == "price" else field))
    return f"около {single} {unit}" if single else "нужно уточнить"


def draft_messages(row):
    try:
        ai = json.loads(row.get("ai_json") or "{}")
    except (TypeError, ValueError):
        ai = {}
    if not isinstance(ai, dict):
        ai = {}
    category = ai.get("category")
    category = category if isinstance(category, str) else None
    title = " ".join(str(row.get("title") or "").split())[:180]
    pricing.apply_stored_formula_quote(ai, row)
    scoped_quote = pricing.formula_quote_note(ai)
    if scoped_quote:
        approach = "Посмотрю действующие формулы и сверю исправленный расчёт с вашим примером."
        question = pricing.FORMULA_QUESTION
    else:
        approach, question = APPROACH.get(category, DEFAULT)
    public = [f"Здравствуйте! Готов обсудить задачу «{title}»." if title else "Здравствуйте! Готов обсудить вашу задачу.", approach]
    matches = relevant_experience(category, title + " " + (row.get("snippet") or ""), limit=20)
    experience = next((item["title"] for item in matches if item["client_ok"]), None)
    if experience:
        public.append(f"Из похожего опыта — {experience}.")
    public.append(question)
    conditions = ai.get("price_conditions") or ai.get("unknowns")
    assumptions = ", ".join(str(x)[:120] for x in conditions[:3] if isinstance(x, str) and x.strip()) if isinstance(conditions, list) else ""
    note = ["Только для Ивана", "Актуальность на площадке нужно проверить перед отправкой."]
    if row.get("stale_reason") or row.get("desc_changed"):
        note.append("Объявление устарело или изменилось. Сверь исходный текст.")
    note.append(f"Риск: {str(ai.get('risks') or 'не оценён')[:300]}")
    if scoped_quote:
        note.append(scoped_quote)
    else:
        note.extend((f"Трудозатраты: {_estimate(ai, 'hours', 'ч')}",
                     f"Цена полного объёма: {_estimate(ai, 'price', '₽')}"))
    note.append(f"Допущения: {assumptions or 'уточнить объём и критерий приёмки'}")
    questions = ai.get("questions")
    if isinstance(questions, list) and questions and isinstance(questions[0], str):
        note.append("Уточнение из оценки: " + questions[0][:200])
    return "\n\n".join(public), "\n".join(note)
