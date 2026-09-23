"""Парсер вакансий с GeekJob (geekjob.ru).

Сайт — SPA на Vue.js. Требует Playwright для рендеринга JS.
Запускается в отдельном потоке через asyncio.to_thread,
чтобы не блокировать event loop бота.
"""
import logging
import re
import time
from urllib.parse import quote

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

from config import QUERIES, REQUEST_DELAY
from parsers.base import BaseParser, Vacancy


# Перевод меток GeekJob на русский
LABEL_TRANSLATIONS = {
    "remote": "удалённо",
    "office": "офис",
    "parttime": "частичная занятость",
    "relocate": "релокация",
    "inhouse": "прямой работодатель",
}


class GeekJobParser(BaseParser):
    """Парсер вакансий с GeekJob через Playwright."""

    source_name = "geekjob"
    BASE_URL = "https://geekjob.ru/vacancies"

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
        "безопасност", "security", "appsec", "infosec",
    ]

    INCLUDE_IT_WORDS = [
        "python", "java", " go ", "golang", "javascript", " typescript",
        "c++", "c#", "php", "ruby", "swift", "kotlin", "scala", "rust",
        "developer", "разработчик", "программист",
        "backend", "бэкенд", "frontend", "фронтенд", "fullstack", "фулстек",
        "ml engineer", "ml-инженер", "data engineer", "data-инженер",
        "dba", "1с", "1c", "разработк",
        "embedded", "bios", "bsp",
        "ios", "android",
    ]

    LEVEL_PATTERN = re.compile(
        r"(Junior|Middle|Senior|Lead|Intern|Стажёр|Стажер|Trainee)",
        re.IGNORECASE,
    )
    ALLOWED_LEVELS = {"", "junior", "intern", "стажёр", "стажер", "trainee"}

    def __init__(self, headless: bool = True):
        self.headless = headless

    def _is_relevant(self, title: str) -> bool:
        t = title.lower()
        if any(w in t for w in self.EXCLUDE_WORDS):
            return False
        if any(w in t for w in self.SENIORITY_WORDS):
            return False
        if not any(w in t for w in self.INCLUDE_IT_WORDS):
            return False
        return True

    def _clean(self, s: str) -> str:
        if not s:
            return ""
        return " ".join(s.split()).strip()

    def _translate_labels(self, labels: list[str]) -> list[str]:
        """Переводит английские метки на русский."""
        result = []
        for label in labels:
            low = label.lower()
            translated = LABEL_TRANSLATIONS.get(low, label)
            result.append(translated)
        return result

    def _parse_card(self, card) -> Vacancy | None:
        try:
            title_link = card.select_one("a.title")
            if not title_link:
                return None
            title = self._clean(title_link.get_text())
            href = title_link.get("href", "")
            if not href.startswith("/vacancy/"):
                return None

            url = "https://geekjob.ru" + href
            vacancy_id = href.rstrip("/").split("/")[-1]

            if not self._is_relevant(title):
                return None

            # Уровень из заголовка
            m = self.LEVEL_PATTERN.search(title)
            level = m.group(1) if m else ""
            if level.lower() not in self.ALLOWED_LEVELS:
                return None

            # Компания
            company_tag = card.select_one(".company-name")
            if company_tag:
                a = company_tag.find("a")
                company = self._clean((a or company_tag).get_text())
            else:
                company = "Не указана"

            # Метки — переводим на русский
            raw_labels = []
            for sel in (".remote-label", ".relocate-label", ".parttime-label", ".inhouse-label"):
                el = card.select_one(sel)
                if el:
                    raw_labels.append(self._clean(el.get_text()))
            labels = self._translate_labels(raw_labels)

            location = " · ".join(labels) if labels else "—"
            is_remote = "удалённо" in labels

            # Дата публикации
            dt_tag = card.select_one(".datetime-info")
            published_at = self._clean(dt_tag.get_text()) if dt_tag else ""

            return Vacancy(
                source=self.source_name,
                vacancy_id=vacancy_id,
                title=title,
                company=company,
                url=url,
                location=location,
                salary="не указана",
                is_remote=is_remote,
                published_at=published_at,  # ← дата публикации, не опыт
                experience="",              # у GeekJob нет данных об опыте
            )
        except Exception as e:
            logging.debug(f"[GEEKJOB] Ошибка парсинга карточки: {e}")
            return None

    def _parse_html(self, html: str) -> list[Vacancy]:
        soup = BeautifulSoup(html, "html.parser")
        cards = soup.select(".collection-item.avatar")
        logging.info(f"[GEEKJOB] Найдено карточек: {len(cards)}")

        vacancies = []
        for card in cards:
            v = self._parse_card(card)
            if v:
                vacancies.append(v)

        logging.info(f"[GEEKJOB] Прошло фильтры: {len(vacancies)}")
        return vacancies

    def fetch(self) -> list[Vacancy]:
        seen_ids = set()
        all_vacancies = []

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=self.headless)
            try:
                page = browser.new_page()
                page.set_default_timeout(30000)

                for query in QUERIES:
                    logging.info(f"[GEEKJOB] Запрос: '{query}'")
                    url = f"{self.BASE_URL}?qs={quote(query)}"

                    try:
                        page.goto(url, wait_until="networkidle", timeout=30000)
                    except PlaywrightTimeoutError:
                        logging.warning(f"[GEEKJOB] Таймаут для '{query}'")
                        continue

                    try:
                        page.wait_for_selector(".collection-item.avatar", timeout=10000)
                    except PlaywrightTimeoutError:
                        logging.info(f"[GEEKJOB] Нет карточек для '{query}'")
                        continue

                    html = page.content()
                    vacancies = self._parse_html(html)

                    for v in vacancies:
                        if v.vacancy_id in seen_ids:
                            continue
                        seen_ids.add(v.vacancy_id)
                        all_vacancies.append(v)

                    time.sleep(REQUEST_DELAY)

            finally:
                browser.close()

        logging.info(f"[GEEKJOB] Итого собрано: {len(all_vacancies)}")
        return all_vacancies


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = GeekJobParser(headless=True)  # headless — без окна браузера
    vacancies = parser.fetch()

    print(f"\nВсего собрано: {len(vacancies)}\n")
    for v in vacancies[:15]:
        print(v.format_message())
        print("-" * 60)