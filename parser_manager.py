"""Менеджер парсеров: запускает все источники, возвращает только новые вакансии."""
import logging

from database import is_sent, mark_sent
from parsers.base import Vacancy
from parsers.habr_parser import HabrParser


def get_all_parsers() -> list:
    """Возвращает список активных парсеров.

    Чтобы добавить новый источник — просто допишите его сюда.
    """
    return [
        HabrParser(max_pages=1),
        # DreamJobParser(max_pages=1),  # ← добавим позже
    ]


def fetch_new_vacancies() -> list[Vacancy]:
    """Запускает все парсеры и возвращает только НОВЫЕ вакансии.

    «Новая» = её нет в базе данных (не отправляли ранее).
    """
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
                continue  # уже отправляли

            new_vacancies.append(v)
            source_new += 1

        logging.info(
            f"[MANAGER] {parser.source_name}: собрано {len(vacancies)}, "
            f"новых {source_new}"
        )

    return new_vacancies


def mark_vacancies_sent(vacancies: list[Vacancy]) -> None:
    """Помечает вакансии как отправленные в БД."""
    for v in vacancies:
        mark_sent(
            source=v.source,
            vacancy_id=v.vacancy_id,
            title=v.title,
            company=v.company,
            url=v.url,
        )
    logging.info(f"[MANAGER] Помечено как отправлено: {len(vacancies)}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    from database import init_db

    init_db()

    new = fetch_new_vacancies()
    print(f"\nНовых вакансий: {len(new)}\n")
    for v in new[:5]:
        print(v.format_message())
        print("-" * 60)

    # Проверим дедупликацию
    mark_vacancies_sent(new)
    new_again = fetch_new_vacancies()
    print(f"\nПовторный запуск — новых: {len(new_again)} (должно быть 0)")