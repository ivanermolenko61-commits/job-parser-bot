# Job Parser Bot

Telegram-бот для мониторинга вакансий IT Junior/стажёров и фриланс-заказов.

Две основные функции:

- **📋 Вакансии** — автоматический поиск Junior-вакансий по разработке из нескольких источников (Habr, DreamJob, hh.ru, GeekJob), дедупликация, периодическая проверка каждые 15 минут.
- **💼 Фриланс** — скан фриланс-заказов с alot.pro (агрегатор фриланс-бирж), префильтр по категориям и ключевым словам, оценка релевантности через Gemini (бесплатно, fit 0–10), рекомендуемая цена и примерное время реализации.

## Источники вакансий

Бот парсит следующие площадки:

| Источник | Парсер | Метод |
|---|---|---|
| career.habr.com | `habr_parser.py` | requests + BeautifulSoup |
| dreamjob.ru | `dreamjob_parser.py` | requests + BeautifulSoup |
| hh.ru | `hh_parser.py` | Playwright + Chromium |
| geekjob.ru | `geekjob_parser.py` | Playwright + Chromium |

Поиск ведётся по ключевым словам из `config.py` (Python, junior, стажёр, разработчик и др.). Вакансии старше 5 дней не отправляются. Удалённые вакансии показываются в приоритете.

## Фриланс-заказы

Заказы приходят с **alot.pro** (агрегатор фриланс-бирж) каждые 5 минут:

- **Префильтр** (перед AI): категория разработки, исключение вакансионных сайтов и явно не нашего (1C, Bitrix, дизайн, переводы и т.д.)
- **Свежесть**: присылаются только заказы не старше `FREELANCE_MAX_AGE_HOURS` (6 часов по умолчанию); alot.pro не отдаёт статус заказа, поэтому мёртвые/взятые заказы отсекаются свежестью.
- **AI-оценка** (Gemini, бесплатный тариф):
  - `fit` (0–10) — насколько подходит ваш профиль (от skills, опыта в prompt)
  - `сложность` — техническая сложность реализации (легко/средне/сложно)
  - `часы` — примерное время на реализацию с Claude Code
  - `рекомендуемая цена` — калькуляция на основе fit + сложности + часов
- **Цепочка AI**: порядок задаёт `AI_PROVIDERS` (по умолчанию `gemini,cloudflare,openrouter,github,groq,cerebras,mistral`; платный YandexGPT в неё не входит). Провайдер без ключа пропускается. Если провайдер в лимите (429), дневная квота кончилась, ключ отклонён или ответ не JSON — заказ сразу уходит следующему, а провайдер/модель на паузе пропускаются до её окончания. Подробности и ключи — в разделе «AI-провайдеры» ниже.
- **После оценки**: отправляются только заказы с `fit ≥ FREELANCE_MIN_FIT` (по умолчанию 5, настраивается в `config.py`)
- **Кнопки под заказом**: 🔗 Открыть, ✅ Взял (сообщение остаётся, помечается «В работе»), ❌ Не подходит (сообщение удаляется)
- **Автоудаление**: бот сам удаляет неотмеченные заказы из чата через `FREELANCE_MESSAGE_TTL_HOURS` (24 часа по умолчанию).
- **Дедупликация**: одинаковые заказы за 3 дня отправляются один раз

**Если AI не работает**:
- Без ключей (ни Gemini, ни GitHub/Cloudflare/OpenRouter/Groq/Cerebras/Mistral, ни включённого Yandex) заказы приходят с пометкой «без AI» (префильтр работает).
- Если ключ задан, но модель не ответила (сбой, таймаут, лимит), заказ ждёт и оценивается в следующих проверках, пока ему меньше `FREELANCE_MAX_AGE_HOURS`. Без оценки в чат он не уходит.
- Исчерпанная дневная квота модели (HTTP 429 с `retryDelay`) ставит её на паузу до сброса, запросы идут к следующей модели.
- Если не отвечает ни один провайдер 3 проверки подряд, приходит предупреждение в Telegram. Отклонённый ключ отдельного провайдера — отдельное предупреждение «ключ отклонён» (пока другие провайдеры работают, это не «AI не работает»).

## Команды

