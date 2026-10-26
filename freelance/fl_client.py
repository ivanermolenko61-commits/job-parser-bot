"""Клиент RSS FL.ru - второй источник фриланс-заказов, независимый от alot.pro.

GET https://www.fl.ru/rss/all.xml, авторизация не нужна, ~60 последних заказов.
В RSS нет отдельного поля бюджета: он стоит в конце названия «(Бюджет: ...)» и выносится
в price_text; текст заказа - только начало описания.
Заказы отдаются тем же FreelanceOrder, что и alot: order_id = FL_ID_OFFSET + id заказа
на FL.ru, чтобы не пересечься с id alot.pro в общей таблице freelance_orders.
"""
import html
import logging
import re
import xml.etree.ElementTree as ET
from datetime import timezone
from email.utils import parsedate_to_datetime

import requests

from config import FL_RSS_URL
from freelance.alot_client import FreelanceOrder, _clean_body

FL_ID_OFFSET = 10 ** 10
FL_SITE = "flru"
REQUEST_TIMEOUT = 20
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/rss+xml, application/xml, text/xml",
}
_LINK_ID_RE = re.compile(r"/projects/(\d+)")


def is_fl_id(order_id: int) -> bool:
    return order_id >= FL_ID_OFFSET


# Префильтр (freelance/filters.py) опирается на коды категорий alot.pro, а у FL.ru они
# текстовые («Программирование / Парсинг данных»): добавляем соответствующий код разработки
_WEB_SKIP = ("дизайн", "продвижение", "битрикс", "seo")


def _dev_code(category: str) -> str | None:
    low = category.lower()
    if low.startswith(("программирование", "ai ", "ai—", "ai —")):
        return "PROG_OTHER"
    if low.startswith("сайты") and not any(w in low for w in _WEB_SKIP):
        return "WEB"
    return None


# Бюджет FL.ru кладёт в конец названия: «Нужен бот (Бюджет: 3 000 &#8381;)»
_BUDGET_RE = re.compile(r"\s*\(\s*Бюджет:\s*([^)]*)\)\s*$", re.IGNORECASE)


def _split_budget(raw_title: str) -> tuple[str, str]:
    """(название без хвоста с бюджетом, текст бюджета); сущности HTML раскрываются."""
    title = html.unescape(raw_title).strip()
    m = _BUDGET_RE.search(title)
    if not m:
        return title, ""
    return title[:m.start()].strip(), re.sub(r"\s+", " ", m.group(1)).strip()


def _price_value(price_text: str) -> float:
    digits = re.sub(r"[^\d]", "", price_text)
    return float(digits) if digits else 0.0


def _parse_item(item: ET.Element) -> FreelanceOrder | None:
    try:
        link = (item.findtext("link") or "").strip()
        m = _LINK_ID_RE.search(link)
        if not m:
            raise ValueError("нет id заказа в ссылке")
        published = parsedate_to_datetime(item.findtext("pubDate") or "")
        if published.tzinfo is None:
            published = published.replace(tzinfo=timezone.utc)
        category = (item.findtext("category") or "").strip()
        categories = [category] if category else []
        code = _dev_code(category)
        if code:
            categories.append(code)
        title, price_text = _split_budget(item.findtext("title") or "")
        return FreelanceOrder(
            order_id=FL_ID_OFFSET + int(m.group(1)),
            site=FL_SITE,
            title=title,
            body=_clean_body(item.findtext("description") or ""),
            price_text=price_text,
            price_value=_price_value(price_text),
            categories=categories,
            published_at=published.astimezone(timezone.utc),
            url=link,
        )
    except (TypeError, ValueError, OverflowError):
        # содержимое заказа в лог не пишем: там могут быть контакты заказчика
        logging.warning("[FL] не удалось разобрать заказ из RSS")
        return None


def parse_rss(xml_data: bytes | str) -> list[FreelanceOrder]:
    """Разбирает RSS. Битый XML или лента без <channel> - исключение (источник сломан)."""
    try:
        root = ET.fromstring(xml_data)
    except ET.ParseError as e:
        raise RuntimeError(f"FL.ru: некорректный RSS ({e})") from e
    if root.find("channel") is None:
        raise RuntimeError("FL.ru: в ответе нет <channel>")
    return [o for o in (_parse_item(i) for i in root.iter("item")) if o]


def fetch_fl_orders() -> list[FreelanceOrder]:
    """Свежие заказы FL.ru. Сетевой сбой или не-RSS ответ - исключение."""
    resp = requests.get(FL_RSS_URL, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return parse_rss(resp.content)  # байты: кодировку берёт сам XML-заголовок


if __name__ == "__main__":
    from freelance.filters import is_candidate

    logging.basicConfig(level=logging.INFO)
    orders = fetch_fl_orders()
    passed = [o for o in orders if is_candidate(o)]
    print(f"Заказов в RSS: {len(orders)}, прошли префильтр: {len(passed)}")
    for o in sorted(passed, key=lambda o: o.published_at, reverse=True)[:20]:
        t = o.published_at.astimezone().strftime("%d.%m %H:%M")
        print(f"{o.order_id} {t} | {','.join(o.categories)} | {o.title[:70]}")
