"""Оценка фриланс-заказа: цепочка бесплатных AI-провайдеров, взаимозаменяемых.

Порядок задаёт AI_PROVIDERS (по умолчанию gemini,cloudflare,openrouter,github,groq,
cerebras,mistral; платный yandex - только явно). Провайдер без ключа пропускается. Если провайдер в лимите, квота кончилась,
ключ отклонён или ответ не JSON - заказ сразу уходит следующему, а провайдер/модель
на паузе пропускаются до её окончания. Gemini - свои модели GEMINI_MODELS; groq,
cerebras, mistral, openrouter - общий OpenAI-совместимый клиент (только бесплатные
тарифы и модели); yandexgpt-lite - запасной с лимитом YANDEX_MAX_PER_DAY в сутки.
Если никто не ответил, score_order() возвращает None, и бот повторит оценку в
следующем цикле (заказ ждёт оценки, пока свежий; без оценки в чат не уходит).
"""
import email.utils
import json
import logging
import math
import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("YANDEX_API_KEY")
FOLDER_ID = os.getenv("YANDEX_FOLDER_ID")

# Gemini (бесплатный тариф AI Studio) - основной оценщик. Цепочка моделей: следующая
# берётся, если предыдущая на паузе (429), недоступна (404) или не дала разбираемый ответ.
GEMINI_KEY = os.getenv("GEMINI_API_KEY")
# gemma: дневной лимит бесплатного тарифа 14 400 (у flash-lite - 500), но 16K токенов
# в минуту; у каждой модели своя квота, поэтому обе gemma в цепочке
DEFAULT_GEMINI_MODELS = (
    "gemini-3.5-flash-lite,gemini-3.1-flash-lite,gemini-3.1-flash-lite-preview,"
    "gemma-4-31b-it,gemma-4-26b-a4b-it"
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
GEMINI_COOLDOWN_SEC = 120          # пауза после 429, если Google не назвал срок
MAX_QUOTA_PAUSE_SEC = 24 * 3600    # потолок паузы по retryDelay (дневная квота)
CONFIG_ERROR_COOLDOWN_SEC = 300  # после ошибки ключа Gemini не долбим 5 минут
REGION_BLOCK_COOLDOWN_SEC = 1800  # сервер в неподдерживаемом регионе: Gemini не пробуем 30 минут
NON_JSON_PAUSE_SEC = 1800         # 200 без JSON (страница блокировки/прокси): провайдера не долбим
REGION_ERROR_TEXT = "Gemini недоступен из региона сервера (User location is not supported)"

_lock = threading.Lock()
_next_at = 0.0                        # не раньше (monotonic) - следующий запрос к Gemini
_paused_until: dict[str, float] = {}  # модель -> конец паузы после 429
_disabled: set[str] = set()           # модели, которых нет (404) - до рестарта
_config_blocked_until = 0.0
_config_error = ""
_region_error = ""                    # Gemini не поддерживает регион сервера (не ошибка ключа)
_last_error = ""
_gemini_last_error = ""             # последняя ошибка именно Gemini
_last_ok: dict | None = None          # {"model": ..., "at": iso}

# YandexGPT ПЛАТНЫЙ (бесплатен только стартовый грант): по умолчанию выключен, не вызывается
# никогда, пока нет YANDEX_ENABLED=1 (плюс yandex в AI_PROVIDERS и оба ключа).
YANDEX_ENABLED = os.getenv("YANDEX_ENABLED") == "1" and bool(API_KEY and FOLDER_ID)
YANDEX_MAX_PER_DAY = int(os.getenv("YANDEX_MAX_PER_DAY", "60"))
YANDEX_MODEL = "yandexgpt-lite"
_yandex_day = ""
_yandex_count = 0

REFUSAL_RE = re.compile(r"не могу (обсуждать|ответить|помочь)", re.IGNORECASE)
REQUEST_TIMEOUT_SEC = 30
GEMMA_TIMEOUT_SEC = 90
BODY_LIMIT = 3000
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
    "без доступов. "
    "Лечение взлома или вирусов и работа на чужом сервере: fit не выше 5, не меньше 3 ч "
    "на диагностику, доступы впиши в unknowns;\n"
    "0-2: не наш профиль (дизайн, тексты, офлайн, 1С, обучение, ручной труд), "
    "а также объявление исполнителя о своих услугах (от первого лица: «создам», "
    "«сделаю для вас»), а не заказ клиента - fit 0.\n"
    "Бюджет: небольшой, но реальный (3-15 тыс. за бота, парсер, лендинг, "
    "правки сайта) нормален, не снижай за него оценку. Снижай fit до 4 и "
    "ниже только если бюджет абсурдно мал (меньше 1000 ₽ за сайт, бота или "
    "парсер). Цена «по договорённости» не повод снижать оценку.\n"
    "Оценивай саму задачу: нужно ли тут писать код или настраивать сайт. "
    "Ручная работа за компьютером (покупать, скачивать, регистрировать, "
    "кликать, заполнять) кодом не решается: fit 0-2.\n"
    "Цену НЕ называй: её посчитает программа по твоим часам. Твоя задача - "
    "разложить работу на компоненты и честно оценить часы работы С Claude Code "
    "(не вручную). Ориентиры по часам: типовой Telegram-бот 3-6 ч; бот с "
    "оплатой, админкой или интеграцией 6-12 ч; парсер одной страницы/сайта 1-3 ч; "
    "парсер с авторизацией, капчей или многими сайтами 4-8 ч; лендинг 3-5 ч; "
    "сайт-визитка 4-8 ч; правка или доработка существующего сайта 0.5-2 ч; "
    "скрипт или автоматизация 1-3 ч; Google Таблицы/Apps Script 1-4 ч; "
    "интеграция с API 2-6 ч. Если нужно оборудование (терминалы, СКУД, кассы, "
    "принтеры, контроллеры) или чужая система без открытой документации API "
    "(iiko, 1С, CRM, учётные программы): clarity «размыто», не меньше 3 ч на "
    "изучение каждой такой системы, доступы и оборудование впиши в unknowns. "
    "Сайт с каталогом, калькулятором, CMS или блогом - это не визитка: считай "
    "каждый раздел отдельным компонентом. Компонент - отдельный кусок работы (например "
    "«меню и запись», «выгрузка в Google Sheets», «деплой»), у каждого часы "
    "от 0.25 до 40; компонентов не больше 8.\n"
    "category - одно из: tg_bot, parser, landing, site_fix, wp, script, "
    "integration, sheets, other.\n"
    "clarity: «ясно» (ТЗ понятно), «частично» (есть пробелы), «размыто» (непонятно, что делать).\n"
    "unknowns - неизвестные, которые могут увеличить объём (нет доступа к API, "
    "нет макетов, чужой сервер); если их нет - пустой список.\n"
    "client_budget_ok: true, если указанный бюджет адекватен объёму, false если "
    "мал; null, если бюджет не указан.\n"
    "Ответь СТРОГО одним JSON-объектом без пояснений и без markdown:\n"
    '{"fit": целое число 0-10 по шкале выше, '
    '"difficulty": "легко" | "средне" | "сложно", '
    '"category": "tg_bot" | "parser" | "landing" | "site_fix" | "wp" | "script" | '
    '"integration" | "sheets" | "other", '
    '"components": [{"name": "короткое название", "hours": число}], '
    '"unknowns": ["неизвестное"], '
    '"clarity": "ясно" | "частично" | "размыто", '
    '"client_budget_ok": true | false | null, '
    '"summary": "суть заказа в одну короткую строку", '
    '"risks": "главный риск в одну короткую строку", '
    '"questions": ["вопрос заказчику"]}'
)

YANDEX_URL = "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"
YANDEX_MAX_TOKENS = "1000"


