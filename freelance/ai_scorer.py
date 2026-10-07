"""Оценка фриланс-заказа: цепочка бесплатных моделей Gemini, YandexGPT lite - запасной.

Порядок: GEMINI_MODELS по очереди (у каждой свой бесплатный лимит), затем, если
заданы ключи, yandexgpt-lite (не больше YANDEX_MAX_PER_DAY в сутки). Если ничего не
ответило, score_order() возвращает None, и бот повторит оценку в следующем цикле
(после MAX_AI_ATTEMPTS попыток заказ уходит с пометкой «без AI»).
"""
import json
import logging
import math
import os
import re
import threading
import time
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("YANDEX_API_KEY")
FOLDER_ID = os.getenv("YANDEX_FOLDER_ID")

# Gemini (бесплатный тариф AI Studio) - основной оценщик. Цепочка моделей: следующая
# берётся, если предыдущая на паузе (429), недоступна (404) или не дала разбираемый ответ.
GEMINI_KEY = os.getenv("GEMINI_API_KEY")
DEFAULT_GEMINI_MODELS = (
    "gemini-3.5-flash-lite,gemini-3.1-flash-lite,gemini-3.1-flash-lite-preview,gemma-4-31b-it"
)


def _parse_models() -> list[str]:
    # GEMINI_MODEL (одна модель, старая настройка) ставится первой, остальные из
    # цепочки по умолчанию идут запасными; явный GEMINI_MODELS задаёт цепочку целиком
    explicit = os.getenv("GEMINI_MODELS")
    if explicit:
        raw = explicit
    else:
        legacy = (os.getenv("GEMINI_MODEL") or "").strip()
        raw = f"{legacy},{DEFAULT_GEMINI_MODELS}" if legacy else DEFAULT_GEMINI_MODELS
    models: list[str] = []
    for name in raw.split(","):
        name = name.strip()
        if name and name not in models:
            models.append(name)
    return models


GEMINI_MODELS = _parse_models()
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{}:generateContent"
# Бережём лимиты бесплатного тарифа: общий интервал между запросами к Gemini,
# при 429 пауза только той модели, которая ответила 429
GEMINI_MIN_INTERVAL_SEC = float(os.getenv("GEMINI_MIN_INTERVAL_SEC", "4.5"))
GEMINI_COOLDOWN_SEC = 120
CONFIG_ERROR_COOLDOWN_SEC = 300  # после ошибки ключа Gemini не долбим 5 минут

_lock = threading.Lock()
_next_at = 0.0                        # не раньше (monotonic) - следующий запрос к Gemini
_paused_until: dict[str, float] = {}  # модель -> конец паузы после 429
_disabled: set[str] = set()           # модели, которых нет (404) - до рестарта
_config_blocked_until = 0.0
_config_error = ""
_last_error = ""
_last_ok: dict | None = None          # {"model": ..., "at": iso}

# Запасной путь Yandex включён, если есть ключи; отключить: YANDEX_ENABLED=0.
YANDEX_ENABLED = os.getenv("YANDEX_ENABLED") != "0" and bool(API_KEY and FOLDER_ID)
YANDEX_MAX_PER_DAY = int(os.getenv("YANDEX_MAX_PER_DAY", "60"))
YANDEX_MODEL = "yandexgpt-lite"
_yandex_day = ""
_yandex_count = 0

REFUSAL_RE = re.compile(r"не могу (обсуждать|ответить|помочь)", re.IGNORECASE)
REQUEST_TIMEOUT_SEC = 30
GEMMA_TIMEOUT_SEC = 90
BODY_LIMIT = 1500
# Признаки неверного/недействительного ключа (а не проблемы конкретной модели)
KEY_ERROR_MARKERS = ("API_KEY_INVALID", "API key not valid", "PERMISSION_DENIED",
                     "API key expired", "UNAUTHENTICATED")

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
    return bool(GEMINI_KEY or YANDEX_ENABLED)


def _get_model(name: str = YANDEX_MODEL):
    global _sdk
    if _sdk is None:
        # импорт здесь: без SDK бот не падает
        try:
            from yandex_ai_studio_sdk import AIStudio as _Client
        except ImportError:  # старое имя пакета
            from yandex_cloud_ml_sdk import YCloudML as _Client
        _sdk = _Client(folder_id=FOLDER_ID, auth=API_KEY)
    return _sdk.models.completions(name).configure(temperature=0.2)