| Команда | Что делает |
|---|---|
| `/start` | Приветствие, первая проверка вакансий |
| `/check` | Проверить новые вакансии сейчас |
| `/status` | Статистика: сколько в БД, по источникам, последняя проверка |
| `/list` | Список всех вакансий в БД (с пагинацией) |
| `/stop` | Приостановить уведомления о вакансиях |
| `/reset` | Очистить БД (все вакансии удаляются) |

## Reply-кнопки

Внизу экрана доступны быстрые кнопки:

- **📋 Список** — список вакансий
- **📊 Статус** — статистика
- **🔍 Проверить сейчас** — мгновенная проверка
- **💼 Фриланс** — меню фриланс-заказов
- **⏸ Пауза / ▶️ Возобновить** — включение/выключение уведомлений

### Меню фриланса

При нажатии на **💼 Фриланс** появляется inline-клавиатура:

- **📬 Свежие** — показать последние заказы с AI-оценкой
- **⚙️ Проверить сейчас** — запустить цикл сканирования
- **⏸️ / ▶️ Toggle** — включить/выключить отправку заказов (отдельно от вакансий)
- **📊 Статистика** — сводка по заказам
- **📊 Точность цен** — сколько сделок записано и средняя ошибка рекомендуемой цены против фактической (по категориям)

#### Как считается цена

Модель (Gemini/YandexGPT) цену не называет: она разбирает заказ на компоненты с часами «с Claude Code», определяет категорию, ясность ТЗ и неизвестные. Цену считает код (`freelance/pricing.py`):

- `часы × ставка × риск` — риск 1.0 / 1.15 / 1.3 по ясности ТЗ + 0.1 за каждое неизвестное (максимум +0.3); ниже минимума категории не опускается;
- вилка ±15%, округление до 100 ₽ (до 10 000) или 500 ₽;
- если заказчик указал бюджет — цена не выше бюджета ×1.1, при сильном занижении бюджета заказ помечается «бюджет занижен»;
- если бюджета нет, а по категории накоплено ≥5 бюджетов за 90 дней — цена сдвигается к рыночной медиане (вес 0.3);
- после кнопки **✅ Взял** бот спрашивает, за сколько договорились (число или `/skip`). Когда по категории ≥5 сделок, ставка категории берётся как медиана «цена сделки / часы».

Старые записи без расчёта показывают прежнюю цену от модели.

## Настройка

### .env

Создайте файл `.env` на основе `.env.example`:

```ini
BOT_TOKEN=your_token_from_botfather
MY_CHAT_ID=your_chat_id_from_userinfobot
# Обязателен для AI-оценки фриланс-заказов:
GEMINI_API_KEY=your_gemini_api_key  # aistudio.google.com/apikey
# Опционально: цепочка бесплатных моделей Gemini (по умолчанию встроена)
# GEMINI_MODELS=gemini-3.5-flash-lite,gemini-3.1-flash-lite,...
# Опционально: YandexGPT ПЛАТНЫЙ, выключен по умолчанию (нужны YANDEX_ENABLED=1 и yandex в AI_PROVIDERS)
YANDEX_API_KEY=
YANDEX_FOLDER_ID=
```