# ---------------------------------------------------------------------------
# Бесплатные OpenAI-совместимые провайдеры: groq, cerebras, mistral, openrouter
# ---------------------------------------------------------------------------
# Порядок по живому сравнению 09.10.2026 на реальных заказах: gemini (быстрый, большой лимит) ->
# cloudflare gpt-oss-120b (лучшая детализация, 40/сутки) -> openrouter nemotron -> github (из РФ
# не отвечает, проверить с сервера) -> провайдеры без бесплатного ключа
PROVIDER_NAMES = ("gemini", "cloudflare", "openrouter", "github", "groq", "cerebras", "mistral", "yandex")
# yandex платный: в цепочку по умолчанию не входит, включается только явно
DEFAULT_CHAIN = ",".join(n for n in PROVIDER_NAMES if n != "yandex")

PROVIDER_TIMEOUT_SEC = 60            # рассуждающие модели могут думать до минуты
PROVIDER_MAX_WAIT_SEC = float(os.getenv("AI_PROVIDER_MAX_WAIT_SEC", "20"))  # дольше ждать очередь - отдаём заказ дальше
PROVIDER_COOLDOWN_SEC = 60           # пауза после 429, если срок не назван
PROVIDER_DAILY_PAUSE_SEC = 3 * 3600  # пауза при дневной квоте, если срок не назван
MIN_DAILY_PAUSE_SEC = 3600
MAX_PROVIDER_PAUSE_SEC = 6 * 3600
PROVIDER_MAX_TOKENS = 2500
ORDER_DEADLINE_SEC = float(os.getenv("AI_ORDER_DEADLINE_SEC", "120"))  # новых моделей/провайдеров после этого не начинаем
PAYMENT_REQUIRED = 402               # признак платного вызова: провайдер выключаем до рестарта

OPENROUTER_FREE_ROUTER = "openrouter/free"
OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
OPENROUTER_RECHECK_SEC = 6 * 3600    # как часто сверяться с ценами в /models
OPENROUTER_RETRY_SEC = 600           # если /models не ответил - повтор через 10 минут

# Признаки дневной квоты в тексте 429
DAILY_MARKERS = ("per day", "per-day", "daily", "tokens per day", "requests per day",
                 "(tpd)", "(rpd)", "free-models-per-day",
                 "byday", "86400")              # github: «... per 86400s exceeded for UserByModelByDay»

# base_url, ключ/префикс env, модели по умолчанию, мин. интервал (с), потолок запросов в сутки.
# Лимиты бесплатных тарифов (октябрь 2026):
#  groq: 30 RPM, 1000 RPD на крупных моделях, ещё TPM/TPD (по ним приходит 429 с retry-after);
#  cerebras: 5 RPM, 1M токенов в сутки на модель (~300 заказов по ~3 тыс. токенов);
#  mistral (тариф Experiment): ~1 запрос/с, дневного лимита нет;
#  openrouter: 50 запросов/сутки на бесплатные модели без пополнения (берём 45);
#  github (GitHub Models, бесплатно с аккаунтом GitHub): mini-модели 15 RPM и 150 RPD на модель;
#  cloudflare (Workers AI, бесплатный план): 10 000 нейронов в сутки на аккаунт; рассуждающая
#    gpt-oss-120b тратит до ~250 нейронов на заказ, поэтому потолок 40 в сутки.
#    URL содержит ID аккаунта (CLOUDFLARE_ACCOUNT_ID); без него провайдер выключен.
PROVIDER_SPECS = {
    "github": {
        "url": "https://models.github.ai/inference", "prefix": "GITHUB",
        "models": "openai/gpt-4.1-mini,openai/gpt-4o-mini",
        "interval": 4.5, "per_day": 290, "shared_daily": False,
    },
    "cloudflare": {
        "url": "https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1", "prefix": "CLOUDFLARE",
        "models": "@cf/openai/gpt-oss-120b,@cf/qwen/qwen3-30b-a3b-fp8",
        # нейроны общие на аккаунт: дневной 429 паузит весь провайдер
        # 10 000 нейронов/сутки ≈ 50-55 оценок gpt-oss-120b (замер 11.10: ~185 нейронов на заказ)
        "interval": 3.0, "per_day": 50, "shared_daily": True,
    },
    "groq": {
        "url": "https://api.groq.com/openai/v1", "prefix": "GROQ",
        "models": "openai/gpt-oss-120b,llama-3.3-70b-versatile,llama-3.1-8b-instant",
        "interval": 2.5, "per_day": 900, "shared_daily": False,
    },
    "cerebras": {
        "url": "https://api.cerebras.ai/v1", "prefix": "CEREBRAS",
        "models": "gpt-oss-120b,qwen-3.8-27b",
        "interval": 12.5, "per_day": 300, "shared_daily": False,
    },
    "mistral": {
        "url": "https://api.mistral.ai/v1", "prefix": "MISTRAL",
        "models": "mistral-small-latest,ministral-8b-latest",
        "interval": 1.2, "per_day": 1000, "shared_daily": False,
    },
    "openrouter": {
        "url": "https://openrouter.ai/api/v1", "prefix": "OPENROUTER",
        # google/gemma-4-31b-it:free убрана: на сравнении 09.10 отвечала только 429
        "models": "nvidia/nemotron-3-super-120b-a12b:free," + OPENROUTER_FREE_ROUTER,
        # лимит 50/сутки общий на все :free-модели, поэтому 429 по дню паузит весь провайдер
        "interval": 3.5, "per_day": 45, "shared_daily": True,
    },
}


@dataclass
class _Provider:
    name: str
    base_url: str
    key: str
    models: list
    min_interval: float
    max_per_day: int
    shared_daily: bool = False
    next_at: float = 0.0                 # monotonic: не раньше - следующий запрос
    day: str = ""
    count: int = 0                       # запросов за сутки (UTC)
    blocked_until: float = 0.0           # ключ отклонён - пауза всего провайдера
    dead: bool = False                   # 402: выключен до рестарта
    key_error: str = ""
    key_error_at: float = 0.0            # monotonic: когда поставлена блокировка по ключу
    paid_reason: str = ""                # признак платного вызова (402 или платная модель)
    last_error: str = ""                 # последняя ошибка этого провайдера (успех очищает)
    paused: dict = field(default_factory=dict)    # модель -> конец паузы после 429
    disabled: set = field(default_factory=set)    # модели без доступа (404) - до рестарта
    no_json: set = field(default_factory=set)     # модели без response_format
    count_loaded: bool = False          # дневной счётчик уже восстановлен из kv
    cap_logged_day: str = ""             # за какие сутки уже писали в лог об исчерпанном потолке


def _is_free_openrouter_model(model: str) -> bool:
    return model.endswith(":free") or model == OPENROUTER_FREE_ROUTER


def _split_models(raw: str) -> list[str]:
    models: list[str] = []
    for name in (raw or "").split(","):
        name = name.strip()
        if name and name not in models:
            models.append(name)
    return models