def status() -> dict:
    """Состояние AI для статуса и оповещений (без секретов)."""
    now = time.monotonic()
    with _lock:
        paused = {m: int(t - now) for m, t in _paused_until.items()
                  if t > now and m not in _disabled}
        return {
            "enabled": is_enabled(),
            "gemini": bool(GEMINI_KEY),
            "models": [m for m in GEMINI_MODELS if m not in _disabled],
            "paused": paused,
            "disabled": [m for m in GEMINI_MODELS if m in _disabled],
            "yandex": YANDEX_ENABLED,
            "last_ok": dict(_last_ok) if _last_ok else None,
            "config_error": _config_error,
            "last_error": _last_error,
        }


def _clean_float(value) -> float:
    try:
        f = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    return f if math.isfinite(f) else 0.0


def parse_ai_json(text: str) -> dict | None:
    """Достаёт JSON из ответа модели (с ```json-обёрткой, рассуждениями или лишним текстом)."""
    if not text:
        return None
    cleaned = re.sub(r"```(?:json)?", "", text, flags=re.IGNORECASE).strip()
    data = None
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start != -1 and end > start:
        try:
            data = json.loads(cleaned[start:end + 1])
        except ValueError:
            data = None
    if not isinstance(data, dict) or "fit" not in data:
        # Рассуждающие модели (gemma) пишут текст вокруг: берём последний объект с fit
        data = None
        decoder = json.JSONDecoder()
        for m in re.finditer(r"\{", cleaned):
            try:
                obj, _ = decoder.raw_decode(cleaned[m.start():])
            except ValueError:
                continue
            if isinstance(obj, dict) and "fit" in obj:
                data = obj
        if data is None:
            return None
    try:
        fit_raw = float(data.get("fit"))
        if not math.isfinite(fit_raw):
            return None
        fit = int(round(fit_raw))
    except (TypeError, ValueError):
        return None
    hours = _clean_float(data.get("hours"))  # NaN/Infinity -> 0
    difficulty = str(data.get("difficulty") or "").strip().lower()
    if difficulty not in ("легко", "средне", "сложно"):
        difficulty = ""
    return {
        "fit": max(0, min(10, fit)),
        "difficulty": difficulty,
        "hours": round(hours, 1),
        "price_rub": str(data.get("price_rub") or "").strip()[:100],
        "summary": str(data.get("summary") or "").strip()[:300],
        "risks": str(data.get("risks") or "").strip()[:300],
    }


def score_order(order) -> dict | None:
    """Оценка заказа: Gemini по цепочке GEMINI_MODELS, затем (если есть ключи)
    yandexgpt-lite с дневным лимитом YANDEX_MAX_PER_DAY. Возвращает dict оценки
    (с ключом "model") или None: AI недоступен или ни одна модель не дала
    разбираемый ответ."""
    if not is_enabled():
        return None
    if GEMINI_KEY:
        result = _ask_gemini_chain(order)
        if result is not None:
            return result
    if YANDEX_ENABLED:
        return _ask_yandex_lite(order)
    return None


def _ask_yandex_lite(order) -> dict | None:
    """Запасной путь: самая простая модель, не больше YANDEX_MAX_PER_DAY в сутки."""
    global _yandex_day, _yandex_count
    today = time.strftime("%Y-%m-%d")
    with _lock:
        if _yandex_day != today:
            _yandex_day, _yandex_count = today, 0
        if _yandex_count >= YANDEX_MAX_PER_DAY:
            return None
        _yandex_count += 1
    result = _ask(YANDEX_MODEL, order)
    if result is not None:
        _mark_ok(YANDEX_MODEL, gemini=False)
        result["model"] = YANDEX_MODEL
    return result


def _user_prompt(order) -> str:
    return (
        f"Название: {order.title}\n"
        f"Цена: {order.price_text or 'не указана'}\n"
        f"Описание:\n{(order.body or '')[:BODY_LIMIT]}"
    )


def _mark_ok(model: str, gemini: bool = True) -> None:
    """Запоминает удачную оценку. Успех Yandex не снимает ошибку ключа Gemini."""
    global _last_ok, _config_error, _config_blocked_until, _last_error
    with _lock:
        _last_ok = {"model": model, "at": datetime.now(timezone.utc).isoformat()}
        if gemini:
            _config_error = ""
            _config_blocked_until = 0.0
            _last_error = ""


def _set_error(text: str) -> None:
    global _last_error
    with _lock:
        _last_error = text


def _is_gemma(model: str) -> bool:
    return model.lower().startswith("gemma")


def _build_body(model: str, order) -> dict:
    prompt = _user_prompt(order)
    if _is_gemma(model):
        # gemma не поддерживает systemInstruction и JSON-режим: клеим промпт в сообщение
        text = f"{SYSTEM_PROMPT}\n\n---\n{prompt}"
        return {
            "contents": [{"role": "user", "parts": [{"text": text}]}],
            "generationConfig": {"temperature": 0.2},
        }
    return {
        "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json"},
    }


