"""Публичные заказы CONTRACTIS и Kadrof."""
import json
import re
from datetime import datetime, timezone

from bs4 import BeautifulSoup

from freelance import sources
from freelance.alot_client import FreelanceOrder, _clean_body
from freelance.public_boards import _get, _link, _text, _timestamp

CONTRACTIS_URL = "https://contractis.ru/order/c/programmirovanie"
KADROF_URL = "https://www.kadrof.ru/work"
KADROF_PATH = re.compile(r"/work/(\d{1,9})/?")


def parse_kadrof(page: str) -> list[FreelanceOrder]:
    soup = BeautifulSoup(page, "html.parser")
    orders = {}
    for card in soup.select(".post-list .post.project-list"):
        anchor = card.select_one(".project-title h6 a[href]")
        if not anchor:
            continue
        link = _link(KADROF_URL, anchor["href"], KADROF_PATH)
        stamp = _timestamp(_text(card.select_one(".date")).replace(" в ", " "))
        if not link or not stamp:
            continue
        ident, url = link
        body = " ".join(s.strip() for s in card.find_all(string=True, recursive=False) if s.strip())
        orders[ident] = FreelanceOrder(
            sources.KADROF_ID_OFFSET + ident, "kadrofru", _text(anchor), body,
            _text(card.select_one(".budget")), 0, [], stamp, url,
        )
    if not orders:
        raise ValueError("Kadrof: не распознаны карточки каталога")
    return list(orders.values())


def fetch_kadrof() -> list[FreelanceOrder]:
    return parse_kadrof(_get(KADROF_URL))


def parse_contractis(page: str) -> list[FreelanceOrder]:
    soup = BeautifulSoup(page, "html.parser")
    data = soup.select_one("script#__NUXT_DATA__")
    if not data:
        raise ValueError("CONTRACTIS: отсутствуют данные каталога")
    values = json.loads(data.get_text())
    links = {a["href"] for a in soup.select('a[href^="/order/"]')}
    result = {}
    recognized = False
    for row in values:
        if not isinstance(row, dict) or not {"publishedDate", "slug", "startPrice"} <= row.keys():
            continue
        def field(key):
            index = row.get(key)
            return values[index] if type(index) is int and 0 <= index < len(values) else None

        slug = field("slug")
        ident = field("id")
        if not isinstance(slug, str) or not re.fullmatch("[a-z0-9-]+", slug) or f"/order/{slug}" not in links:
            continue
        recognized = True
        if field("status") != "Open" or field("moderStatus") != "Approved":
            continue
        if type(ident) is not int or not 0 < ident < 10**10:
            continue
        try:
            stamp = datetime.fromisoformat(field("publishedDate") or "")
        except ValueError:
            continue
        if stamp.tzinfo is None:
            # CONTRACTIS exposes the same value with a Z suffix in the order's datePosted.
            stamp = stamp.replace(tzinfo=timezone.utc)
        prices = [str(n) for n in (field("startPrice"), field("endPrice")) if type(n) in (int, float) and n > 0]
        price = " – ".join(dict.fromkeys(prices)) + " ₽" if prices else ""
        if price and field("paymentType") == "hourly":
            price += "/час"
        result[ident] = FreelanceOrder(
            sources.CONTRACTIS_ID_OFFSET + ident, sources.CONTRACTIS, field("title"),
            _clean_body(field("description") or ""), price, 0, [],
            stamp.astimezone(timezone.utc), f"https://contractis.ru/order/{slug}",
        )
    if not recognized:
        for tag in soup.select('script[type="application/ld+json"]'):
            metadata = json.loads(tag.get_text())
            if isinstance(metadata, dict) and metadata.get("@type") == "ItemList" and metadata.get("numberOfItems") == 0:
                return []
        raise ValueError("CONTRACTIS: не распознаны карточки каталога")
    return list(result.values())


def fetch_contractis() -> list[FreelanceOrder]:
    return parse_contractis(_get(CONTRACTIS_URL))