def _env_number(environ, name: str, default, cast):
    raw = (environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = cast(raw)
        if value < 0:
            raise ValueError
        return value
    except ValueError:
        logging.warning(f"[AI] {name}: некорректное значение, берём {default}")
        return default


def _build_providers(environ) -> dict:
    """Провайдеры из переменных окружения (environ - словарь вида os.environ)."""
    providers = {}
    for name, spec in PROVIDER_SPECS.items():
        prefix = spec["prefix"]
        models = _split_models(environ.get(f"{prefix}_MODELS") or spec["models"])
        if name == "openrouter":
            # только бесплатное: суффикс ":free" или "openrouter/free", остальное отбрасываем
            kept = [m for m in models if _is_free_openrouter_model(m)]
            for m in models:
                if m not in kept:
                    logging.warning(f"[AI] openrouter: модель {m} не бесплатная (нет :free), отброшена")
            models = kept
        key = (environ.get(f"{prefix}_API_KEY") or "").strip()
        base_url = spec["url"]
        if "{account_id}" in base_url:
            account = (environ.get(f"{prefix}_ACCOUNT_ID") or "").strip()
            if not account.isalnum():
                if key:
                    logging.warning(f"[AI] {name}: нет {prefix}_ACCOUNT_ID, провайдер выключен")
                key = ""                    # без аккаунта провайдер неактивен
            base_url = base_url.format(account_id=account)
        providers[name] = _Provider(
            name=name,
            base_url=base_url,
            key=key,
            models=models,
            min_interval=_env_number(environ, f"{prefix}_MIN_INTERVAL_SEC", spec["interval"], float),
            max_per_day=_env_number(environ, f"{prefix}_MAX_PER_DAY", spec["per_day"], int),
            shared_daily=spec["shared_daily"],
        )
    return providers


def _parse_chain(raw: str | None) -> list[str]:
    """Порядок провайдеров из AI_PROVIDERS; неизвестные имена пропускаются с предупреждением."""
    chain: list[str] = []
    for name in (raw or DEFAULT_CHAIN).split(","):
        name = name.strip().lower()
        if not name or name in chain:
            continue
        if name not in PROVIDER_NAMES:
            logging.warning(f"[AI] AI_PROVIDERS: неизвестный провайдер {name!r}, пропущен")
            continue
        chain.append(name)
    return chain


PROVIDERS = _build_providers(os.environ)


def _resolve_chain(raw: str | None) -> list[str]:
    """Цепочка из AI_PROVIDERS. Пустая (опечатки) или без провайдеров с ключами -
    ERROR в лог и цепочка по умолчанию, чтобы заказы не уходили «без AI» из-за опечатки."""
    chain = _parse_chain(raw)
    if raw and (not chain or not _chain_active(chain)):
        logging.error(f"[AI] AI_PROVIDERS={raw!r}: нет ни одного рабочего провайдера с ключом, "
                      f"беру цепочку по умолчанию ({DEFAULT_CHAIN})")
        chain = _parse_chain(DEFAULT_CHAIN)
    return chain

_or_lock = threading.Lock()
_or_next_check = 0.0                 # monotonic: когда сверяться с /models в следующий раз
_or_paid: set[str] = set()           # модели OpenRouter с ненулевой ценой по /models


def _chain_active(chain=None) -> list[str]:
    """Провайдеры цепочки, у которых есть ключи (yandex - ещё и YANDEX_ENABLED=1)."""
    active = []
    for name in (CHAIN if chain is None else chain):
        if name == "gemini":
            ok = bool(GEMINI_KEY)
        elif name == "yandex":
            ok = YANDEX_ENABLED
        else:
            p = PROVIDERS.get(name)
            ok = bool(p and p.key)
        if ok:
            active.append(name)
    return active


def is_enabled() -> bool:
    return bool(_chain_active())


CHAIN = _resolve_chain(os.getenv("AI_PROVIDERS"))


def _yandex_headers() -> dict:
    # IAM-токены начинаются с "t1." (Bearer), остальное - API-ключ сервисного аккаунта
    scheme = "Bearer" if (API_KEY or "").startswith("t1.") else "Api-Key"
    return {"Authorization": f"{scheme} {API_KEY}", "x-folder-id": FOLDER_ID or ""}


def _expire_key_error(p, now: float) -> None:
    """Пауза после отклонённого ключа истекла - провайдер снова пробуется (под _lock).
    key_error тут НЕ стираем: предупреждение снимает только реальный ответ не 401/403
    (иначе «отклонён/принят» мигало бы после каждой паузы)."""
    if p.blocked_until and now >= p.blocked_until:
        p.blocked_until = 0.0


def status() -> dict:
    """Состояние AI для статуса и оповещений (без секретов)."""
    now = time.monotonic()
    with _lock:
        paused = {m: int(t - now) for m, t in _paused_until.items()
                  if t > now and m not in _disabled}
        today = _today()
        providers = {}
        key_errors = {}
        paid_errors = {}
        if _config_error:
            key_errors["gemini"] = _config_error
        for name, p in PROVIDERS.items():
            _expire_key_error(p, now)
            providers[name] = {
                "enabled": bool(p.key) and name in CHAIN,
                "has_key": bool(p.key),
                "models": [m for m in p.models if m not in p.disabled],
                "paused": {m: int(t - now) for m, t in p.paused.items()
                           if t > now and m not in p.disabled},
                "disabled": [m for m in p.models if m in p.disabled],
                "blocked_sec": max(0, int(p.blocked_until - now)),
                "dead": p.dead,
                "paid_reason": p.paid_reason,
                "today": p.count if p.day == today else 0,
                "limit": p.max_per_day,
                "key_error": p.key_error,
                "last_error": p.last_error,
            }
            if p.key_error:
                key_errors[name] = p.key_error
            if p.paid_reason:
                paid_errors[name] = p.paid_reason
        return {
            "enabled": is_enabled(),
            "chain": list(CHAIN),
            "providers": providers,
            "key_errors": key_errors,
            "paid_errors": paid_errors,
            "gemini": bool(GEMINI_KEY),
            "models": [m for m in GEMINI_MODELS if m not in _disabled],
            "paused": paused,
            "disabled": [m for m in GEMINI_MODELS if m in _disabled],
            "yandex": YANDEX_ENABLED,
            "last_ok": dict(_last_ok) if _last_ok else None,
            "config_error": _config_error,
            "region_error": _region_error,
            "last_error": _last_error,
            "gemini_last_error": _gemini_last_error,
        }


def failure_summary(now: datetime | None = None) -> str:
    """Почему не ответил никто из цепочки: короткая строка на каждого активного провайдера
    (для алерта «AI-оценка заказов»). now - aware datetime, по умолчанию текущее время."""
    from zoneinfo import ZoneInfo
    msk = ZoneInfo("Europe/Moscow")
    now = now or datetime.now(timezone.utc)
    mono = time.monotonic()
    today = now.astimezone(timezone.utc).strftime("%Y-%m-%d")
    # сброс дневных потолков - полночь UTC (03:00 МСК)
    midnight = (now.astimezone(timezone.utc) + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0)

    def at_msk(dt: datetime) -> str:
        return dt.astimezone(msk).strftime("%H:%M")

    def wall(t: float) -> datetime:
        # monotonic -> настенное время (через now, чтобы не зависеть от time.time())
        return datetime.fromtimestamp(now.timestamp() + (t - mono), timezone.utc)

    parts: list[str] = []
    resumes: list[datetime] = []       # когда вернётся каждый провайдер, остановленный квотой
    blocked_count = 0
    active = _chain_active()
    with _lock:
        for name in active:
            if name == "gemini":
                live = [m for m in GEMINI_MODELS if m not in _disabled]
                ends = [_paused_until.get(m, 0.0) for m in live]
                if _region_error and mono < _config_blocked_until:
                    # регион - не квота: ни в resumes, ни в blocked_count
                    parts.append("gemini: регион сервера не поддерживается, "
                                 f"повтор в {at_msk(wall(_config_blocked_until))} МСК")
                elif _config_error:
                    parts.append(f"gemini: {_config_error}")
                elif live and all(t > mono for t in ends):
                    resumes.append(wall(min(ends)))
                    blocked_count += 1
                    parts.append(f"gemini: на паузе до {at_msk(wall(max(ends)))} МСК")
                else:
                    parts.append(f"gemini: {_gemini_last_error or 'нет ответа'}")
                continue
            if name == "yandex":
                if _yandex_day == today and _yandex_count >= YANDEX_MAX_PER_DAY:
                    resumes.append(midnight)
                    blocked_count += 1
                    parts.append(f"yandex: лимит {YANDEX_MAX_PER_DAY}/{YANDEX_MAX_PER_DAY} за сутки")
                else:
                    parts.append("yandex: нет ответа")
                continue
            p = PROVIDERS[name]
            live = [m for m in p.models if m not in p.disabled]
            ends = [p.paused.get(m, 0.0) for m in live]
            if p.dead:
                parts.append(f"{name}: {p.paid_reason or 'выключен'}")
            elif p.key_error:
                parts.append(f"{name}: {p.key_error}")
            elif p.day == today and p.count >= p.max_per_day:
                resumes.append(midnight)
                blocked_count += 1
                parts.append(f"{name}: лимит {p.max_per_day}/{p.max_per_day} за сутки")
            elif live and all(t > mono for t in ends):
                resumes.append(wall(min(ends)))
                blocked_count += 1
                parts.append(f"{name}: на паузе до {at_msk(wall(max(ends)))} МСК")
            else:
                err = p.last_error or "нет ответа"
                parts.append(err if err.startswith(f"{name}:") else f"{name}: {err}")
    text = " · ".join(parts)
    if active and blocked_count == len(active) and resumes:
        text = (f"кончились бесплатные квоты, оценка возобновится ~{at_msk(min(resumes))} МСК "
                f"(ближайший срок): {text}")
    return text


def _clean_float(value) -> float:
    try:
        f = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    return f if math.isfinite(f) else 0.0


CATEGORIES = ("tg_bot", "parser", "landing", "site_fix", "wp", "script",
              "integration", "sheets", "other")
CLARITY_VALUES = ("ясно", "частично", "размыто")
MIN_COMPONENT_HOURS = 0.25
MAX_COMPONENT_HOURS = 40.0
MAX_COMPONENTS = 8
MAX_UNKNOWNS = 5
MAX_QUESTIONS = 3


def _parse_hours(value) -> float:
    """Часы из числа или строки ("2-3", "2 ч", "1,5"): первое число, запятая как точка."""
    if isinstance(value, str):
        m = re.search(r"\d+(?:[.,]\d+)?", value)
        if not m:
            return 0.0
        value = m.group(0).replace(",", ".")
    return max(0.0, _clean_float(value))


def _clean_components(raw) -> list[dict]:
    """Компоненты работы: часы 0.25-40 (вне диапазона - в границу), не больше MAX_COMPONENTS."""
    if not isinstance(raw, list):
        return []
    result = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        hours = _parse_hours(item.get("hours"))
        name = str(item.get("name") or "").strip()[:80]
        if hours <= 0 or not name:
            continue
        result.append({"name": name,
                       "hours": round(min(MAX_COMPONENT_HOURS, max(MIN_COMPONENT_HOURS, hours)), 2)})
        if len(result) >= MAX_COMPONENTS:
            break
    return result


def _clean_str_list(raw, limit: int, max_len: int) -> list[str]:
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    result = []
    for item in raw:
        text = str(item or "").strip()[:max_len]
        if text:
            result.append(text)
        if len(result) >= limit:
            break
    return result


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
    difficulty = str(data.get("difficulty") or "").strip().lower()
    if difficulty not in ("легко", "средне", "сложно"):
        difficulty = ""
    category = str(data.get("category") or "").strip().lower()
    if category not in CATEGORIES:
        category = "other"
    components = _clean_components(data.get("components"))
    if components:
        hours = sum(c["hours"] for c in components)  # часы считает код, не модель
    else:
        # старый формат; NaN/Infinity -> 0; потолок как у суммы компонентов
        hours = min(_parse_hours(data.get("hours")), MAX_COMPONENT_HOURS * MAX_COMPONENTS)
    clarity = str(data.get("clarity") or "").strip().lower()
    if clarity not in CLARITY_VALUES:
        clarity = ""
    budget_ok = data.get("client_budget_ok")
    if not isinstance(budget_ok, bool):
        budget_ok = None
    return {
        "fit": max(0, min(10, fit)),
        "difficulty": difficulty,
        "hours": round(hours, 1),
        "category": category,
        "components": components,
        "unknowns": _clean_str_list(data.get("unknowns"), MAX_UNKNOWNS, 120),
        "clarity": clarity,
        "client_budget_ok": budget_ok,
        "price_rub": str(data.get("price_rub") or "").strip()[:100],  # только старые записи
        "summary": str(data.get("summary") or "").strip()[:300],
        "risks": str(data.get("risks") or "").strip()[:300],
        "questions": _clean_str_list(data.get("questions"), MAX_QUESTIONS, 200),
    }


def score_order(order) -> dict | None:
    """Оценка заказа: провайдеры по порядку CHAIN (AI_PROVIDERS); провайдер без ключа
    пропускается, при лимите/сбое заказ сразу уходит следующему. Возвращает dict оценки
    (с ключом "model") или None: AI недоступен или никто не дал разбираемый ответ."""
    deadline = time.monotonic() + ORDER_DEADLINE_SEC
    for name in _chain_active():
        if time.monotonic() >= deadline:
            logging.warning(f"[AI] дедлайн {ORDER_DEADLINE_SEC:g} с на заказ вышел, дальше не пробуем")
            break
        try:
            if name == "gemini":
                result = _ask_gemini_chain(order, deadline)
            elif name == "yandex":
                result = _ask_yandex_lite(order) if YANDEX_ENABLED else None
            else:
                result = _ask_provider(PROVIDERS[name], order, deadline)
        except Exception:
            logging.exception(f"[AI] {name}: неожиданный сбой, берём следующего")
            continue
        if result is not None:
            return result
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


def _mark_ok(model: str, gemini: bool = True, provider=None) -> None:
    """Запоминает удачную оценку. Успех Yandex не снимает ошибку ключа Gemini.
    provider - _Provider, чья ошибка очищается (для gemini очищается _gemini_last_error)."""
    global _last_ok, _config_error, _config_blocked_until, _last_error, _gemini_last_error
    global _region_error
    with _lock:
        _last_ok = {"model": model, "at": datetime.now(timezone.utc).isoformat()}
        if gemini:
            _config_error = ""
            _region_error = ""
            _config_blocked_until = 0.0
            _last_error = ""
            _gemini_last_error = ""
        if provider is not None:
            provider.last_error = ""


def _set_error(text: str, provider=None) -> None:
    """Пишет глобальную _last_error (её читают bot.py и тесты) и ошибку конкретного
    провайдера: _Provider или строка "gemini"."""
    global _last_error, _gemini_last_error
    with _lock:
        _last_error = text
        if provider == "gemini":
            _gemini_last_error = text
        elif provider is not None:
            provider.last_error = text


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


def _quota_pause(resp) -> tuple[int, bool]:
    """Пауза модели после 429: столько, сколько велел Google (RetryInfo.retryDelay),
    иначе GEMINI_COOLDOWN_SEC. Второе значение - исчерпана ли дневная квота.

    При дневной квоте retryDelay - часы до сброса: без этого модель долбилась бы
    каждые 2 минуты, а каждый заказ ждал бы её очереди в общем темпе запросов.
    """
    pause, daily = GEMINI_COOLDOWN_SEC, False
    try:
        details = (resp.json().get("error") or {}).get("details") or []
        for d in details if isinstance(details, list) else []:
            if not isinstance(d, dict):
                continue
            violations = d.get("violations")
            for v in violations if isinstance(violations, list) else []:
                if isinstance(v, dict) and "PerDay" in str(v.get("quotaId", "")):
                    daily = True
            m = re.fullmatch(r"(\d+(?:\.\d+)?)s", str(d.get("retryDelay", "")))
            if m:
                pause = max(1, min(MAX_QUOTA_PAUSE_SEC, math.ceil(float(m.group(1)))))
    except Exception:
        return GEMINI_COOLDOWN_SEC, False
    if daily:
        # Дневная квота сбрасывается в полночь по тихоокеанскому времени; короткий
        # retryDelay тут не верим - иначе модель опрашивалась бы до конца суток
        pause = max(pause, _until_quota_reset())
    return pause, daily


def _until_quota_reset() -> int:
    """Секунд до полуночи America/Los_Angeles (сброс дневных квот Gemini) + 1 мин."""
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo("America/Los_Angeles"))
    except Exception:
        return 3600
    midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    # через UTC: вычитание в одной зоне не учитывает переход на летнее/зимнее время
    delta = midnight.astimezone(timezone.utc) - now.astimezone(timezone.utc)
    return min(MAX_QUOTA_PAUSE_SEC, int(delta.total_seconds()) + 60)


