"""Менеджер парсеров: запускает все источники, возвращает только новые вакансии."""
import logging
import os
import signal
import threading

import memwatch
from config import MAX_VACANCY_AGE_DAYS
from database import is_duplicate, is_fresh, is_sent, mark_sent
from health import monitor
from parsers.base import Vacancy
from parsers.dreamjob_parser import DreamJobParser
from parsers.filters import dedup_key
from parsers.geekjob_parser import GeekJobParser
from parsers.habr_parser import HabrParser
from parsers.hh_parser import HHParser


# Сколько секунд парсеру разрешено работать целиком. Без этого один зависший
# сайт/Chromium навсегда занимает _check_lock, и бот молча перестаёт слать вакансии.
# hh — 900: у него есть запасной проход с JS (худший случай ~5×140 с).
# Сумма 900+600+240+240 = 1980 с < 2400 с предохранителя в bot.py.
PARSER_TIMEOUT_SEC = {"hh": 900, "geekjob": 600}
DEFAULT_PARSER_TIMEOUT_SEC = 240


_fetch_lock = threading.Lock()  # защита от наложения двух прогонов (два Chromium > 1 ГБ)


def _kill_browser_procs() -> None:
    """Убивает зависший Chromium и node-драйвер Playwright (run-driver),
    чтобы поток Playwright мог завершиться.

    Вызывается, только когда поток парсера завис. В образе python:3.11-slim нет
    pkill, поэтому обходим /proc сами (Linux). Парсеры идут по очереди, так что
    чужих браузерных процессов в контейнере нет.
    """
    me = os.getpid()
    try:
        pids = [int(n) for n in os.listdir("/proc") if n.isdigit()]
    except OSError:
        return  # не Linux (локально на Windows) — нечего убивать
    killed = 0
    for pid in pids:
        if pid == me:
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cmd = f.read()
            if b"chrom" in cmd or b"run-driver" in cmd:
                os.kill(pid, signal.SIGKILL)
                killed += 1
        except (OSError, ProcessLookupError):
            continue
    logging.warning(f"[MANAGER] Завершено браузерных процессов (chromium/node-драйвер): {killed}")