- `BOT_TOKEN` — токен от [BotFather](https://t.me/botfather)
- `MY_CHAT_ID` — ваш ID в Telegram (используйте [@userinfobot](https://t.me/userinfobot))
- `GEMINI_API_KEY` — ключ Gemini API (бесплатный, [aistudio.google.com/apikey](https://aistudio.google.com/apikey)); обязателен для AI-оценки; без него заказы приходят с пометкой «без AI»
- `GEMINI_MODELS` — цепочка моделей (опционально; встроена по умолчанию): `gemini-3.5-flash-lite,gemini-3.1-flash-lite,gemini-3.1-flash-lite-preview,gemma-4-31b-it,gemma-4-26b-a4b-it`. При лимите одной модели (429) берётся следующая. Бесплатный дневной лимит: flash-lite — 500 запросов, gemma — 14 400 (у каждой модели свой)
- `YANDEX_API_KEY`, `YANDEX_FOLDER_ID` — YandexGPT-lite **платный** (бесплатен только стартовый грант), поэтому **по умолчанию выключен**: вызывается только при `YANDEX_ENABLED=1` И `yandex` в `AI_PROVIDERS`, не более `YANDEX_MAX_PER_DAY` (60) запросов в сутки. Без `YANDEX_ENABLED=1` не вызывается никогда, даже если ключи заданы.

### AI-провайдеры (бесплатные, взаимозаменяемые)

Все провайдеры используются **только на бесплатных тарифах и бесплатных моделях**; платные вызовы бот не делает. Ключи — в `.env`, достаточно любого одного. **Берите ключи только из аккаунтов без привязанной карты/биллинга** (у Google AI Studio — проект без Billing): иначе превышение бесплатного лимита может стать платным.

| Провайдер | Получить ключ | Бесплатный тариф | Модели по умолчанию (переменная) |
|---|---|---|---|
| Gemini | [aistudio.google.com/apikey](https://aistudio.google.com/apikey) | да, без карты | `GEMINI_MODELS` |
| GitHub Models | [github.com/settings/personal-access-tokens](https://github.com/settings/personal-access-tokens) (право **Models: read**) | да, нужен только аккаунт GitHub; 15 запросов/мин и 150/сутки на модель; paid usage не включать | `GITHUB_MODELS=openai/gpt-4.1-mini,openai/gpt-4o-mini` |
| Cloudflare Workers AI | [dash.cloudflare.com](https://dash.cloudflare.com) → AI → Workers AI → Use REST API (`CLOUDFLARE_ACCOUNT_ID` + `CLOUDFLARE_API_KEY`) | да, без карты; 10 000 нейронов/сутки (бот берёт не больше 40 заказов); на Workers Paid не переходить | `CLOUDFLARE_MODELS=@cf/openai/gpt-oss-120b,@cf/qwen/qwen3-30b-a3b-fp8` |
| Groq | [console.groq.com/keys](https://console.groq.com/keys) | сайт недоступен из РФ; без карты; ~30 запросов/мин, ~1000/сутки на крупных моделях (плюс лимиты по токенам) | `GROQ_MODELS=openai/gpt-oss-120b,llama-3.3-70b-versatile,llama-3.1-8b-instant` |
| Cerebras | [cloud.cerebras.ai](https://cloud.cerebras.ai) | сейчас ключ только с картой; 5 запросов/мин, 1M токенов/сутки на модель | `CEREBRAS_MODELS=gpt-oss-120b,qwen-3.8-27b` |
| Mistral | [console.mistral.ai/api-keys](https://console.mistral.ai/api-keys) | сейчас ключ только на платном тарифе; был тариф **Experiment**; **данные запросов на нём могут использоваться для обучения** | `MISTRAL_MODELS=mistral-small-latest,ministral-8b-latest` |
| OpenRouter | [openrouter.ai/keys](https://openrouter.ai/keys) | только модели `:free` / `openrouter/free`; без пополнения баланса 50 запросов/сутки на все вместе | `OPENROUTER_MODELS=nvidia/nemotron-3-super-120b-a12b:free,openrouter/free` |
| YandexGPT | см. выше | **платный**, по умолчанию выключен (нужны `YANDEX_ENABLED=1` и `yandex` в `AI_PROVIDERS`) | `yandexgpt-lite` |

- Порядок: `AI_PROVIDERS=gemini,cloudflare,openrouter,github` (неизвестные имена пропускаются с предупреждением в логе; если после разбора цепочка пуста или в ней нет провайдеров с ключами — ERROR в лог и берётся цепочка по умолчанию, чтобы опечатка не отключила AI). Модели внутри провайдера берутся по порядку списка. На один заказ отводится не больше `AI_ORDER_DEADLINE_SEC` (120 с): после этого новые модели и провайдеры не пробуются.
- У каждого провайдера свой минимальный интервал между запросами (`CLOUDFLARE_MIN_INTERVAL_SEC` 3, `OPENROUTER_MIN_INTERVAL_SEC` 3.5, `GITHUB_MIN_INTERVAL_SEC` 4.5, `GROQ_MIN_INTERVAL_SEC` 2.5, `CEREBRAS_MIN_INTERVAL_SEC` 12.5, `MISTRAL_MIN_INTERVAL_SEC` 1.2) и свой дневной потолок запросов (`CLOUDFLARE_MAX_PER_DAY` 40, `OPENROUTER_MAX_PER_DAY` 45, `GITHUB_MAX_PER_DAY` 290, `GROQ_MAX_PER_DAY` 900, `CEREBRAS_MAX_PER_DAY` 300, `MISTRAL_MAX_PER_DAY` 1000); дневной счётчик идёт по UTC. Если очередь к провайдеру длиннее `AI_PROVIDER_MAX_WAIT_SEC` (20 с), заказ уходит следующему.
- 429: пауза модели по `Retry-After` / `x-ratelimit-reset-*`; при дневной квоте — от 1 до 6 часов. 401/403: провайдер выключается на 5 минут (остальные не затрагиваются). 404: модель выключается до рестарта. **402 (требуется оплата) или ответ платной модели OpenRouter (`model` без `:free` и ненулевой `usage.cost`): провайдер выключается до рестарта, в логе ERROR и сразу приходит оповещение в Telegram.** 403 с признаками модерации — отказ по заказу, ключ не блокируется.
- OpenRouter: из `OPENROUTER_MODELS` отбрасываются модели без `:free` (кроме `openrouter/free`); раз в 6 часов список сверяется с публичным `/models`, модели с ненулевой ценой исключаются. Платные функции (web search и т.п.) в запросах не используются.
- Статус AI (кнопка статуса фриланса) показывает все провайдеры: модели, паузы, счётчик за сутки.
- Лимиты и список моделей бесплатных тарифов меняются: если модель пропала (404), она выключается до рестарта — обновите `*_MODELS`.

### config.py

Основные параметры:

| Параметр | Значение | Назначение |
|---|---|---|
| `QUERIES` | список слов | Поисковые запросы для лёгких парсеров (Habr, DreamJob) |
| `PLAYWRIGHT_QUERIES` | список слов | Запросы для Playwright-парсеров (hh.ru, GeekJob) |
| `MAX_PAGES_PER_QUERY` | 1 | Страниц на каждый запрос (1 стр = ~25 карточек) |
| `CHECK_INTERVAL_MINUTES` | 15 | Интервал проверки вакансий |
| `MAX_VACANCY_AGE_DAYS` | 5 | Вакансии старше не отправляются |
| `FREELANCE_INTERVAL_MINUTES` | 5 | Интервал проверки фриланс-заказов |
| `FREELANCE_MIN_FIT` | 5 | Минимальная оценка ИИ (0–10) для отправки |
| `FREELANCE_MAX_AGE_HOURS` | 6 | Заказы старше не берутся |
| `FREELANCE_MESSAGE_TTL_HOURS` | 24 | Через сколько часов бот сам удаляет неразобранные сообщения |
| `FREELANCE_MAX_PER_CYCLE` | 15 | Макс. отдельных сообщений за цикл; остальное — сводкой |
| `ALOT_SEED_ID` | 17328000 | Якорь поиска верхней границы id |
| `FREELANCE_RATE_PER_HOUR` | 1000 | Ставка ₽/ч для расчёта цены (env) |
| `FREELANCE_MIN_RATE_PER_HOUR` | 775 | Минимальная ставка ₽/ч: цена и низ вилки не ниже часы × ставка, даже при низком бюджете, рынке или сделках (env) |
| `FREELANCE_PRICE_SPREAD` | 0.15 | Ширина вилки цены ±15% (env) |
| `FREELANCE_MIN_PRICE` | по категориям | Минимум цены: tg_bot 3000, parser 1500, landing 3000, site_fix 1000, wp 1500, script 1500, integration 2500, sheets 1500, other 1500; env `FREELANCE_MIN_PRICE_<КАТЕГОРИЯ>` |

Параметры фриланса (фильтры, слова-признаки) находятся в `freelance/filters.py`.

## Установка и запуск

### Локально

1. **Клонируем репозиторий:**
   ```bash
   git clone https://github.com/ivanermolenko61-commits/job-parser-bot.git
   cd job-parser-bot
   ```

2. **Создаём виртуальное окружение:**
   ```bash
   python -m venv .venv
   source .venv/Scripts/activate  # Windows (PowerShell: .venv\Scripts\Activate.ps1)
   # или
   source .venv/bin/activate      # Linux/macOS
   ```

3. **Устанавливаем зависимости:**
   ```bash
   pip install -r requirements.txt
   ```

4. **Скачиваем Chromium для Playwright:**
   ```bash
   playwright install chromium
   ```

5. **Создаём `.env`:**
   ```bash
   cp .env.example .env
   # Отредактируйте .env, добавив ваши токены
   ```

6. **Запускаем бот:**
   ```bash
   python bot.py
   ```

### Docker

Проект разворачивается на платформах вроде [BotHost](https://bothost.ru/):

```bash
docker build -t job-parser-bot .
docker run -d \
  --env-file .env \
  --name job-parser-bot \
  job-parser-bot
```

**Примечание:** Dockerfile содержит `playwright install --with-deps chromium`, поэтому образ получается достаточно большим (~2 GB). В BotHost это нормально.

## Структура проекта

```
job-parser-bot/
├── bot.py                    # Главный модуль: Telegram Bot API, команды, рассылка
├── config.py                 # Настройки: ключевые слова, интервалы, параметры фильтров
├── database.py               # SQLite: дедупликация вакансий и заказов
├── parser_manager.py         # Координатор всех парсеров, таймауты
├── health.py                 # Мониторинг работоспособности
│
├── parsers/                  # Парсеры вакансий
│   ├── base.py              # Базовый класс Vacancy и BaseParser
│   ├── habr_parser.py       # Parses career.habr.com
│   ├── dreamjob_parser.py   # Parses dreamjob.ru
│   ├── hh_parser.py         # Parses hh.ru (Playwright)
│   ├── geekjob_parser.py    # Parses geekjob.ru (Playwright)
│   └── filters.py           # Фильтры при дедупликации вакансий
│
├── freelance/               # Фриланс-модуль (alot.pro)
│   ├── pipeline.py          # Цикл: скан, префильтр, дедуп, AI-оценка
│   ├── alot_client.py       # API alot.pro
│   ├── ai_scorer.py         # цепочка бесплатных AI: Gemini, GitHub Models, Cloudflare, OpenRouter, Groq, Cerebras, Mistral, YandexGPT
│   └── filters.py           # Префильтры по категориям, словам
│
├── requirements.txt         # Python-зависимости
├── Dockerfile               # Docker-образ
├── .env.example             # Шаблон переменных окружения
└── README.md                # Этот файл
```

## Как добавить свой парсер вакансий

1. **Создайте файл** `parsers/your_site_parser.py`
2. **Наследуйте от BaseParser**:
   ```python
   from parsers.base import BaseParser, Vacancy
   
   class YourSiteParser(BaseParser):
       source_name = "yoursite"
       
       def fetch(self) -> list[Vacancy]:
           # Реализуйте логику парсинга
           return [...]
   ```
3. **Добавьте в `parser_manager.py`**:
   - Импортируйте парсер
   - Добавьте его в функцию `get_all_parsers()`
4. **Тестируйте** и добавьте поддерживаемые запросы в `config.py` (или `PLAYWRIGHT_QUERIES` для Playwright)

## Как менять фильтры фриланса

Основные фильтры находятся в `freelance/filters.py`:

- **`DEV_CATEGORIES`** — категории alot.pro, относящиеся к разработке
- **`INCLUDE_WORDS`** — слова-признаки подходящего заказа (бот, telegram, python, парсер, API и др.)
- **`EXCLUDE_WORDS`** — явно не наше (1C, Bitrix, дизайн, копирайт и др.)

Функция `is_candidate()` применяет эти фильтры. Чтобы изменить логику, отредактируйте этот файл и перезагрузите бот.

**Параметры в `config.py`:**
- `FREELANCE_MIN_FIT` — минимальная оценка для отправки
- `FREELANCE_MAX_AGE_HOURS` — максимальный возраст заказа

## Важные замечания

### API alot.pro неофициальный

Интеграция с alot.pro построена на обратном инжиниринге публичного API (всё доступно в браузере). При изменении фронтенда сайта API может поломаться.

**Что происходит при поломке:**
- Модуль отправляет health-алерт (если настроен)
- Вакансии продолжают работать, заказы временно не приходят
- Логируется ошибка с типом исключения

Проверяйте логи:
```bash
grep FREELANCE bot.log  # или в Telegram через /status
```

### Параметры для экономии ресурсов

Если Chromium занимает слишком много памяти/CPU:

- Уменьшайте `MAX_PAGES_PER_QUERY`
- Сокращайте `PLAYWRIGHT_QUERIES`
- Увеличивайте `CHECK_INTERVAL_MINUTES`
- Отключайте GeekJob парсер в `parser_manager.py` временно

## Лицензия

MIT