def _body_info(resp) -> tuple[str, str]:
    """content-type и начало тела ответа одной строкой (для логов; без заголовков)."""
    try:
        ctype = str((resp.headers or {}).get("content-type") or (resp.headers or {}).get("Content-Type") or "")
    except Exception:
        ctype = ""
    text = " ".join(str(resp.text or "").split())
    return ctype or "?", text


def _ask_gemini_chain(order, deadline: float | None = None) -> dict | None:
    with _lock:
        if time.monotonic() < _config_blocked_until:
            return None  # недавно ключ был отклонён: не долбим
    for model in GEMINI_MODELS:
        if deadline is not None and time.monotonic() >= deadline:
            return None
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
    global _config_error, _config_blocked_until, _region_error
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
        _set_error(f"{model}: {type(e).__name__}", "gemini")
        return "next"

    code = resp.status_code
    if code == 429:
        pause, daily = _quota_pause(resp)
        with _lock:
            # max: параллельный короткий 429 не затирает паузу дневной квоты
            _paused_until[model] = max(_paused_until.get(model, 0.0), time.monotonic() + pause)
        kind = "дневная квота исчерпана" if daily else "лимит запросов"
        logging.warning(f"[AI] {model}: {kind}, пауза {pause} с, берём следующую")
        _set_error(f"{model}: HTTP 429 ({kind})", "gemini")
        return "next"
    if code != 200:
        body = resp.text[:400]
        if code == 400 and "User location is not supported" in body:
            # регион сервера не поддерживается - это не ключ: блокируем весь Gemini на время
            with _lock:
                _region_error = REGION_ERROR_TEXT
                _config_blocked_until = time.monotonic() + REGION_BLOCK_COOLDOWN_SEC
            logging.warning(f"[AI] {REGION_ERROR_TEXT}, Gemini отключён на "
                            f"{REGION_BLOCK_COOLDOWN_SEC // 60} мин")
            _set_error(REGION_ERROR_TEXT, "gemini")
            return "config"
        if code in (400, 401, 403) and any(m in body for m in KEY_ERROR_MARKERS):
            reason = f"ключ Gemini отклонён (HTTP {code})"
            with _lock:
                _config_error = reason
                _config_blocked_until = time.monotonic() + CONFIG_ERROR_COOLDOWN_SEC
            logging.error(f"[AI] {reason}")
            _set_error(reason, "gemini")
            return "config"
        if code == 404 or "is not found" in body or "no longer available" in body:
            with _lock:
                _disabled.add(model)
            logging.warning(f"[AI] модель {model} недоступна (HTTP {code}), выключена до рестарта")
            _set_error(f"{model}: недоступна (HTTP {code})", "gemini")
            return "next"
        logging.warning(f"[AI] {model}: HTTP {code}: {body[:150]!r}")
        _set_error(f"{model}: HTTP {code}", "gemini")
        return "next"

    try:
        payload = resp.json()
        cand = (payload.get("candidates") or [{}])[0]
        parts = (cand.get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
    except Exception as e:
        ctype, snippet = _body_info(resp)
        logging.warning(f"[AI] {model}: не разобран ответ: {type(e).__name__} "
                        f"({ctype}): {snippet[:200]!r}")
        _set_error(f"{model}: нечитаемый ответ", "gemini")
        return "next"
    if not text:
        # safety-блок (promptFeedback.blockReason) или пустой ответ: следующая модель
        reason = (cand.get("finishReason")
                  or (payload.get("promptFeedback") or {}).get("blockReason") or "пусто")
        logging.warning(f"[AI] {model}: пустой ответ ({reason})")
        _set_error(f"{model}: пустой ответ ({reason})", "gemini")
        return "next"
    parsed = parse_ai_json(text)
    if parsed is None:
        logging.warning(f"[AI] {model}: ответ не JSON: {text[:150]!r}")
        _set_error(f"{model}: ответ не JSON", "gemini")
        return "next"
    parsed["model"] = model
    _mark_ok(model)
    return parsed


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _parse_duration(value) -> float | None:
    """Секунды из "30", "7.66s", "2m59.5s", "150ms", а также epoch (сек или мс) до сброса."""
    s = str(value or "").strip().lower()
    if not s:
        return None
    try:
        f = float(s)
    except ValueError:
        if not re.fullmatch(r"(?:\d+(?:\.\d+)?(?:ms|h|m|s))+", s):
            return None
        factor = {"h": 3600.0, "m": 60.0, "s": 1.0, "ms": 0.001}
        return sum(float(n) * factor[u] for n, u in re.findall(r"(\d+(?:\.\d+)?)(ms|h|m|s)", s))
    if not math.isfinite(f):
        return None
    if f > 1e12:       # epoch в миллисекундах (OpenRouter)
        f = f / 1000 - time.time()
    elif f > 1e9:      # epoch в секундах
        f -= time.time()
    return max(0.0, f)


def _parse_retry_after(value) -> float | None:
    sec = _parse_duration(value)
    if sec is not None:
        return sec
    try:  # HTTP-date
        dt = email.utils.parsedate_to_datetime(str(value))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (dt - datetime.now(timezone.utc)).total_seconds())
    except Exception:
        return None


