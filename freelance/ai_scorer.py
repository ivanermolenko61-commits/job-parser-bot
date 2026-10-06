"""Оценка фриланс-заказа через YandexGPT.

Изолирован: если ключей нет или модель ответила мусором, score_order()
возвращает None, и заказ отправляется без оценки (пометка «без AI»).
"""
import json
import logging
import os
import re

from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("YANDEX_API_KEY")
FOLDER_ID = os.getenv("YANDEX_FOLDER_ID")

# полная yandexgpt заметно строже lite к мусору (проверено на реальных заказах)
MODEL_NAME = os.getenv("YANDEX_MODEL", "yandexgpt")
# Gemini (бесплатный тариф AI Studio) — основной оценщик, если задан ключ;
# YandexGPT остаётся запасным на случай сбоя, квоты или недоступности региона
GEMINI_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{}:generateContent"

# дешёвое сито: заказы с оценкой lite ниже порога дальше не идут (пусто = выключено)
PRESCREEN_MODEL = os.getenv("YANDEX_PRESCREEN_MODEL", "yandexgpt-lite")
PRESCREEN_MIN_FIT = 4
REFUSAL_RE =re.compile(r"не могу (обсуждать|ответить|помочь)", re.IGNORECASE)
REQUEST_TIMEOUT_SEC = 20
BODY_LIMIT = 1500

SYSTEM_PROMPT = (
    "Ты оцениваешь фриланс-заказ для исполнителя, у которого весь код пишет "
    "ИИ-ассистент Claude Code. Хорошо подходят: Telegram-боты, сайты и "
    "лендинги, парсеры, скрипты и автоматизация на Python, интеграции по API, "
    "WordPress/Tilda, Google Таблицы и Apps Script, вёрстка. Не подходят: "
    "дизайн и графика, тексты, SMM, физические и офлайн-услуги, 1С, "
    "установка ПО, обучение, заполнение карточек, вакансии «в штат», "
    "размытые или подозрительные заказы.\n"
    "Шкала fit (используй ВСЕ значения, не только 0 и 10):\n"
    "9-10: типовой бот, лендинг, сайт-визитка или парсер, ТЗ ясное, "
    "бюджет адекватен объёму;\n"
    "7-8: подходит, но ТЗ частично размыто, нужна интеграция или бюджет "
    "скромный для такого объёма. Сюда же: доработка и правки существующего "
    "сайта (WordPress, Tilda, вёрстка, формы), простой парсинг данных в "
    "таблицу или Excel, несложный бот или скрипт;\n"
    "5-6: подходит с оговорками: большой объём, много неизвестных, "
    "бюджет явно мал или не указан, а задача неясна;\n"
    "3-4: сложно или рискованно: высоконагруженная система, мобильное "
    "приложение, игра, интернет-магазин с нуля, работа на чужом сервере "
    "без доступов;\n"
    "0-2: не наш профиль (дизайн, тексты, офлайн, 1С, обучение, ручной труд).\n"
    "Бюджет: небольшой, но реальный (3-15 тыс. за бота, парсер, лендинг, "
    "правки сайта) нормален, не снижай за него оценку. Снижай fit до 4 и "
    "ниже только если бюджет абсурдно мал (меньше 1000 ₽ за сайт, бота или "
    "парсер). Цена «по договорённости» не повод снижать оценку.\n"
    "Оценивай саму задачу: нужно ли тут писать код или настраивать сайт. "
    "Ручная работа за компьютером (покупать, скачивать, регистрировать, "
    "кликать, заполнять) кодом не решается: fit 0-2.\n"
    "Ответь СТРОГО одним JSON-объектом без пояснений и без markdown:\n"
    '{"fit": целое число 0-10 по шкале выше, '
    '"difficulty": "легко" | "средне" | "сложно", '
    '"hours": число (оценка часов работы с Claude Code), '
    '"price_rub": "рекомендуемая цена для отклика, диапазон, например 8-12 тыс.", '
    '"summary": "суть заказа в одну короткую строку", '
    '"risks": "главный риск в одну короткую строку"}'
)

_sdk = None


def is_enabled() -> bool:
    return bool(GEMINI_KEY or (API_KEY and FOLDER_ID))


def _get_model(name: str = MODEL_NAME):
    global _sdk
    if _sdk is None:
        # импорт здесь: без SDK бот не падает
        try:
            from yandex_ai_studio_sdk import AIStudio as _Client
        except ImportError:  # старое имя пакета
            from yandex_cloud_ml_sdk import YCloudML as _Client
        _sdk = _Client(folder_id=FOLDER_ID, auth=API_KEY)
    return _sdk.models.completions(name).configure(temperature=0.2)


