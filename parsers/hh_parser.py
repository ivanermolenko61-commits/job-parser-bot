"""Парсер вакансий с hh.ru через Playwright.

Сайт — динамический (SPA на React), requests не работает. Требуется
Playwright для рендеринга JS.

Публичный API hh.ru закрыт с декабря 2025 — поэтому парсим HTML.
Дату публикации HH анонимно не отдаёт, поэтому поле published_at
всегда пустое. Свежесть обеспечивается параметром search_period в URL
запроса — HH сам отсекает вакансии старше указанного числа дней.

Оптимизация памяти (важно: лимит контейнера — 1 ГБ RAM):
  • Блокировка картинок/шрифтов/медиа/трекеров.
  • Одна страница переиспользуется.
  • Аргументы Chromium для Docker + ограничение V8 heap и процессов.
  • Короткий список запросов (PLAYWRIGHT_QUERIES).
  • items_on_page=50 — меньше навигаций при том же охвате.
"""
import logging
import re
import time
from urllib.parse import quote

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

from config import MAX_VACANCY_AGE_DAYS, PLAYWRIGHT_QUERIES, REQUEST_DELAY
from parsers.base import BaseParser, Vacancy


class HHParser(BaseParser):
    """Парсер вакансий с hh.ru через Playwright."""

    source_name = "hh"
    BASE_URL = "https://hh.ru/search/vacancy"

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
        "методолог", "seo", "igaming", "гейминг",
        "wordpress", "битрикс", "bitrix",
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

    GEO_BLACKLIST = [
        "ташкент", "алматы", "астана", "нур-султан",
        "тбилиси", "баку", "ереван", "бишкек", "минск", "брест",
        "гомель", "витебск", "гродно", "могилёв", "могилев",
        "душанбе", "ашхабад", "кишинёв", "кишинев", "киев",
        "львов", "харьков", "одесса", "днепр",
        "казахстан", "узбекистан", "грузия", "азербайджан",
        "армения", "киргизия", "кыргызстан", "беларусь",
        "белоруссия", "таджикистан", "туркменистан", "молдова",
        "украина",
    ]

    LEVEL_PATTERN = re.compile(
        r"(Junior|Middle|Senior|Lead|Intern|Стажёр|Стажер|Trainee)",
        re.IGNORECASE,
    )
    ALLOWED_LEVELS = {"", "junior", "intern", "стажёр", "стажер", "trainee"}

    # Аргументы Chromium: важно для Docker с 1 ГБ RAM.
    CHROMIUM_ARGS = [
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--disable-extensions",
        "--disable-software-rasterizer",
        "--disable-background-networking",
        "--disable-sync",
        "--js-flags=--max-old-space-size=256",
        "--renderer-process-limit=1",
        "--disable-features=site-per-process",
    ]

    # Хосты-трекеры и реклама — режем, чтобы не грузить их JS.
    BLOCKED_HOSTS = (
        "mc.yandex.ru",
        "google-analytics.com",
        "googletagmanager.com",
        "facebook.net",
        "facebook.com",
        "sentry.io",
        "top-fwz1.mail.ru",
        "ad.mail.ru",
    )

    def __init__(self, headless: bool = True, max_pages: int = 1):
        self.headless = headless
        self.max_pages = max_pages

    def _is_relevant(self, title: str) -> bool:
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

    def _clean(self, s: str) -> str:
        if not s:
            return ""
        return " ".join(s.split()).strip()

    def _route_handler(self, route) -> None:
        """Блокирует картинки/медиа/шрифты/трекеры — экономит RAM и трафик."""
        try:
            if route.request.resource_type in ("image", "media", "font"):
                route.abort()
                return

            url = route.request.url
            if "://" in url:
                host = url.split("/")[2].lower()
                if any(b in host for b in self.BLOCKED_HOSTS):
                    route.abort()
                    return

            route.continue_()
        except Exception:
            try:
                route.continue_()
            except Exception:
                pass

    def _parse_card(self, card) -> Vacancy | None:
        try:
            title_link = card.select_one('[data-qa="serp-item__title"]')
            if not title_link:
                return None

            title = self._clean(title_link.get_text())
            href = title_link.get("href", "")
            if not href:
                return None

            if "?" in href:
                href = href.split("?")[0]

            m = re.search(r"/vacancy/(\d+)", href)
            if not m:
                return None
            vacancy_id = m.group(1)

            if not self._is_relevant(title):
                return None

            level_m = self.LEVEL_PATTERN.search(title)
            level = level_m.group(1) if level_m else ""
            if level.lower() not in self.ALLOWED_LEVELS:
                return None

            company_tag = card.select_one('[data-qa="vacancy-serp__vacancy-employer-text"]')
            company = self._clean(company_tag.get_text()) if company_tag else "Не указана"

            location_tag = card.select_one('[data-qa="vacancy-serp__vacancy-address"]')
            location = self._clean(location_tag.get_text()) if location_tag else ""

            if not self._is_russian_location(location):
                logging.debug(f"[HH] Пропуск (гео {location}): {title}")
                return None

            salary_tag = card.select_one('[data-qa="vacancy-serp__compensation"]')
            salary = self._clean(salary_tag.get_text()) if salary_tag else "не указана"
            if not salary:
                salary = "не указана"

            is_remote = card.select_one('[data-qa="vacancy-label-work-schedule-remote"]') is not None
            if is_remote and location:
                location = f"{location} · удалённо"
            elif is_remote:
                location = "удалённо"

            # HH анонимно дату публикации не отдаёт. Свежесть обеспечивается
            # параметром search_period в URL запроса — HH сам отсекает старое.
            published_at = ""

            return Vacancy(
                source=self.source_name,
                vacancy_id=vacancy_id,
                title=title,
                company=company,
                url=href,
                location=location or "—",
                salary=salary,
                is_remote=is_remote,
                published_at=published_at,
                experience="",
                applications_count="",
            )
        except Exception as e:
            logging.debug(f"[HH] Ошибка парсинга карточки: {e}")
            return None

    def _parse_html(self, html: str) -> list[Vacancy]:
        soup = BeautifulSoup(html, "html.parser")
        cards = soup.select('[data-qa="vacancy-serp__vacancy"]')
        logging.info(f"[HH] Найдено карточек: {len(cards)}")

        vacancies = []
        for card in cards:
            v = self._parse_card(card)
            if v:
                vacancies.append(v)

        logging.info(f"[HH] Прошло фильтры: {len(vacancies)}")
        return vacancies

    def fetch(self) -> list[Vacancy]:
        seen_ids = set()
        all_vacancies = []

        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=self.headless,
                args=self.CHROMIUM_ARGS,
            )
            page = None
            try:
                page = browser.new_page()
                page.set_default_timeout(30000)
                page.set_extra_http_headers({
                    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
                })
                page.route("**/*", self._route_handler)

                for query in PLAYWRIGHT_QUERIES:
                    logging.info(f"[HH] Запрос: '{query}'")

                    for page_num in range(self.max_pages):
                        params = (
                            f"?text={quote(query)}"
                            f"&experience=noExperience"
                            f"&experience=between1And3"
                            f"&items_on_page=50"
                            f"&order_by=publication_time"
                            f"&search_period={MAX_VACANCY_AGE_DAYS}"
                            f"&page={page_num}"
                        )
                        url = self.BASE_URL + params

                        try:
                            page.goto(url, wait_until="domcontentloaded", timeout=60000)
                        except PlaywrightTimeoutError:
                            logging.warning(f"[HH] Таймаут для '{query}' стр. {page_num}")
                            break

                        try:
                            page.wait_for_selector(
                                '[data-qa="vacancy-serp__vacancy"]',
                                timeout=20000,
                            )
                        except PlaywrightTimeoutError:
                            logging.info(
                                f"[HH] Нет карточек для '{query}' стр. {page_num}"
                            )
                            break

                        html = page.content()
                        vacancies = self._parse_html(html)

                        if not vacancies:
                            break

                        for v in vacancies:
                            if v.vacancy_id in seen_ids:
                                continue
                            seen_ids.add(v.vacancy_id)
                            all_vacancies.append(v)

                        time.sleep(REQUEST_DELAY)

                    time.sleep(REQUEST_DELAY)

            finally:
                if page is not None:
                    try:
                        page.close()
                    except Exception:
                        pass
                try:
                    browser.close()
                except Exception:
                    pass

        logging.info(f"[HH] Итого собрано: {len(all_vacancies)}")
        return all_vacancies


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = HHParser(headless=True, max_pages=1)
    vacancies = parser.fetch()

    print(f"\nВсего собрано: {len(vacancies)}\n")
    for v in vacancies[:15]:
        print(v.format_message())
        print("-" * 60)