def _provider_pause(resp) -> tuple[int, bool]:
    """Пауза после 429: Retry-After, затем x-ratelimit-reset-* исчерпанных корзин
    (remaining = 0). Второе значение - дневная квота (по заголовкам/тексту/сроку > 1 ч).
    Дневная пауза - от 1 до 6 часов; срок не назван - 3 часа."""
    try:
        headers = {str(k).lower(): v for k, v in dict(resp.headers or {}).items()}
    except Exception:
        headers = {}
    try:
        text = str(resp.text or "")[:600].lower()
    except Exception:
        text = ""
    daily = any(marker in text for marker in DAILY_MARKERS)
    candidates = []
    if headers.get("retry-after") is not None:
        sec = _parse_retry_after(headers["retry-after"])
        if sec is not None:
            candidates.append(sec)
    prefix = "x-ratelimit-reset"
    for name, value in headers.items():
        if not name.startswith(prefix):
            continue
        suffix = name[len(prefix):]
        remaining = headers.get("x-ratelimit-remaining" + suffix)
        if remaining is not None:
            try:
                if float(remaining) > 0:
                    continue  # корзина не исчерпана: её сброс к 429 не относится
            except (TypeError, ValueError):
                pass
        sec = _parse_duration(value)
        if sec is None:
            continue
        candidates.append(sec)
        if "day" in suffix:
            daily = True
    pause = max(candidates) if candidates else None
    if pause is not None and pause >= MIN_DAILY_PAUSE_SEC:
        daily = True
    if daily:
        pause = PROVIDER_DAILY_PAUSE_SEC if pause is None else pause
        pause = max(MIN_DAILY_PAUSE_SEC, pause)
    elif pause is None:
        pause = PROVIDER_COOLDOWN_SEC
    return int(max(1, min(MAX_PROVIDER_PAUSE_SEC, math.ceil(pause)))), daily


_count_load_lock = threading.Lock()   # загрузка счётчиков из kv (держим только на время чтения)
_count_save_lock = threading.Lock()   # пара «снимок + запись» в kv: последняя запись - самая свежая


def _load_count(p: _Provider) -> None:
    """Один раз за процесс подтягивает дневной счётчик провайдера из kv (ключ
    ai_count_<имя> = "YYYY-MM-DD:N"), чтобы рестарт не обнулял бесплатную квоту.
    Остальные потоки ждут загрузку (иначе заняли бы слот до неё и потеряли сохранённое N).
    Любая ошибка БД или чужое значение - работаем как раньше, только в памяти."""
    with _count_load_lock:
        if p.count_loaded:
            return
        try:
            try:
                from database import kv_get
                raw = kv_get(f"ai_count_{p.name}")
            except Exception as e:
                logging.warning(f"[AI] {p.name}: счётчик за сутки не прочитан из БД: {type(e).__name__}")
                return
            day, sep, num = str(raw or "").partition(":")
            if not (sep and num.isdigit() and re.fullmatch(r"\d{4}-\d{2}-\d{2}", day)):
                return
            with _lock:
                if day == _today() and p.day in ("", day):
                    p.day, p.count = day, max(p.count, int(num))
        finally:
            p.count_loaded = True


