"""Менеджер парсеров: запускает все источники, возвращает только новые вакансии."""
import gc
import logging

from database import is_duplicate, is_sent, mark_sent
from parsers.base import Vacancy
from parsers.dreamjob_parser import DreamJobParser
from parsers.geekjob_parser import GeekJobParser
from parsers.habr_parser import HabrParser
from parsers.hh_parser import HHParser


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

    Дедупликация двойная:
      1. По (source, vacancy_id) — точный дубль с того же сайта.
      2. По (title, company) — один и тот же работодатель разместил
         вакансию на нескольких площадках. Берём только первую.

    После каждого парсера вызывается gc.collect() — освобождает память,
    которую держал Playwright (Chromium — тяжёлый процесс ~250 МБ).
    """
    parsers = get_all_parsers()
    new_vacancies = []
    seen_pairs: set[tuple[str, str]] = set()

    for parser in parsers:
        logging.info(f"[MANAGER] Запуск парсера: {parser.source_name}")
        try:
            vacancies = parser.fetch()
        except Exception as e:
            logging.exception(f"[MANAGER] Ошибка в парсере {parser.source_name}: {e}")
            vacancies = []

        # Освобождаем память после парсера (особенно важно для Playwright)
        collected = gc.collect()
        logging.info(
            f"[MANAGER] gc.collect() после {parser.source_name}: "
            f"освобождено объектов — {collected}"
        )

        source_new = 0
        for v in vacancies:
            # Дубль по ID с того же источника
            if is_sent(v.source, v.vacancy_id):
                continue

            # Дубль по названию+компании (кросс-источник)
            pair = (v.title.strip().lower(), v.company.strip().lower())
            if pair in seen_pairs:
                logging.debug(f"[MANAGER] Пропуск дубля: {v.title} / {v.company}")
                continue
            if is_duplicate(v.title, v.company):
                continue

            seen_pairs.add(pair)
            new_vacancies.append(v)
            source_new += 1

        logging.info(
            f"[MANAGER] {parser.source_name}: собрано {len(vacancies)}, "
            f"новых {source_new}"
        )

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