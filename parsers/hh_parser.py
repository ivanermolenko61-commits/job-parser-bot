"""Парсер вакансий с hh.ru через Playwright.

Сайт — SPA на React, requests не работает. Карточки при этом
приходят уже в HTML, поэтому страница грузится в Chromium с выключенным JS;
если так карточек нет — тот же запрос повторяется с JS (прежний способ).

Публичный API hh.ru закрыт с декабря 2025 — поэтому парсим HTML.
Дату публикации HH анонимно не отдаёт, поэтому поле published_at
всегда пустое. Свежесть обеспечивается параметром search_period в URL
запроса — HH сам отсекает вакансии старше указанного числа дней.

Оптимизация памяти (важно: лимит контейнера — 1 ГБ RAM):
  • JS выключен (пик ~300 МБ вместо ~770 МБ), с JS — только запасной путь.
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
from parsers.filters import is_allowed_level, is_relevant_title, is_russian_location


class HHParser(BaseParser):
    """Парсер вакансий с hh.ru через Playwright."""

    source_name = "hh"
    BASE_URL = "https://hh.ru/search/vacancy"

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
        # Один флаг: Chromium учитывает только последний --disable-features
        "--disable-features=site-per-process,BackForwardCache,Translate,OptimizationHints,MediaRouter",
        "--mute-audio",
    ]

    # Селектор карточки: один и для выборки из браузера, и для разбора в _parse_html
    CARD_SELECTOR = '[data-qa="vacancy-serp__vacancy"]'

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
        return is_relevant_title(title)

    def _is_russian_location(self, location: str) -> bool:
        return is_russian_location(location)

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

            if not is_allowed_level(title):
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
        cards = soup.select(self.CARD_SELECTOR)
        self.cards_seen += len(cards)  # для health-мониторинга
        logging.info(f"[HH] Найдено карточек: {len(cards)}")

        vacancies = []
        for card in cards:
            v = self._parse_card(card)
            if v:
                vacancies.append(v)

        logging.info(f"[HH] Прошло фильтры: {len(vacancies)}")
        return vacancies

    def _new_page(self, browser, js: bool):
        """Страница в своём контексте: с JS или без (JS — главный потребитель RAM)."""
        context = browser.new_context(java_script_enabled=js)
        page = context.new_page()
        page.set_default_timeout(30000)
        page.set_extra_http_headers({
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
        })
        page.route("**/*", self._route_handler)
        return page

    @staticmethod
    def _close_page(page) -> None:
        if page is None:
            return
        try:
            page.context.close()
        except Exception:
            pass

    def _cards_html(self, page) -> list[str]:
        # Только карточки, а не весь документ (hh отдаёт 1-3 МБ со встроенным JSON)
        return page.eval_on_selector_all(
            self.CARD_SELECTOR, "els => els.map(e => e.outerHTML)"
        )

    def _cards_without_js(self, page, url: str) -> list[str]:
        """hh отдаёт карточки сразу в HTML: без JS рендерер берёт ~80 МБ вместо ~500.

        Пустой список — значит без JS не вышло (капча, сайт стал требовать JS);
        тогда вызывающий повторяет запрос прежним способом, с JS.
        Таймаут загрузки пробрасывается: как и раньше, такой запрос пропускается
        сразу, без второй минутной попытки (иначе hh не уложится в свой таймаут).
        """
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            return self._cards_html(page)
        except PlaywrightTimeoutError:
            raise
        except Exception as e:
            logging.info(f"[HH] Без JS не получилось ({type(e).__name__}), повтор с JS")
            return []

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
                page = self._new_page(browser, js=False)

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
                            cards_html = self._cards_without_js(page, url)
                        except PlaywrightTimeoutError:
                            logging.warning(f"[HH] Таймаут для '{query}' стр. {page_num}")
                            break
                        if not cards_html:
                            # Запасной путь — ровно прежняя логика, со страницей с JS.
                            # Страница закрывается сразу, чтобы рендерер с JS не висел в памяти.
                            js_page = self._new_page(browser, js=True)
                            try:
                                try:
                                    js_page.goto(url, wait_until="domcontentloaded", timeout=60000)
                                except PlaywrightTimeoutError:
                                    logging.warning(f"[HH] Таймаут для '{query}' стр. {page_num}")
                                    break

                                try:
                                    js_page.wait_for_selector(
                                        self.CARD_SELECTOR,
                                        timeout=20000,
                                    )
                                except PlaywrightTimeoutError:
                                    logging.info(
                                        f"[HH] Нет карточек для '{query}' стр. {page_num}"
                                    )
                                    break

                                cards_html = self._cards_html(js_page)
                            finally:
                                self._close_page(js_page)

                        html = "<div>" + "".join(cards_html) + "</div>"
                        del cards_html
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
                self._close_page(page)
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