def _save_count(p: _Provider) -> None:
    """Записывает дневной счётчик провайдера в kv. Снимок и запись - под _count_save_lock,
    чтобы старый снимок не перезаписал новый; сам _lock во время записи не держим."""
    with _count_save_lock:
        with _lock:
            value = f"{p.day}:{p.count}"
        try:
            from database import kv_set
            kv_set(f"ai_count_{p.name}", value)
        except Exception as e:
            logging.warning(f"[AI] {p.name}: счётчик за сутки не записан в БД: {type(e).__name__}")


def _give_back_slot(p: _Provider) -> None:
    """Запрос не дал ответа от провайдера (сеть, 5xx, не-JSON): слот дневного лимита возвращаем."""
    with _lock:
        p.count = max(0, p.count - 1)
    _save_count(p)


def _reserve_slot(p: _Provider) -> float | None:
    """Занимает слот запроса у провайдера: дневной потолок и свой интервал между запросами.
    Возвращает, сколько секунд ждать до запроса, или None - провайдер сейчас не годится
    (потолок за сутки или очередь длиннее PROVIDER_MAX_WAIT_SEC: заказ уйдёт дальше)."""
    _load_count(p)
    with _lock:
        now = time.monotonic()
        if p.dead or now < p.blocked_until:
            return None
        today = _today()
        if p.day != today:
            p.day, p.count = today, 0
        if p.count >= p.max_per_day:
            # warning раз в сутки: иначе провайдер молча пропадает из логов до 03:00 МСК
            if p.cap_logged_day != today:
                p.cap_logged_day = today
                logging.warning(f"[AI] {p.name}: дневной потолок {p.max_per_day} запросов исчерпан, "
                                f"пропускаем до 03:00 МСК")
            return None
        start = max(now, p.next_at)
        if start - now > PROVIDER_MAX_WAIT_SEC:
            logging.debug(f"[AI] {p.name}: очередь запросов длиннее {PROVIDER_MAX_WAIT_SEC:g} с")
            return None
        p.count += 1
        p.next_at = start + p.min_interval
    _save_count(p)
    return start - now


CAPACITY_PAUSE_SEC = 3600   # провайдер на паузе дольше этого - его остаток квоты сейчас не в счёт


def _gemini_available() -> bool:
    """Gemini может ответить прямо сейчас (под _lock): нет блокировки по ключу/региону
    и есть модель не на паузе. Ошибка региона снимается только успешным ответом,
    поэтому между повторами раз в 30 мин Gemini «доступным» не считается."""
    now = time.monotonic()
    if _region_error or now < _config_blocked_until:
        return False
    return any(m not in _disabled and now >= _paused_until.get(m, 0.0) for m in GEMINI_MODELS)


def daily_capacity() -> tuple[int, int] | None:
    """(осталось, всего) запросов на сегодня у провайдеров цепочки с дневным потолком
    (с ключом и не выключенных из-за оплаты). None - резерв не нужен: таких провайдеров
    нет или доступен Gemini (у него нет нашего потолка, лимиты он сообщает сам).
    Yandex не считаем: платный и ограничен отдельно. Провайдер, который сейчас не ответит
    (ключ отклонён, все модели выключены или на паузе дольше часа - например, дневной 429),
    входит в «всего», но его остаток не считается."""
    left = total = 0
    found = False
    active = _chain_active()
    if "gemini" in active:
        with _lock:
            if _gemini_available():
                return None
    for name in active:
        p = PROVIDERS.get(name)
        if p is None:
            continue
        _load_count(p)
        with _lock:
            if p.dead:
                continue
            now = time.monotonic()
            usable = now >= p.blocked_until and any(
                m not in p.disabled and p.paused.get(m, 0.0) - now < CAPACITY_PAUSE_SEC
                for m in p.models)
            used = p.count if p.day == _today() else 0
            total += p.max_per_day
            if usable:
                left += max(0, p.max_per_day - used)
            found = True
    return (left, total) if found else None


def _is_paid(pricing) -> bool:
    """Платная ли модель по pricing из /models: любое ненулевое поле (prompt, completion,
    request, image, web_search, internal_reasoning...) - платная. Нет pricing, нечитаемое
    значение - тоже платная (неизвестно - не рискуем)."""
    if not isinstance(pricing, dict):
        return True
    for value in pricing.values():
        if value is None or isinstance(value, bool):
            continue
        try:
            if Decimal(str(value)) != 0:
                return True
        except InvalidOperation:
            return True
    return False


def _refresh_openrouter_prices(p: _Provider) -> None:
    """Сверка с публичным /models (не чаще раза в 6 ч): модели с ненулевой ценой исключаются.
    Если запрос не удался - работаем по суффиксу :free, повтор через 10 минут."""
    global _or_next_check, _or_paid
    import requests
    with _or_lock:
        now = time.monotonic()
        if now < _or_next_check:
            return
        try:
            resp = requests.get(OPENROUTER_MODELS_URL, timeout=15)
            resp.raise_for_status()
            data = resp.json().get("data") or []
            paid = {m["id"] for m in data
                    if isinstance(m, dict) and m.get("id") and _is_paid(m.get("pricing"))}
            _or_paid = paid
            _or_next_check = now + OPENROUTER_RECHECK_SEC
            for m in p.models:
                if m in paid:
                    logging.warning(f"[AI] openrouter: модель {m} платная по /models, исключена")
        except Exception as e:
            logging.warning(f"[AI] openrouter: /models недоступен ({type(e).__name__}), "
                            f"работаем по суффиксу :free")
            _or_next_check = now + OPENROUTER_RETRY_SEC


def _provider_models(p: _Provider) -> list[str]:
    if p.name != "openrouter":
        return list(p.models)
    _refresh_openrouter_prices(p)
    with _or_lock:
        paid = set(_or_paid)
    return [m for m in p.models if m not in paid and _is_free_openrouter_model(m)]


def _provider_headers(p: _Provider) -> dict:
    headers = {"Authorization": f"Bearer {p.key}", "Content-Type": "application/json"}
    if p.name == "openrouter":
        # только название бота; никаких личных данных
        headers["X-Title"] = "job-parser-bot"
        referer = (os.getenv("OPENROUTER_REFERER") or "").strip()
        if referer:
            headers["HTTP-Referer"] = referer
    return headers


def _provider_body(model: str, order, use_json: bool, openrouter: bool = False) -> dict:
    # Никаких plugins/web search/«:online»/fallback-списков моделей - они могут быть платными
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": _user_prompt(order)},
        ],
        "temperature": 0.2,
        "max_tokens": PROVIDER_MAX_TOKENS,
    }
    if openrouter:
        body["usage"] = {"include": True}  # учёт расхода: в ответе usage.cost, по нему ловим платное
    if use_json:
        body["response_format"] = {"type": "json_object"}
    return body


def _usable(p: _Provider, model: str) -> str | None:
    """Можно ли слать запрос к модели прямо сейчас (под _lock). None - можно;
    "stop" - провайдер выключен (402/ключ/платная модель); "next" - пропустить модель."""
    now = time.monotonic()
    if p.dead or now < p.blocked_until:
        return "stop"
    if model in p.disabled or now < p.paused.get(model, 0.0):
        return "next"
    return None


