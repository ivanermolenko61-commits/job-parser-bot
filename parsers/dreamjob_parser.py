"""Парсер вакансий с Dream Job (dreamjob.ru).

Использует requests + BeautifulSoup. Актуальная структура (2026):
карточки .vacancy-new__item, заголовок .vacancy-new__heading,
ссылка .vacancy-new__main-link в формате /employers/{id}/vakansii/{vid}.
"""
import logging
import re
import time

import requests
from bs4 import BeautifulSoup

from config import QUERIES, REQUEST_DELAY
from parsers.base import BaseParser, Vacancy


class DreamJobParser(BaseParser):
    """Парсер вакансий с Dream Job."""

    source_name = "dreamjob"
    BASE_URL = "https://dreamjob.ru/vakansii"

    SENIORITY_WORDS = [
        "senior", "lead ", "principal", "middle", "head of",
        "team lead", "tech lead", "director", "руководитель",
    ]

    EXCLUDE_WORDS = [
        # Менеджмент и продукт
        "менеджер", "manager", "product", "продукт",
        "проектами", "проектов",
        # Аналитика, дизайн, research
        "аналитик", "analyst",
        "дизайнер", "designer",
        "исследователь", "researcher", "research", "ресерчер",
        # QA и тестирование
        "qa", "тестировщик", "тестирован", "testing",
        "quality assurance",
        # Инфраструктура
        "devops", "sre", "administrator", "администратор",
        # Поддержка и сопровождение
        "техподдержка", "поддержки", "сопровождени", "support",
        # Маркетинг, HR, продажи
        "маркетолог", "marketing", "hr ", "рекрутер",
        "sales", "продаж",
        # Юридические, документарные, бухгалтерия
        "бухгалтер", "accountant", "юрист", "юрисконсульт", "логист",
        "документами", "документооборот", "делопроизвод",
        # Прочие профессии
        "тренер", "coach",
        "воспитатель", "учитель", "педагог", "репетитор",
        "банк", "банка", "банке", "банковск",
        "риелтор", "риэлтор", "недвижимост",
        "агент по", "агентств",
        "юридических лиц", "юридическим",
        "кредитных", "кредитован", "ипотек",
        "секретарь", "офис-менеджер",
        "водитель", "курьер", "продавец", "кассир",
        "оператор call", "оператор колл",
        "сварщик", "электрик", "монтажник",
        "медицинск", "врач", "медсестр",
        # Промышленность, не IT
        "чпу", "cnc", "станк", "станков",
        "инженер-конструктор", "инженер-технолог",
    ]

    # IT-слова: хотя бы одно должно быть в заголовке
    INCLUDE_IT_WORDS = [
        "python", "java", " go ", "golang", "javascript", " typescript",
        "c++", "c#", "php", "ruby", "swift", "kotlin", "scala", "rust",
        "developer", "разработчик", "программист",
        "backend", "бэкенд", "frontend", "фронтенд", "fullstack", "фулстек",
        "ml engineer", "ml-инженер", "data engineer", "data-инженер",
        "dba", "1с", "1c", "разработк",
    ]

    # Нероссийские города/страны — отсеиваем
    GEO_BLACKLIST = [
        "ташкент", "алматы", "астана", "нур-султан",
        "тбилиси", "баку", "ереван", "бишкек", "минск",
        "душанбе", "ашхабад", "кишинёв", "кишинев", "киев",
        "казахстан", "узбекистан", "грузия", "азербайджан",
        "армения", "киргизия", "кыргызстан", "беларусь",
        "белоруссия", "таджикистан", "туркменистан", "молдова",
    ]

    # Нероссийские валюты — отсеиваем
    FOREIGN_CURRENCIES = [
        "so'm", "сум", "₸", "тенге", "₼", "манат", "₾", "лари",
        "$", "€", "£", "₴", "гривн", "лей",
    ]

    # Опыт, который отсеиваем (по тегам карточки)
    EXCLUDE_EXPERIENCE_TAGS = [
        "от 3 до 6 лет", "3-6 лет", "3–6 лет",
        "более 6 лет", "6+ лет",
    ]

    VACANCY_HREF_PATTERN = re.compile(r"/employers/\d+/vakansii/(\d+)")

    def __init__(self, max_pages: int = 5):
        self.max_pages = max_pages
        self.headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        }

    def _fetch_page(self, query: str, page: int) -> str | None:
        params = {"jbfrp[text]": query, "page": page}
        try:
            response = requests.get(
                self.BASE_URL, params=params,
                headers=self.headers, timeout=15,
            )
            response.raise_for_status()
            return response.text
        except requests.RequestException as e:
            logging.warning(f"[DREAMJOB] Ошибка '{query}' (page={page}): {e}")
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

    def _is_russian_location(self, location: str) -> bool:
        if not location:
            return True
        loc = location.lower()
        return not any(city in loc for city in self.GEO_BLACKLIST)

    def _is_russian_salary(self, salary: str) -> bool:
        if not salary or salary == "не указана":
            return True
        sal = salary.lower()
        return not any(cur in sal for cur in self.FOREIGN_CURRENCIES)

    def _get_tags(self, card) -> list[str]:
        return [self._clean(t.get_text()) for t in card.select(".tags__item")]

    def _check_experience(self, tags: list[str]) -> bool:
        tags_lower = [t.lower() for t in tags]
        for tag in tags_lower:
            for excl in self.EXCLUDE_EXPERIENCE_TAGS:
                if excl in tag:
                    return False
        return True

    def _clean(self, s: str) -> str:
        if not s:
            return ""
        return " ".join(s.split()).strip()

    def _parse_card(self, card) -> Vacancy | None:
        try:
            title_tag = card.select_one(".vacancy-new__heading")
            if not title_tag:
                return None
            title = self._clean(title_tag.get_text())

            if not self._is_relevant_category(title):
                return None

            link_tag = card.select_one(".vacancy-new__main-link")
            if not link_tag:
                return None
            href = link_tag.get("href", "")
            m = self.VACANCY_HREF_PATTERN.search(href)
            if not m:
                return None
            vacancy_id = m.group(1)
            url = "https://dreamjob.ru" + href if href.startswith("/") else href

            company_tag = card.select_one(".vacancy-new__employer-name")
            company = self._clean(company_tag.get_text()) if company_tag else "Не указана"

            city_tag = card.select_one(".vacancy-new__city")
            location = self._clean(city_tag.get_text()) if city_tag else ""

            if not self._is_russian_location(location):
                logging.debug(f"[DREAMJOB] Пропуск (гео {location}): {title}")
                return None

            salary_tag = card.select_one(".vacancy-new__salary")
            salary = self._clean(salary_tag.get_text()) if salary_tag else ""
            if not salary:
                salary = "не указана"

            if not self._is_russian_salary(salary):
                logging.debug(f"[DREAMJOB] Пропуск (валюта {salary}): {title}")
                return None

            tags = self._get_tags(card)
            if not self._check_experience(tags):
                return None

            experience = ""
            for t in tags:
                tl = t.lower()
                if any(x in tl for x in ["опыт", "года", "лет", "junior", "стаж", "intern", "trainee"]):
                    experience = t
                    break

            card_text = card.get_text(" ", strip=True).lower()
            is_remote = "удалён" in card_text or "удален" in card_text

            return Vacancy(
                source=self.source_name,
                vacancy_id=vacancy_id,
                title=title,
                company=company,
                url=url,
                location=location or "—",
                salary=salary,
                is_remote=is_remote,
                published_at="",
                experience=experience,
            )
        except Exception as e:
            logging.debug(f"[DREAMJOB] Ошибка парсинга: {e}")
            return None

    def _parse_html(self, html: str) -> list[Vacancy]:
        soup = BeautifulSoup(html, "html.parser")
        cards = soup.select(".vacancy-new__item")
        logging.info(f"[DREAMJOB] Найдено карточек: {len(cards)}")

        vacancies = []
        for card in cards:
            v = self._parse_card(card)
            if v:
                vacancies.append(v)

        logging.info(f"[DREAMJOB] Прошло фильтры: {len(vacancies)}")
        return vacancies

    def fetch(self) -> list[Vacancy]:
        seen_ids = set()
        all_vacancies = []

        for query in QUERIES:
            logging.info(f"[DREAMJOB] Запрос: '{query}'")
            for page in range(1, self.max_pages + 1):
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

                if page < self.max_pages:
                    time.sleep(REQUEST_DELAY)

            time.sleep(REQUEST_DELAY)

        logging.info(f"[DREAMJOB] Итого собрано: {len(all_vacancies)}")
        return all_vacancies


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = DreamJobParser()
    vacancies = parser.fetch()

    print(f"\nВсего собрано: {len(vacancies)}\n")
    for v in vacancies[:15]:
        print(v.format_message())
        print("-" * 60)