def _throttle() -> None:
    """Общий интервал между запросами к Gemini (все модели делят один клиентский темп)."""
    global _next_at
    with _lock:
        now = time.monotonic()
        start = max(now, _next_at)
        _next_at = start + GEMINI_MIN_INTERVAL_SEC
    if start > now:
        time.sleep(start - now)


def _ask_gemini_chain(order) -> dict | None:
    with _lock:
        if time.monotonic() < _config_blocked_until:
            return None  # недавно ключ был отклонён: не долбим
    for model in GEMINI_MODELS:
        with _lock:
            if model in _disabled or time.monotonic() < _paused_until.get(model, 0.0):
                continue
        outcome = _ask_gemini_model(model, order)
        if isinstance(outcome, dict):
            return outcome
        if outcome == "config":
            return None  # ключ неверный: остальные модели Gemini не помогут
    return None


def _ask_gemini_model(model: str, order):
    """Один запрос к одной модели. dict - успех; "next" - пробуем следующую;
    "config" - ошибка ключа (цепочку Gemini прерываем)."""
    import requests
    global _config_error, _config_blocked_until
    try:
        _throttle()
        resp = requests.post(
            GEMINI_URL.format(model),
            headers={"x-goog-api-key": GEMINI_KEY},
            json=_build_body(model, order),
            # gemma «думает» до ~50 с (проверено живым запросом), ей нужен больший таймаут
            timeout=GEMMA_TIMEOUT_SEC if _is_gemma(model) else REQUEST_TIMEOUT_SEC,
        )
    except Exception as e:
        logging.warning(f"[AI] {model}: сбой запроса: {type(e).__name__}")
        _set_error(f"{model}: {type(e).__name__}")
        return "next"

    code = resp.status_code
    if code == 429:
        with _lock:
            _paused_until[model] = time.monotonic() + GEMINI_COOLDOWN_SEC
        logging.warning(f"[AI] {model}: лимит запросов, пауза {GEMINI_COOLDOWN_SEC} с, берём следующую")
        _set_error(f"{model}: HTTP 429 (лимит)")
        return "next"
    if code != 200:
        body = resp.text[:400]
        if code in (400, 401, 403) and any(m in body for m in KEY_ERROR_MARKERS):
            reason = f"ключ Gemini отклонён (HTTP {code})"
            with _lock:
                _config_error = reason
                _config_blocked_until = time.monotonic() + CONFIG_ERROR_COOLDOWN_SEC
            logging.error(f"[AI] {reason}")
            _set_error(reason)
            return "config"
        if code == 404 or "is not found" in body or "no longer available" in body:
            with _lock:
                _disabled.add(model)
            logging.warning(f"[AI] модель {model} недоступна (HTTP {code}), выключена до рестарта")
            _set_error(f"{model}: недоступна (HTTP {code})")
            return "next"
        logging.warning(f"[AI] {model}: HTTP {code}: {body[:150]!r}")
        _set_error(f"{model}: HTTP {code}")
        return "next"

    try:
        payload = resp.json()
        cand = (payload.get("candidates") or [{}])[0]
        parts = (cand.get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
    except Exception as e:
        logging.warning(f"[AI] {model}: не разобран ответ: {type(e).__name__}")
        _set_error(f"{model}: нечитаемый ответ")
        return "next"
    if not text:
        # safety-блок (promptFeedback.blockReason) или пустой ответ: следующая модель
        reason = (cand.get("finishReason")
                  or (payload.get("promptFeedback") or {}).get("blockReason") or "пусто")
        logging.warning(f"[AI] {model}: пустой ответ ({reason})")
        _set_error(f"{model}: пустой ответ ({reason})")
        return "next"
    parsed = parse_ai_json(text)
    if parsed is None:
        logging.warning(f"[AI] {model}: ответ не JSON: {text[:150]!r}")
        _set_error(f"{model}: ответ не JSON")
        return "next"
    parsed["model"] = model
    _mark_ok(model)
    return parsed


def _ask(model_name: str, order) -> dict | None:
    user_prompt = _user_prompt(order)
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
    # Ручной прогон: типовые заказы
    from datetime import datetime, timezone

    from freelance.alot_client import FreelanceOrder

    logging.basicConfig(level=logging.INFO)
    print("AI включён:", is_enabled(), "модели:", GEMINI_MODELS)

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
    print("\nstatus:", json.dumps(status(), ensure_ascii=False))

    # Проверка устойчивого разбора обёрнутого ответа
    wrapped = '```json\n{"fit": 8, "difficulty": "легко", "hours": 5, "price_rub": "8-12 тыс.", "summary": "x", "risks": "y"}\n```'
    print("\nparse ```json:", parse_ai_json(wrapped))
    print("parse NaN hours:", parse_ai_json('{"fit": 7, "hours": NaN}'))