def _ask_provider(p: _Provider, order, deadline: float | None = None) -> dict | None:
    """Модели провайдера по очереди; None - никто не ответил (заказ идёт дальше)."""
    with _lock:
        _expire_key_error(p, time.monotonic())
        if p.dead or time.monotonic() < p.blocked_until:
            return None
    for model in _provider_models(p):
        if deadline is not None and time.monotonic() >= deadline:
            return None
        with _lock:
            state = _usable(p, model)
        if state == "stop":
            return None
        if state == "next":
            continue
        outcome = _ask_provider_model(p, model, order)
        if isinstance(outcome, dict):
            return outcome
        if outcome == "stop":
            return None
    return None


def _content_text(content) -> str | None:
    """Текст ответа: строка или список частей ({"text": ...}/строки); иначе None."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(part["text"])
        return "".join(parts) if parts else None
    return None


def _openrouter_response_is_paid(payload) -> bool:
    """Ответ OpenRouter пришёл от платной модели. Решает прежде всего usage.cost
    (просим его через usage.include): > 0 - платный, 0 - бесплатный (model для
    openrouter/free может быть любым). Нет cost: платный, если model есть и без :free.
    Нет ни cost, ни model - не считаем платным (запрашивались только :free-модели)."""
    if not isinstance(payload, dict):
        return False
    usage = payload.get("usage")
    cost = usage.get("cost") if isinstance(usage, dict) else None
    if cost is not None:
        try:
            return Decimal(str(cost)) != 0
        except InvalidOperation:
            return True
    model = payload.get("model")
    if isinstance(model, str) and model:
        return not model.endswith(":free")
    return False


def _mark_paid(p: _Provider, reason: str) -> None:
    with _lock:
        p.dead = True
        p.paid_reason = reason
    logging.error(f"[AI] {reason}: платные вызовы недопустимы, провайдер выключен до рестарта")
    _set_error(reason, p)


def _ask_provider_model(p: _Provider, model: str, order):
    """Один заказ - одна модель. dict - успех; "next" - следующая модель;
    "stop" - провайдер для этого заказа не годится (ключ, 402, потолок, очередь)."""
    import requests
    label = f"{p.name}:{model}"
    with _lock:
        use_json = model not in p.no_json
    resp = None
    for attempt in (1, 2):
        wait = _reserve_slot(p)   # внутри под _lock проверяет dead/blocked_until
        if wait is None:
            return "stop"
        if wait > 0:
            time.sleep(wait)
            # за время ожидания другой поток мог получить 402/401 или 429 на эту модель
            with _lock:
                state = _usable(p, model)
                if state:
                    p.count = max(0, p.count - 1)  # запрос не ушёл - слот возвращаем
            if state:
                _save_count(p)
                return state
        sent_at = time.monotonic()
        try:
            resp = requests.post(
                f"{p.base_url}/chat/completions",
                headers=_provider_headers(p),
                json=_provider_body(model, order, use_json, p.name == "openrouter"),
                timeout=PROVIDER_TIMEOUT_SEC,
            )
        except Exception as e:
            logging.warning(f"[AI] {label}: сбой запроса: {type(e).__name__}")
            _set_error(f"{label}: {type(e).__name__}", p)
            if not isinstance(e, requests.Timeout):
                _give_back_slot(p)   # запрос не дошёл - слот не тратим (при таймауте провайдер мог засчитать)
            return "next"
        if resp.status_code == 400 and use_json and attempt == 1:
            body = str(resp.text or "")[:600].lower()
            # json_validate_failed - модель дала кривой JSON, а не "формат не поддержан"
            if "json_validate_failed" not in body and "response_format" in body and any(
                    m in body for m in ("not supported", "unsupported", "invalid")):
                with _lock:
                    p.no_json.add(model)
                use_json = False
                logging.info(f"[AI] {label}: response_format не поддержан, повтор без него")
                continue
        break

    code = resp.status_code
    if code not in (401, 403):
        # ключ принят (реальный ответ не 401/403) - но только если запрос ушёл ПОСЛЕ
        # установки блокировки: ответ на старый запрос не должен её стирать
        with _lock:
            if p.key_error and sent_at > p.key_error_at:
                p.key_error = ""
                p.blocked_until = 0.0
    if code == 429:
        pause, daily = _provider_pause(resp)
        until = time.monotonic() + pause
        with _lock:
            targets = p.models if (daily and p.shared_daily) else [model]
            for m in targets:
                # max: параллельный короткий 429 не затирает паузу дневной квоты
                p.paused[m] = max(p.paused.get(m, 0.0), until)
        kind = "дневная квота исчерпана" if daily else "лимит запросов"
        logging.warning(f"[AI] {label}: {kind}, пауза {pause} с, берём следующую")
        _set_error(f"{label}: HTTP 429 ({kind})", p)
        return "next"
    if code == PAYMENT_REQUIRED:
        _mark_paid(p, f"{p.name}: HTTP 402 (требуется оплата)")
        return "stop"
    if code in (401, 403):
        body = str(resp.text or "")[:600].lower()
        if code == 403 and any(m in body for m in ("moderation", "flagged", "content policy",
                                                    "content_policy")):
            # отказ по содержанию заказа, а не по ключу
            logging.warning(f"[AI] {label}: HTTP 403 (модерация), берём следующую")
            _set_error(f"{label}: HTTP 403 (модерация)", p)
            return "next"
        reason = f"ключ {p.name} отклонён (HTTP {code})"
        with _lock:
            p.key_error = reason
            p.key_error_at = time.monotonic()
            p.blocked_until = p.key_error_at + CONFIG_ERROR_COOLDOWN_SEC
        logging.error(f"[AI] {reason}")
        _set_error(reason, p)
        return "stop"
    if code == 404:
        with _lock:
            p.disabled.add(model)
        logging.warning(f"[AI] {label}: модель недоступна (HTTP 404), выключена до рестарта")
        _set_error(f"{label}: недоступна (HTTP 404)", p)
        return "next"
    if code != 200:
        logging.warning(f"[AI] {label}: HTTP {code}: {str(resp.text or '')[:150]!r}")
        _set_error(f"{label}: HTTP {code}", p)
        if code >= 500:
            _give_back_slot(p)   # сбой на стороне провайдера - слот не тратим
        return "next"

    try:
        payload = resp.json()
    except Exception as e:
        ctype, snippet = _body_info(resp)
        logging.warning(f"[AI] {label}: ответ не JSON: {type(e).__name__} ({ctype}): {snippet[:200]!r}")
        _set_error(f"{label}: ответ не JSON ({ctype}): «{snippet[:80]}»", p)
        _give_back_slot(p)       # настоящего ответа не было - слот не тратим
        html_page = "text/html" in ctype.lower()
        if html_page or not snippet:
            # HTML - скорее всего страница блокировки/прокси: пауза всего провайдера;
            # пустое тело - только этой модели
            until = time.monotonic() + NON_JSON_PAUSE_SEC
            with _lock:
                for m in (p.models if html_page else [model]):
                    p.paused[m] = max(p.paused.get(m, 0.0), until)
            logging.warning(f"[AI] {label}: пустой/HTML-ответ, пауза {NON_JSON_PAUSE_SEC} с")
        return "next"
    # платность - до разбора choices: платный ответ с кривой структурой тоже должен выключить провайдера
    if p.name == "openrouter" and _openrouter_response_is_paid(payload):
        _mark_paid(p, f"{label}: ответ платной модели ({str(payload.get('model'))[:60]})")
        return "stop"
    try:
        choice = payload["choices"][0]
    except Exception:
        choice = None
    if not isinstance(choice, dict):
        logging.warning(f"[AI] {label}: нет choices в ответе: {str(resp.text or '')[:200]!r}")
        _set_error(f"{label}: нет choices", p)
        return "next"
    finish = choice.get("finish_reason")
    message = choice.get("message")
    text = _content_text(message.get("content")) if isinstance(message, dict) else None
    if text is None:
        # GitHub Models/Azure: сработал content filter - content: null, finish_reason: content_filter
        if finish == "content_filter" or "content_filter" in str(resp.text or "")[:2000]:
            logging.warning(f"[AI] {label}: отказ модерации (content_filter)")
            _set_error(f"{label}: отказ модерации", p)
            return "next"
        logging.warning(f"[AI] {label}: пустой ответ (finish_reason={finish}): "
                        f"{str(resp.text or '')[:200]!r}")
        _set_error(f"{label}: пустой ответ (finish_reason={finish})", p)
        return "next"
    parsed = parse_ai_json(text)
    if parsed is None:
        logging.warning(f"[AI] {label}: ответ не JSON: {text[:150]!r}")
        _set_error(f"{label}: ответ не JSON", p)
        return "next"
    parsed["model"] = label
    _mark_ok(label, gemini=False, provider=p)
    return parsed


def _ask(model_name: str, order) -> dict | None:
    """Один запрос к YandexGPT по REST. dict - оценка, None - не вышло."""
    import requests
    body = {
        "modelUri": f"gpt://{FOLDER_ID}/{model_name}/latest",
        "completionOptions": {
            "stream": False,
            "temperature": 0.2,
            "maxTokens": YANDEX_MAX_TOKENS,
        },
        "messages": [
            {"role": "system", "text": SYSTEM_PROMPT},
            {"role": "user", "text": _user_prompt(order)},
        ],
    }
    # Ошибки Yandex только логируются (как при SDK): причина в оповещении
    # «AI-оценка заказов» остаётся от Gemini. Сетевой сбой и 5xx - один повтор.
    resp = None
    for attempt in (1, 2):
        try:
            resp = requests.post(YANDEX_URL, headers=_yandex_headers(), json=body,
                                 timeout=REQUEST_TIMEOUT_SEC)
        except Exception as e:
            logging.warning(f"[AI] YandexGPT: сбой запроса (попытка {attempt}): {type(e).__name__}")
            resp = None
        if resp is not None and resp.status_code < 500:
            break
        if attempt == 1:
            time.sleep(2)
    if resp is None:
        return None

    code = resp.status_code
    if code != 200:
        if code == 429:
            logging.warning("[AI] YandexGPT: лимит запросов (HTTP 429)")
        elif code in (401, 403):
            logging.error(f"[AI] YandexGPT: ключ или права отклонены (HTTP {code})")
        else:
            logging.warning(f"[AI] YandexGPT: HTTP {code}: {resp.text[:150]!r}")
        return None

    try:
        text = resp.json()["result"]["alternatives"][0]["message"]["text"]
    except Exception as e:
        logging.warning(f"[AI] YandexGPT: не разобран ответ: {type(e).__name__}")
        return None
    parsed = parse_ai_json(text)
    if parsed is None:
        logging.warning(f"[AI] ответ не JSON: {text[:150]!r}")
        if REFUSAL_RE.search(text or ""):
            # фильтр модели отказался: заказ не наш, не переоцениваем вечно
            return {"fit": 0, "difficulty": "", "hours": 0.0, "category": "other",
                    "components": [], "unknowns": [], "clarity": "",
                    "client_budget_ok": None, "price_rub": "",
                    "summary": "отказ модели", "risks": "", "questions": []}
    return parsed


def _probe_order():
    from freelance.alot_client import FreelanceOrder
    return FreelanceOrder(0, "youdoru", "Создать Telegram-бота для записи клиентов",
                          "Бот для салона: выбор мастера и времени, напоминание за день.",
                          "до 10 000 ₽", 10000.0, [], datetime.now(timezone.utc), "")


def probe() -> list[dict]:
    """Для /aitest: по одному тестовому запросу к первой доступной модели каждого
    провайдера цепочки, прямо с сервера. Паузы после 429 обходятся (иначе не увидим,
    что отвечает провайдер сейчас), дневной потолок - нет (он защищает бесплатную квоту).
    Возвращает [{provider, configured, ok, model, seconds, fit, error}]."""
    order = _probe_order()
    active = set(_chain_active())
    results = []
    for name in CHAIN:
        r = {"provider": name, "configured": name in active, "ok": False,
             "model": "", "seconds": 0.0, "fit": None, "error": ""}
        results.append(r)
        if not r["configured"]:
            r["error"] = "нет ключа" if name != "yandex" else "выключен (платный)"
            continue
        if name == "yandex":
            r["error"] = "платный, не проверяем"
            continue
        if name == "gemini":
            models = [m for m in GEMINI_MODELS if m not in _disabled]
        else:
            p = PROVIDERS[name]
            models = [m for m in _provider_models(p) if m not in p.disabled]
        if not models:
            r["error"] = "нет доступных моделей"
            continue
        model = models[0]
        r["model"] = f"{name}:{model}" if name != "gemini" else model
        if name != "gemini":
            with _lock:
                capped = p.day == _today() and p.count >= p.max_per_day
            if capped:
                r["error"] = f"дневной потолок {p.max_per_day} исчерпан (сброс в 03:00 МСК)"
                continue
        started = time.monotonic()
        try:
            if name == "gemini":
                outcome = _ask_gemini_model(model, order)
            else:
                with _lock:
                    p.paused.pop(model, None)
                    p.last_error = ""   # в отчёт - только ошибка этой проверки, не старая
                outcome = _ask_provider_model(p, model, order)
                if outcome == "stop" and not (p.last_error or p.key_error or p.paid_reason):
                    r["error"] = "провайдер на блокировке или очередь запросов занята"
        except Exception as e:
            logging.exception(f"[AI] /aitest: {name} упал")
            outcome, r["error"] = None, type(e).__name__
        r["seconds"] = time.monotonic() - started
        if isinstance(outcome, dict):
            r["ok"], r["fit"] = True, outcome.get("fit")
            continue
        if not r["error"]:
            with _lock:
                if name == "gemini":
                    r["error"] = _region_error if outcome == "config" and _region_error else _gemini_last_error
                else:
                    r["error"] = p.key_error or p.paid_reason or p.last_error
        r["error"] = r["error"] or "нет ответа"
    return results


if __name__ == "__main__":
    # Ручной прогон: типовые заказы
    from datetime import datetime, timezone

    from freelance.alot_client import FreelanceOrder

    logging.basicConfig(level=logging.INFO)
    print("AI включён:", is_enabled(), "модели:", GEMINI_MODELS)

    def mk(i, title, body, price):
        digits = re.sub(r"\D", "", price)  # бюджет для расчёта цены ("до 15 000 ₽" -> 15000)
        return FreelanceOrder(i, "youdoru", title, body, price, float(digits or 0), [],
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
        mk(6, "Создам вашего персонального телеграм бота за 10 минут",
           "Быстро и недорого создам бота под ваши задачи.", "1 499 ₽"),
        mk(7, "Восстановить сайт после заражения вирусами",
           "Сайт на Joomla заражён, редиректы на левые сайты. Нужно вылечить.", "по договорённости"),
        mk(5, "Спарсить гостиницы и хостелы Москвы",
           "Собрать название, адрес, телефон, сайт с Яндекс.Карт в Excel.", "2 500 ₽"),
    ]
    for o in samples:
        res = score_order(o)
        print(f"\n{o.title}\n  -> {json.dumps(res, ensure_ascii=False)}")
    print("\nstatus:", json.dumps(status(), ensure_ascii=False))

    # Проверка устойчивого разбора обёрнутого ответа (новый формат)
    wrapped = ('```json\n{"fit": 8, "difficulty": "легко", "category": "tg_bot", '
               '"components": [{"name": "меню", "hours": 2}, {"name": "таблица", "hours": 1.5}], '
               '"unknowns": ["нет доступа к CRM"], "clarity": "частично", '
               '"client_budget_ok": true, "summary": "x", "risks": "y", '
               '"questions": ["есть ли таблица?"]}\n```')
    print("\nparse ```json:", parse_ai_json(wrapped))
    print("parse старый формат:", parse_ai_json(
        '{"fit": 7, "hours": 5, "price_rub": "8-12 тыс."}'))
    print("parse NaN hours:", parse_ai_json('{"fit": 7, "hours": NaN}'))
