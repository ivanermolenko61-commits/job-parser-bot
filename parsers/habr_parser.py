"""Парсер вакансий с Хабр Карьеры (career.habr.com).

Использует requests + BeautifulSoup. Без API — парсим HTML.
"""
import logging
import re
import time

import requests
from bs4 import BeautifulSoup

from config import REMOTE_ONLY, REQUEST_DELAY
from parsers.base import BaseParser, Vacancy


class HabrParser(BaseParser):
    """Парсер вакансий с Хабр Карьеры."""

    source_name = "habr"

    BASE_URL = "https://career.habr.com/vacancies"

    # Стоп-слова: если в заголовке есть — исключаем вакансию
    EXCLUDE_WORDS = [
        "go ", "golang", "java ", "1с", "1c ", "javascript",
        "php", "c++", "c#", "ruby", "react", "vue", "angular",
        "qa", "тестирован", "аналитик", "дизайнер", "менеджер",
        "devops", "sre", "frontend", "фронтенд", "мобильн",
        "android", "ios", "unity", "kotlin", "swift",
    ]

    # Обязательные слова: хотя бы одно должно быть в заголовке
    INCLUDE_WORDS = [
        "python", "backend", "бэкенд", "back-end", "django", "fastapi",
    ]

    # Разрешённые уровни (по meta-блоку). Пустая строка = уровень не указан
    ALLOWED_LEVELS = {"", "junior", "intern", "стажёр", "стажер", "trainee"}

    # Регулярки для разбора meta и компании
    LEVEL_PATTERN = re.compile(
        r"(Junior|Middle|Senior|Lead|Intern|Стажёр|Стажер|Trainee)",
        re.IGNORECASE,
    )
    COMPANY_RATING_PATTERN = re.compile(r"[\d.,]+\s*$")
    CITY_PATTERN = re.compile(r"[А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?")

    def __init__(self, max_pages: int = 1):
        self.max_pages = max_pages
        self.headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        }

    def _fetch_page(self, page: int) -> str | None:
        params = {
            "q": "Python Junior",
            "type": "all",
            "remote": "true" if REMOTE_ONLY else "false",
            "page": page,
        }

        try:
            response = requests.get(
                self.BASE_URL,
                params=params,
                headers=self.headers,
                timeout=15,
            )
            response.raise_for_status()
            return response.text
        except requests.RequestException as e:
            logging.warning(f"[HABR] Ошибка запроса (page={page}): {e}")
            return None

    def _is_relevant_title(self, title: str) -> bool:
        """Проверяет, что вакансия — Python/backend, а не Go/Java/1С."""
        t = title.lower()
        has_include = any(w in t for w in self.INCLUDE_WORDS)
        has_exclude = any(w in t for w in self.EXCLUDE_WORDS)
        return has_include and not has_exclude

    def _clean_company(self, company: str) -> str:
        """Убирает прилипший рейтинг компании (число в конце)."""
        return self.COMPANY_RATING_PATTERN.sub("", company).strip()

    def _clean_salary(self, salary: str) -> str:
        """Обрезает рекламную приписку про 'Похожие специалисты'."""
        if "Похожие специалисты" in salary:
            salary = salary.split("Похожие специалисты")[0]
        if salary.startswith("Зарплата не указана") or salary.startswith("Зарплата не указан"):
            return "не указана"
        return salary.strip()

    def _parse_meta(self, card) -> tuple[str, str]:
        """Разбирает meta: возвращает (location, level).

        Внутри vacancy-card__meta текст может идти сплошняком:
        'JuniorМожно удалённоМоскваСанкт-Петербург'
        """
        meta_tag = card.select_one(".vacancy-card__meta")
        if not meta_tag:
            return "", ""

        text = meta_tag.get_text(" ", strip=True)

        # Уровень
        level = ""
        m = self.LEVEL_PATTERN.search(text)
        if m:
            level = m.group(1)
            text = text.replace(m.group(0), " ", 1)

        # Убираем маркер удалёнки
        text = text.replace("Можно удалённо", " ").replace("Удалённо", " ")

        # Ищем города
        cities = self.CITY_PATTERN.findall(text)

        # Собираем уникальные, сохраняя порядок
        seen = set()
        unique_cities = []
        for c in cities:
            if c.lower() not in seen:
                seen.add(c.lower())
                unique_cities.append(c)

        location = ", ".join(unique_cities)
        return location, level

    def _parse_html(self, html: str) -> list[Vacancy]:
        soup = BeautifulSoup(html, "html.parser")
        vacancies = []

        cards = soup.select(".vacancy-card")

        for card in cards:
            try:
                title_link = card.select_one(".vacancy-card__title-link")
                if not title_link:
                    continue

                title = title_link.get_text(strip=True)

                # Фильтр по ключевым словам
                if not self._is_relevant_title(title):
                    continue

                # Фильтр по уровню
                location, level = self._parse_meta(card)
                if level.lower() not in self.ALLOWED_LEVELS:
                    logging.debug(f"[HABR] Пропуск (уровень {level}): {title}")
                    continue

                url = "https://career.habr.com" + title_link.get("href", "")

                # Компания
                company_tag = card.select_one(".vacancy-card__company")
                company = (
                    self._clean_company(company_tag.get_text(strip=True))
                    if company_tag else "Не указана"
                )

                # Зарплата
                salary_tag = card.select_one(".vacancy-card__salary")
                salary = (
                    self._clean_salary(salary_tag.get_text(strip=True))
                    if salary_tag else "не указана"
                )

                # Локация с уровнем
                if not location and not level:
                    location_display = "Удалённо"
                else:
                    location_display = f"{level} · {location}" if level and location else (level or location or "Удалённо")

                vacancy_id = url.rstrip("/").split("/")[-1]

                vacancies.append(Vacancy(
                    source=self.source_name,
                    vacancy_id=vacancy_id,
                    title=title,
                    company=company,
                    url=url,
                    location=location_display,
                    salary=salary,
                ))
            except Exception as e:
                logging.debug(f"[HABR] Ошибка парсинга карточки: {e}")
                continue

        return vacancies

    def fetch(self) -> list[Vacancy]:
        all_vacancies = []

        for page in range(1, self.max_pages + 1):
            html = self._fetch_page(page)
            if not html:
                break

            vacancies = self._parse_html(html)
            if not vacancies:
                break

            all_vacancies.extend(vacancies)

            if page < self.max_pages:
                time.sleep(REQUEST_DELAY)

        logging.info(f"[HABR] Получено {len(all_vacancies)} вакансий")
        return all_vacancies


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    parser = HabrParser(max_pages=1)
    vacancies = parser.fetch()

    print(f"\nВсего собрано: {len(vacancies)}\n")
    for v in vacancies[:10]:
        print(v.format_message())
        print("-" * 60)