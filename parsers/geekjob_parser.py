"""Парсер вакансий с GeekJob (geekjob.ru).

Сайт — SPA на Vue.js. Требует Playwright для рендеринга JS.
Запускается в отдельном потоке через asyncio.to_thread.

Оптимизация памяти (важно: лимит контейнера — 1 ГБ RAM):
  • Блокировка картинок/шрифтов/медиа/трекеров.
  • Одна страница переиспользуется между запросами.
  • Аргументы Chromium для Docker + ограничение V8 heap и процессов.
  • Короткий список запросов (PLAYWRIGHT_QUERIES).
"""
import logging
import time
from urllib.parse import quote

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

from config import PLAYWRIGHT_QUERIES, REQUEST_DELAY
from parsers.base import BaseParser, Vacancy
from parsers.filters import is_allowed_level, is_relevant_title


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

    def __init__(self, headless: bool = True):
        self.headless = headless

    def _is_relevant(self, title: str) -> bool:
        return is_relevant_title(title)

    def _clean(self, s: str) -> str:
        if not s:
            return ""
        return " ".join(s.split()).strip()

    def _translate_labels(self, labels: list[str]) -> list[str]:
        result = []
        for label in labels:
            low = label.lower()
            translated = LABEL_TRANSLATIONS.get(low, label)
            result.append(translated)
        return result

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

            if not is_allowed_level(title):
                return None

            company_tag = card.select_one(".company-name")
            if company_tag:
                a = company_tag.find("a")
                company = self._clean((a or company_tag).get_text())
            else:
                company = "Не указана"

            raw_labels = []
            for sel in (".remote-label", ".relocate-label", ".parttime-label", ".inhouse-label"):
                el = card.select_one(sel)
                if el:
                    raw_labels.append(self._clean(el.get_text()))
            labels = self._translate_labels(raw_labels)

            location = " · ".join(labels) if labels else "—"
            is_remote = "удалённо" in labels

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
                published_at=published_at,
                experience="",
            )
        except Exception as e:
            logging.debug(f"[GEEKJOB] Ошибка парсинга карточки: {e}")
            return None

    def _parse_html(self, html: str) -> list[Vacancy]:
        soup = BeautifulSoup(html, "html.parser")
        cards = soup.select(".collection-item.avatar")
        self.cards_seen += len(cards)  # для health-мониторинга
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
            browser = p.chromium.launch(
                headless=self.headless,
                args=self.CHROMIUM_ARGS,
            )
            page = None
            try:
                page = browser.new_page()
                page.set_default_timeout(30000)
                page.route("**/*", self._route_handler)

                for query in PLAYWRIGHT_QUERIES:
                    logging.info(f"[GEEKJOB] Запрос: '{query}'")
                    url = f"{self.BASE_URL}?qs={quote(query)}"

                    try:
                        page.goto(url, wait_until="domcontentloaded", timeout=60000)
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
                if page is not None:
                    try:
                        page.close()
                    except Exception:
                        pass
                try:
                    browser.close()
                except Exception:
                    pass

        logging.info(f"[GEEKJOB] Итого собрано: {len(all_vacancies)}")
        return all_vacancies


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = GeekJobParser(headless=True)
    vacancies = parser.fetch()

    print(f"\nВсего собрано: {len(vacancies)}\n")
    for v in vacancies[:15]:
        print(v.format_message())
        print("-" * 60)