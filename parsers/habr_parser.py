"""Парсер вакансий с Хабр Карьеры (career.habr.com).

Ищет Junior/стажировки по разработке — любой язык, любое направление.
Прогоняет несколько запросов и объединяет результаты.
"""
import logging
import re
import time

import requests
from bs4 import BeautifulSoup

from config import MAX_PAGES_PER_QUERY, QUERIES, REQUEST_DELAY
from parsers.base import BaseParser, Vacancy


class HabrParser(BaseParser):
    """Парсер вакансий с Хабр Карьеры."""

    source_name = "habr"
    BASE_URL = "https://career.habr.com/vacancies"

    ALLOWED_LEVELS = {"", "junior", "intern", "стажёр", "стажер", "trainee"}

    SENIORITY_WORDS = [
        "senior", "lead ", "principal", "middle", "head of",
        "team lead", "tech lead", "director", "руководитель",
    ]

    EXCLUDE_WORDS = [
        # Менеджмент и продукт
        "менеджер", "manager", "product", "продукт",
        "проектами", "проектов", "project manager",
        # Аналитика, дизайн, research
        "аналитик", "analyst",
        "дизайнер", "designer",
        "исследователь", "researcher", "research", "ресерчер",
        # QA и тестирование
        "qa", "тестировщик", "тестирован", "testing", "test engineer",
        # Инфраструктура
        "devops", "sre", "administrator", "администратор",
        # Поддержка и сопровождение
        "техподдержка", "поддержки", "сопровождени", "support",
        # Маркетинг, HR, продажи
        "маркетолог", "marketing", "hr ", "рекрутер", "recruiter",
        "sales", "продаж",
        # Юридические, документарные, прочие не-tech
        "бухгалтер", "юрист", "юрисконсульт", "логист",
        "документами", "документооборот", "делопроизвод",
        # Data Science и тренерство
        "data scientist", "дата-сайентист", "data science",
        "тренер", "coach",
        # Специфика CVM/CMO и подобное
        "cvm", "cmo",
    ]

    LEVEL_PATTERN = re.compile(
        r"(Junior|Middle|Senior|Lead|Intern|Стажёр|Стажер|Trainee)",
        re.IGNORECASE,
    )
    COMPANY_RATING_PATTERN = re.compile(r"[\d.,]+\s*$")
    CITY_PATTERN = re.compile(r"[А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?")
    SALARY_NUMBER = re.compile(r"от\s+([\d\s]+)")

    MAX_JUNIOR_SALARY = 200_000

    def __init__(self):
        self.headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        }

    def _fetch_page(self, query: str, page: int) -> str | None:
        params = {
            "q": query,
            "type": "all",
            "sort": "date",
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
            logging.warning(f"[HABR] Ошибка запроса '{query}' (page={page}): {e}")
            return None

    def _is_relevant_category(self, title: str) -> bool:
        t = title.lower()

        if any(w in t for w in self.EXCLUDE_WORDS):
            return False

        if any(w in t for w in self.SENIORITY_WORDS):
            return False

        return True

    def _is_junior_salary(self, salary: str) -> bool:
        m = self.SALARY_NUMBER.search(salary)
        if not m:
            return True
        try:
            number = int(m.group(1).replace(" ", ""))
            return number < self.MAX_JUNIOR_SALARY
        except ValueError:
            return True

    def _clean_company(self, company: str) -> str:
        return self.COMPANY_RATING_PATTERN.sub("", company).strip()

    def _clean_salary(self, salary: str) -> str:
        if "Похожие специалисты" in salary:
            salary = salary.split("Похожие специалисты")[0]
        if salary.startswith("Зарплата не указан"):
            return "не указана"
        return salary.strip()

    def _parse_meta(self, card) -> tuple[str, str, bool]:
        meta_tag = card.select_one(".vacancy-card__meta")
        if not meta_tag:
            return "", "", False

        text = meta_tag.get_text(" ", strip=True)

        level = ""
        m = self.LEVEL_PATTERN.search(text)
        if m:
            level = m.group(1)
            text = text.replace(m.group(0), " ", 1)

        is_remote = "Можно удалённо" in text or "Удалённо" in text
        text = text.replace("Можно удалённо", " ").replace("Удалённо", " ")

        cities = self.CITY_PATTERN.findall(text)
        seen = set()
        unique_cities = []
        for c in cities:
            if c.lower() not in seen:
                seen.add(c.lower())
                unique_cities.append(c)

        location = ", ".join(unique_cities)
        return location, level, is_remote

    def _parse_html(self, html: str) -> list[Vacancy]:
        soup = BeautifulSoup(html, "html.parser")
        vacancies = []

        for card in soup.select(".vacancy-card"):
            try:
                title_link = card.select_one(".vacancy-card__title-link")
                if not title_link:
                    continue

                title = title_link.get_text(strip=True)

                if not self._is_relevant_category(title):
                    continue

                location, level, is_remote = self._parse_meta(card)

                if level.lower() not in self.ALLOWED_LEVELS:
                    continue

                salary_tag = card.select_one(".vacancy-card__salary")
                salary = self._clean_salary(salary_tag.get_text(strip=True)) if salary_tag else "не указана"

                if not self._is_junior_salary(salary):
                    logging.debug(f"[HABR] Пропуск (зарплата): {title} — {salary}")
                    continue

                url = "https://career.habr.com" + title_link.get("href", "")

                company_tag = card.select_one(".vacancy-card__company")
                company = self._clean_company(company_tag.get_text(strip=True)) if company_tag else "Не указана"

                parts = []
                if level:
                    parts.append(level)
                if location:
                    parts.append(location)
                if is_remote:
                    parts.append("удалённо")
                location_display = " · ".join(parts) if parts else "Удалённо"

                vacancy_id = url.rstrip("/").split("/")[-1]

                vacancies.append(Vacancy(
                    source=self.source_name,
                    vacancy_id=vacancy_id,
                    title=title,
                    company=company,
                    url=url,
                    location=location_display,
                    salary=salary,
                    is_remote=is_remote,
                ))
            except Exception as e:
                logging.debug(f"[HABR] Ошибка парсинга карточки: {e}")
                continue

        return vacancies

    def fetch(self) -> list[Vacancy]:
        seen_ids = set()
        all_vacancies = []

        for query in QUERIES:
            logging.info(f"[HABR] Запрос: '{query}'")
            for page in range(1, MAX_PAGES_PER_QUERY + 1):
                html = self._fetch_page(query, page)
                if not html:
                    break

                vacancies = self._parse_html(html)
                if not vacancies:
                    break

                for v in vacancies:
                    if v.vacancy_id in seen_ids:
                        continue
                    seen_ids.add(v.vacancy_id)
                    all_vacancies.append(v)

                if page < MAX_PAGES_PER_QUERY:
                    time.sleep(REQUEST_DELAY)

            time.sleep(REQUEST_DELAY)

        logging.info(f"[HABR] Итого собрано: {len(all_vacancies)}")
        return all_vacancies


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = HabrParser()
    vacancies = parser.fetch()

    print(f"\nВсего собрано: {len(vacancies)}")
    remote = sum(1 for v in vacancies if v.is_remote)
    print(f"Из них удалённых: {remote}\n")

    vacancies.sort(key=lambda v: (not v.is_remote, v.title))
    for v in vacancies[:25]:
        print(v.format_message())
        print("-" * 60)