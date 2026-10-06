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
- **AI-оценка** (Gemini, бесплатный тариф):
  - `fit` (0–10) — насколько подходит ваш профиль (от skills, опыта в prompt)
  - `сложность` (0–10) — техническая сложность реализации
  - `часы` — примерное время на реализацию
  - `рекомендуемая цена` — калькуляция на основе fit + сложности + часов
- **Фильтр по AI**: отправляются только заказы с `fit ≥ 5` (настраивается в `config.py`)
- **Свежесть**: заказы старше 24 часов не берутся
- **Дедупликация**: одинаковые заказы за 3 дня отправляются один раз

Без `GEMINI_API_KEY` заказы приходят с пометкой «без AI-оценки» (префильтр работает). Если ключ задан, но модель не ответила (сбой, таймаут, лимит запросов), заказ не отправляется, а оценивается в следующей проверке. Модель по умолчанию — `gemini-3.5-flash-lite` (меняется через `GEMINI_MODEL`); запросы идут не чаще одного в 4,5 с, при лимите (429) делается пауза 2 минуты. Платный YandexGPT выключен и включается только вручную: `YANDEX_ENABLED=1` вместе с `YANDEX_API_KEY` и `YANDEX_FOLDER_ID` (запасной путь, `YANDEX_MODEL`, `YANDEX_PRESCREEN_MODEL`).

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
# Опционально для AI-оценки фриланс-заказов:
GEMINI_API_KEY=your_gemini_api_key  # aistudio.google.com/apikey
```

- `BOT_TOKEN` — токен от [BotFather](https://t.me/botfather)
- `MY_CHAT_ID` — ваш ID в Telegram (используйте [@userinfobot](https://t.me/userinfobot))
- `GEMINI_API_KEY` — ключ Gemini API (бесплатный); без него заказы приходят без оценки

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
| `FREELANCE_MAX_AGE_HOURS` | 24 | Заказы старше не берутся |
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
