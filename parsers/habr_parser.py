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
        "ведущий", "ведущая", "ведущее",
    ]

    EXCLUDE_WORDS = [
        "менеджер", "manager", "product", "продукт",
        "проектами", "проектов", "project manager",
        "аналитик", "analyst",
        "дизайнер", "designer",
        "исследователь", "researcher", "research", "ресерчер",
        "qa", "тестировщик", "тестирован", "testing", "test engineer",
        "devops", "sre", "administrator", "администратор",
        "техподдержка", "поддержки", "сопровождени", "support",
        "маркетолог", "marketing", "hr ", "рекрутер", "recruiter",
        "sales", "продаж",
        "бухгалтер", "юрист", "юрисконсульт", "логист",
        "документами", "документооборот", "делопроизвод",
        "data scientist", "дата-сайентист", "data science",
        "тренер", "coach",
        "cvm", "cmo",
        # Безопасность — не разработка
        "безопасност", "security", "appsec", "infosec",
    ]

    # IT-слова: хотя бы одно должно быть в заголовке
    INCLUDE_IT_WORDS = [
        "python", "java", " go ", "golang", "javascript", " typescript",
        "c++", "c#", "php", "ruby", "swift", "kotlin", "scala", "rust",
        "developer", "разработчик", "программист",
        "backend", "бэкенд", "frontend", "фронтенд", "fullstack", "фулстек",
        "ml engineer", "ml-инженер", "data engineer", "data-инженер",
        "dba", "1с", "1c", "разработк",
        "embedded", "bios", "bsp",
    ]

    LEVEL_PATTERN = re.compile(
        r"(Junior|Middle|Senior|Lead|Intern|Стажёр|Стажер|Trainee)",
        re.IGNORECASE,
    )
    COMPANY_RATING_PATTERN = re.compile(r"[\d.,]+\s*$")
    CITY_PATTERN = re.compile(r"[А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?")

    def __init__(self):
        self.headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        }

    def _fetch_page(self, query: str, page: int) -> str | None:
        params = {"q": query, "type": "all", "sort": "date", "page": page}
        try:
            response = requests.get(
                self.BASE_URL, params=params,
                headers=self.headers, timeout=15,
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
        if not any(w in t for w in self.INCLUDE_IT_WORDS):
            return False

        return True

    def _extract_company(self, company_tag) -> str:
        """Название компании без рейтинга.

        Рейтинг лежит в отдельном блоке .vacancy-card__company-rating —
        удаляем его из разметки. Раньше цифры отрезались регуляркой с конца
        строки, и заодно портились названия вроде «X5» → «X», «Т1» → «Т».
        Регулярка осталась как запасной вариант, если разметку поменяют.
        """
        rating = company_tag.select(".vacancy-card__company-rating")
        if rating:
            for r in rating:
                r.decompose()
            return company_tag.get_text(strip=True)
        return self.COMPANY_RATING_PATTERN.sub("", company_tag.get_text(strip=True)).strip()

    def _clean_salary(self, salary: str) -> str:
        if "Похожие специалисты" in salary:
            salary = salary.split("Похожие специалисты")[0]
        if salary.startswith("Зарплата не указан"):
            return "не указана"
        return salary.strip()

    def _parse_published_date(self, card) -> str:
        date_tag = card.select_one(".vacancy-card__date")
        if not date_tag:
            return ""
        time_el = date_tag.find("time")
        if time_el and time_el.get("datetime"):
            return time_el["datetime"]
        return date_tag.get_text(strip=True)

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

    def _parse_html(self, html: str) -> tuple[list[Vacancy], int]:
        """Возвращает (вакансии, прошедшие фильтры; сколько карточек было на странице)."""
        soup = BeautifulSoup(html, "html.parser")
        vacancies = []
        cards = soup.select(".vacancy-card")

        for card in cards:
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

                published_at = self._parse_published_date(card)
                url = "https://career.habr.com" + title_link.get("href", "")

                company_tag = card.select_one(".vacancy-card__company")
                company = self._extract_company(company_tag) if company_tag else "Не указана"

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
                    published_at=published_at,
                ))
            except Exception as e:
                logging.debug(f"[HABR] Ошибка парсинга карточки: {e}")
                continue

        return vacancies, len(cards)

    def fetch(self) -> list[Vacancy]:
        seen_ids = set()
        all_vacancies = []

        for query in QUERIES:
            logging.info(f"[HABR] Запрос: '{query}'")
            for page in range(1, MAX_PAGES_PER_QUERY + 1):
                html = self._fetch_page(query, page)
                if not html:
                    break

                vacancies, cards_count = self._parse_html(html)
                # Останавливаемся, только если страница пустая. Если карточки
                # были, но ни одна не прошла фильтры — следующая страница
                # всё равно может содержать подходящие.
                if cards_count == 0:
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