def _fetch_with_timeout(parser) -> list[Vacancy]:
    """parser.fetch() с общим таймаутом. При превышении бросает TimeoutError."""
    timeout = PARSER_TIMEOUT_SEC.get(parser.source_name, DEFAULT_PARSER_TIMEOUT_SEC)
    box: dict = {}

    def run():
        try:
            box["result"] = parser.fetch()
        except Exception as e:  # передаём ошибку в основной поток
            box["error"] = e

    # daemon=True: зависший поток не мешает процессу завершиться
    t = threading.Thread(target=run, name=f"parser-{parser.source_name}", daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        _kill_browser_procs()
        t.join(5)
        if t.is_alive():
            logging.error(
                f"[MANAGER] Поток {parser.source_name} не завершился даже после kill (утечка)"
            )
        raise TimeoutError(f"парсер завис, прервано через {timeout} с")
    if "error" in box:
        raise box["error"]
    return box["result"]


MEM_RECOVER_GAP_MB = 100  # «снова в норме» — только заметно ниже порога, без дребезга


def _watch_memory(used: int | None) -> None:
    """Сторож памяти: одна запись за прогон, только оповещение.

    Поведение бота не меняется. Пока оповещение висит, «в норме» засчитывается
    лишь ниже MEM_WARN_MB - MEM_RECOVER_GAP_MB, чтобы у порога не было пачек
    «близка к лимиту» / «снова в норме».
    """
    if used is None:
        return
    alerted = monitor.streaks().get("memory", 0) > 0
    limit = memwatch.MEM_WARN_MB - (MEM_RECOVER_GAP_MB if alerted else 0)
    monitor.record(
        "memory", 0 if used > limit else 1, error=None,
        label="Память контейнера", immediate=True,
        reason=f"Занято {used} МБ из {memwatch.MEM_LIMIT_MB} (порог {memwatch.MEM_WARN_MB})",
        bad_text="близка к лимиту",
        recovered_text="снова в норме",
    )


def get_all_parsers() -> list:
    """Список активных парсеров."""
    return [
        HabrParser(),
        DreamJobParser(),
        GeekJobParser(),
        HHParser(),
    ]


def fetch_new_vacancies() -> list[Vacancy]:
    """Запускает все парсеры, возвращает только НОВЫЕ вакансии.

    Если предыдущий прогон ещё идёт (например, поток пережил таймаут в bot.py),
    второй не запускается: возвращает [] — иначе два Chromium выбьют лимит RAM.
    """
    if not _fetch_lock.acquire(blocking=False):
        logging.warning("[MANAGER] Предыдущая проверка ещё идёт в фоне, пропуск")
        return []
    try:
        return _fetch_new_vacancies_locked()
    finally:
        _fetch_lock.release()


def _fetch_new_vacancies_locked() -> list[Vacancy]:
    """Тело fetch_new_vacancies (вызывается под _fetch_lock).

    Фильтры (по порядку):
      1. Свежесть — вакансия не старше MAX_VACANCY_AGE_DAYS.
      2. Дубль по (source, vacancy_id) — точный дубль с того же сайта.
      3. Дубль по (title, company) — один и тот же работодатель разместил
         вакансию на нескольких площадках. Берём только первую.

    После каждого парсера вызывается memwatch.trim() (gc.collect() + malloc_trim) —
    освобождает память, которую держал Playwright (Chromium — тяжёлый процесс ~250 МБ).
    """
    parsers = get_all_parsers()
    new_vacancies = []
    seen_keys: set[str] = set()
    skipped_stale = 0
    mem_max: int | None = None  # максимум памяти после парсеров за прогон

    for parser in parsers:
        logging.info(f"[MANAGER] Запуск парсера: {parser.source_name}")
        error = None
        try:
            vacancies = _fetch_with_timeout(parser)
        except Exception as e:
            logging.exception(f"[MANAGER] Ошибка в парсере {parser.source_name}: {e}")
            vacancies = []
            error = e
        monitor.record(parser.source_name, parser.cards_seen, error)

        # Освобождаем память после парсера (особенно важно для Playwright)
        collected = memwatch.trim()
        logging.info(
            f"[MANAGER] gc.collect() после {parser.source_name}: "
            f"освобождено объектов — {collected}"
        )
        memwatch.log_mem(parser.source_name)
        used = memwatch.mem_used_mb()
        if used is not None:
            mem_max = used if mem_max is None else max(mem_max, used)

        source_new = 0
        source_stale = 0
        for v in vacancies:
            # 1. Фильтр свежести: не отправляем старое
            if not is_fresh(v.published_at, MAX_VACANCY_AGE_DAYS):
                source_stale += 1
                logging.debug(
                    f"[MANAGER] Пропуск устаревшей ({v.published_at}): {v.title}"
                )
                continue

            # 2. Дубль по ID с того же источника
            if is_sent(v.source, v.vacancy_id):
                continue

            # 3. Дубль по названию+компании (кросс-источник)
            key = dedup_key(v.title, v.company)
            if key and key in seen_keys:
                logging.debug(f"[MANAGER] Пропуск дубля: {v.title} / {v.company}")
                continue
            if is_duplicate(v.title, v.company):
                continue

            if key:
                seen_keys.add(key)
            new_vacancies.append(v)
            source_new += 1

        skipped_stale += source_stale
        logging.info(
            f"[MANAGER] {parser.source_name}: собрано {len(vacancies)}, "
            f"новых {source_new}, отсеяно устаревших {source_stale}"
        )

    if skipped_stale:
        logging.info(
            f"[MANAGER] Всего отсеяно по фильтру свежести (>"
            f"{MAX_VACANCY_AGE_DAYS} дн): {skipped_stale}"
        )

    _watch_memory(mem_max)

    new_vacancies.sort(key=lambda v: (not v.is_remote, v.title))
    return new_vacancies


def mark_vacancies_sent(vacancies: list[Vacancy]) -> None:
    for v in vacancies:
        mark_sent(
            source=v.source,
            vacancy_id=v.vacancy_id,
            title=v.title,
            company=v.company,
            url=v.url,
            published_at=v.published_at,
        )
    logging.info(f"[MANAGER] Помечено как отправлено: {len(vacancies)}")