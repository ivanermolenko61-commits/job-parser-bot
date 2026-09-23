"""Менеджер парсеров: запускает все источники, возвращает только новые вакансии."""
import logging

from database import is_sent, mark_sent
from parsers.base import Vacancy
from parsers.habr_parser import HabrParser


def get_all_parsers() -> list:
    """Список активных парсеров."""
    return [
        HabrParser(),
        # DreamJobParser(),  # ← добавим позже
    ]


def fetch_new_vacancies() -> list[Vacancy]:
    """Запускает все парсеры, возвращает только НОВЫЕ вакансии."""
    parsers = get_all_parsers()
    new_vacancies = []

    for parser in parsers:
        logging.info(f"[MANAGER] Запуск парсера: {parser.source_name}")
        try:
            vacancies = parser.fetch()
        except Exception as e:
            logging.exception(f"[MANAGER] Ошибка в парсере {parser.source_name}: {e}")
            continue

        source_new = 0
        for v in vacancies:
            if is_sent(v.source, v.vacancy_id):
                continue
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