def parse_ai_json(text: str) -> dict | None:
    """Достаёт JSON из ответа модели (с ```json-обёрткой или лишним текстом)."""
    if not text:
        return None
    cleaned = re.sub(r"```(?:json)?", "", text, flags=re.IGNORECASE).strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        data = json.loads(cleaned[start:end + 1])
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    try:
        fit = int(round(float(data.get("fit"))))
    except (TypeError, ValueError):
        return None
    try:
        hours = float(data.get("hours") or 0)
    except (TypeError, ValueError):
        hours = 0.0
    difficulty = str(data.get("difficulty") or "").strip().lower()
    if difficulty not in ("легко", "средне", "сложно"):
        difficulty = ""
    return {
        "fit": max(0, min(10, fit)),
        "difficulty": difficulty,
        "hours": hours,
        "price_rub": str(data.get("price_rub") or "").strip(),
        "summary": str(data.get("summary") or "").strip(),
        "risks": str(data.get("risks") or "").strip(),
    }


def score_order(order) -> dict | None:
    """Двухступенчатая оценка: дешёвая lite отсеивает очевидный мусор,
    полная модель оценивает только прошедших. Это в разы дешевле, чем гнать
    через полную все заказы. None — AI недоступен или ответ не разобран."""
    if not is_enabled():
        return None
    if GEMINI_KEY:
        result = _ask_gemini(order)
        if result is not None or not (API_KEY and FOLDER_ID):
            return result
        # Gemini не ответил: запасной путь через YandexGPT
    elif not (API_KEY and FOLDER_ID):
        return None
    if PRESCREEN_MODEL and PRESCREEN_MODEL != MODEL_NAME:
        pre = _ask(PRESCREEN_MODEL, order)
        if pre is not None and pre["fit"] < PRESCREEN_MIN_FIT:
            return pre
        # lite сбоит или пропустила: решает полная модель
    return _ask(MODEL_NAME, order)


def _ask(model_name: str, order) -> dict | None:
    user_prompt = (
        f"Название: {order.title}\n"
        f"Цена: {order.price_text or 'не указана'}\n"
        f"Описание:\n{(order.body or '')[:BODY_LIMIT]}"
    )
    try:
        result = _get_model(model_name).run(
            [
                {"role": "system", "text": SYSTEM_PROMPT},
                {"role": "user", "text": user_prompt},
            ],
            timeout=REQUEST_TIMEOUT_SEC,
        )
        for alternative in result:
            parsed = parse_ai_json(alternative.text)
            if parsed is None:
                logging.warning(f"[AI] ответ не JSON: {alternative.text[:150]!r}")
                if REFUSAL_RE.search(alternative.text or ""):
                    # фильтр модели отказался: заказ не наш, не переоцениваем вечно
                    return {"fit": 0, "difficulty": "", "hours": 0.0,
                            "price_rub": "", "summary": "отказ модели",
                            "risks": ""}
            return parsed
    except Exception:
        logging.exception("[AI] ошибка YandexGPT")
    return None


if __name__ == "__main__":
    # Ручной прогон: типовые заказы + свежие реальные
    from datetime import datetime, timezone

    from freelance.alot_client import FreelanceOrder

    logging.basicConfig(level=logging.INFO)
    print("AI включён:", is_enabled())

    def mk(i, title, body, price):
        return FreelanceOrder(i, "youdoru", title, body, price, 0.0, [],
                              datetime.now(timezone.utc), "")

    samples = [
        mk(1, "Создать бота для telegram",
           "Нужен бот для записи клиентов в салон: выбор мастера, времени, "
           "напоминание за день. Админка в Google Таблице.", "до 15 000 ₽"),
        mk(2, "Заполнить карточки товаров на Wildberries",
           "Нужно заполнить 200 карточек по готовой таблице, вручную.", "до 3 000 ₽"),
        mk(3, "Сделать сайт под ключ",
           "Сайт-визитка для строительной компании, 5 страниц, форма заявки.", "по договоренности"),
        mk(4, "Нарисовать логотип для кофейни", "Нужен логотип в векторе.", "до 2 000 ₽"),
        mk(5, "Спарсить гостиницы и хостелы Москвы",
           "Собрать название, адрес, телефон, сайт с Яндекс.Карт в Excel.", "2 500 ₽"),
    ]
    for o in samples:
        res = score_order(o)
        print(f"\n{o.title}\n  -> {json.dumps(res, ensure_ascii=False)}")

    # Проверка устойчивого разбора обёрнутого ответа
    wrapped = '```json\n{"fit": 8, "difficulty": "легко", "hours": 5, "price_rub": "8-12 тыс.", "summary": "x", "risks": "y"}\n```'
    print("\nparse ```json:", parse_ai_json(wrapped))
