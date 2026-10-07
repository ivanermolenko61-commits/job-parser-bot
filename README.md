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

Заказы приходят с **alot.pro** (агрегатор фриланс-бирж) каждые 10 минут:

- **Префильтр** (перед AI): категория разработки, исключение вакансионных сайтов и явно не нашего (1C, Bitrix, дизайн, переводы и т.д.)
- **Свежесть**: присылаются только заказы не старше `FREELANCE_MAX_AGE_HOURS` (6 часов по умолчанию); alot.pro не отдаёт статус заказа, поэтому мёртвые/взятые заказы отсекаются свежестью.
- **AI-оценка** (Gemini, бесплатный тариф):
  - `fit` (0–10) — насколько подходит ваш профиль (от skills, опыта в prompt)
  - `сложность` — техническая сложность реализации (легко/средне/сложно)
  - `часы` — примерное время на реализацию с Claude Code
  - `рекомендуемая цена` — калькуляция на основе fit + сложности + часов
- **Цепочка AI**: начинает Gemini (список моделей `GEMINI_MODELS` по умолчанию), при лимите запросов (429) или сбое одной модели берётся следующая; если ключи `YANDEX_API_KEY` и `YANDEX_FOLDER_ID` заданы, запасной путь — `yandexgpt-lite` (не более `YANDEX_MAX_PER_DAY` = 60 запросов в сутки; отключить: `YANDEX_ENABLED=0`).
- **После оценки**: отправляются только заказы с `fit ≥ FREELANCE_MIN_FIT` (по умолчанию 5, настраивается в `config.py`)
- **Кнопки под заказом**: 🔗 Открыть, ✅ Взял (сообщение остаётся, помечается «В работе»), ❌ Не подходит (сообщение удаляется)
- **Автоудаление**: бот сам удаляет неотмеченные заказы из чата через `FREELANCE_MESSAGE_TTL_HOURS` (24 часа по умолчанию).
- **Дедупликация**: одинаковые заказы за 3 дня отправляются один раз

**Если AI не работает**:
- Без `GEMINI_API_KEY` заказы приходят с пометкой «без AI» (префильтр работает).
- Если ключ задан, но модель не ответила (сбой, таймаут, лимит), заказ откладывается и оценивается в следующей проверке.
- После 3 неудачных попыток оценки заказ уходит с пометкой «без AI», а не теряется.
- При неработающем AI (ошибка ключа) приходит предупреждение в Telegram.

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
# Опционально: запасной YandexGPT (включается только если заданы оба ключа)
YANDEX_API_KEY=
YANDEX_FOLDER_ID=
```

- `BOT_TOKEN` — токен от [BotFather](https://t.me/botfather)
- `MY_CHAT_ID` — ваш ID в Telegram (используйте [@userinfobot](https://t.me/userinfobot))
- `GEMINI_API_KEY` — ключ Gemini API (бесплатный, [aistudio.google.com/apikey](https://aistudio.google.com/apikey)); обязателен для AI-оценки; без него заказы приходят с пометкой «без AI»
- `GEMINI_MODELS` — цепочка моделей (опционально; встроена по умолчанию): `gemini-3.5-flash-lite,gemini-3.1-flash-lite,gemini-3.1-flash-lite-preview,gemma-4-31b-it`. При лимите одной модели (429) берётся следующая
- `YANDEX_API_KEY`, `YANDEX_FOLDER_ID` — запасной путь (опционально); YandexGPT-lite включается автоматически, если оба ключа заданы. Не более `YANDEX_MAX_PER_DAY` (60) запросов в сутки. Отключить: `YANDEX_ENABLED=0`

### config.py

Основные параметры:

| Параметр | Значение | Назначение |
|---|---|---|
| `QUERIES` | список слов | Поисковые запросы для лёгких парсеров (Habr, DreamJob) |
| `PLAYWRIGHT_QUERIES` | список слов | Запросы для Playwright-парсеров (hh.ru, GeekJob) |
| `MAX_PAGES_PER_QUERY` | 1 | Страниц на каждый запрос (1 стр = ~25 карточек) |
| `CHECK_INTERVAL_MINUTES` | 15 | Интервал проверки вакансий |
| `MAX_VACANCY_AGE_DAYS` | 5 | Вакансии старше не отправляются |
| `FREELANCE_INTERVAL_MINUTES` | 10 | Интервал проверки фриланс-заказов |
| `FREELANCE_MIN_FIT` | 5 | Минимальная оценка ИИ (0–10) для отправки |
| `FREELANCE_MAX_AGE_HOURS` | 6 | Заказы старше не берутся |
| `FREELANCE_MESSAGE_TTL_HOURS` | 24 | Через сколько часов бот сам удаляет неразобранные сообщения |
| `FREELANCE_MAX_PER_CYCLE` | 15 | Макс. отдельных сообщений за цикл; остальное — сводкой |
| `ALOT_SEED_ID` | 17328000 | Якорь поиска верхней границы id |

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
│   ├── ai_scorer.py         # Gemini (+ опционально YandexGPT)
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
