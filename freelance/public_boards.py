"""Открытые списки объявлений Freelance.ru и Weblancer, без авторизации."""
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from database import freelance_known
from freelance import sources
from freelance.alot_client import FreelanceOrder
from freelance.filters import reject_reason


FREELANCE_URL = "https://freelance.ru/task"
WEBLANCER_URL = "https://www.weblancer.net/freelance/"
MSK = ZoneInfo("Europe/Moscow")
FREELANCE_PATH = re.compile(r"/task/view/(\d{1,9})/?")
WEBLANCER_PATH = re.compile(r"/freelance/[^/]+/[^/]+-(\d{1,9})/?")
DAY = re.compile(r"\d{2}\.\d{2}\.\d{4}")
MINUTE = re.compile(r"\d{2}\.\d{2}\.\d{4} \d{2}:\d{2}")
BUDGET = re.compile(r"(?:[$€]\s*[\d ]+|[\d ]+\s*[$€₽])")
MAX_DETAIL = 4
MAX_HTML = 1_500_000


def _get(url: str) -> str:
    with requests.get(url, timeout=(4, 6), stream=True, allow_redirects=False,
                      headers={"User-Agent": "job-parser-bot/1.0", "Accept": "text/html"}) as response:
        response.raise_for_status()
        if response.status_code != 200:
            raise RuntimeError(f"страница заказов вернула HTTP {response.status_code}")
        raw = bytearray()
        for chunk in response.iter_content(32768):
            raw.extend(chunk)
            if len(raw) > MAX_HTML:
                raise ValueError("страница заказов превышает лимит")
    return raw.decode("utf-8", errors="replace")


def _link(base: str, href: str, pattern: re.Pattern) -> tuple[int, str] | None:
    try:
        url = urlsplit(urljoin(base, href))
    except ValueError:
        return None
    if url.scheme != "https" or url.netloc != urlsplit(base).netloc:
        return None
    match = pattern.fullmatch(url.path)
    if match:
        return int(match[1]), f"https://{url.netloc}{url.path}"
    return None


def _text(node) -> str:
    return node.get_text(" ", strip=True) if hasattr(node, "get_text") else ""


def _timestamp(raw: str) -> datetime | None:
    try:
        if MINUTE.fullmatch(raw):
            stamp = datetime.strptime(raw, "%d.%m.%Y %H:%M").replace(tzinfo=MSK)
        elif "T" in raw:
            stamp = datetime.fromisoformat(raw)
        else:
            return None
        return stamp.astimezone(timezone.utc) if stamp.tzinfo is not None else None
    except ValueError:
        return None


def _too_old_day(raw: str, now: datetime) -> bool:
    """День без времени нельзя округлить до 00:00 и выдать за точную дату."""
    try:
        day = datetime.strptime(raw, "%d.%m.%Y").date()
    except ValueError:
        return False
    return day < (now.astimezone(MSK) - timedelta(hours=1)).date()


def parse_freelance_page(page: str) -> list[FreelanceOrder]:
    soup = BeautifulSoup(page, "html.parser")
    orders = []
    for card in soup.select("article.task-card"):
        anchor = card.select_one("a.task-card__title-link[href]")
        if not anchor or not (link := _link(FREELANCE_URL, anchor["href"], FREELANCE_PATH)):
            continue
        ident, url = link
        timestamp = next((tag["title"] for tag in card.select(".task-card__foot-item[title]")
                          if tag.select_one(".fa-clock-o")), "")
        orders.append(FreelanceOrder(
            sources.FREELANCE_RU_ID_OFFSET + ident, sources.FREELANCE_RU, _text(anchor),
            _text(card.select_one(".task-card__desc")), _text(card.select_one(".task-card__budget .bold")), 0,
            [], _timestamp(timestamp), url,
        ))
    if not orders and "Открытые задания" not in soup.get_text(" ", strip=True):
        raise ValueError("не найдена разметка списка Freelance.ru")
    return orders


def _freelance_description(page: str) -> str:
    heading = BeautifulSoup(page, "html.parser").select_one(".tv-hero h1")
    if heading is None:
        return ""
    body = []
    for part in heading.next_siblings:
        if "tv-hero__meta" in getattr(part, "attrs", {}).get("class", ()):
            break
        if getattr(part, "name", None) not in ("script", "style"):
            body.append(_text(part) if getattr(part, "name", None) else str(part).strip())
    return " ".join(filter(None, body))


def fetch_freelance_ru() -> list[FreelanceOrder]:
    orders = parse_freelance_page(_get(FREELANCE_URL))
    now = datetime.now(timezone.utc)
    known = freelance_known([order.order_id for order in orders])
    ready, remaining = [], MAX_DETAIL
    for order in orders:
        needs_detail = (order.body.endswith(("...", "…")) and reject_reason(order) is None
                        and (order.published_at is None or now - order.published_at <= timedelta(hours=1)))
        if needs_detail:
            if order.order_id in known or remaining <= 0:
                continue
            remaining -= 1
            try:
                body = _freelance_description(_get(order.url))
            except (requests.RequestException, RuntimeError, ValueError):
                # Предпросмотр остаётся полезен; нехватка деталей будет видна в оценке.
                body = ""
            if body:
                order.body = body
        ready.append(order)
    return ready


def _weblancer_card(anchor):
    for card in anchor.parents:
        if card.name in ("main", "body", "html"):
            return None
        links = {_link(WEBLANCER_URL, tag["href"], WEBLANCER_PATH)
                 for tag in card.select("a[href]")}
        if len(links - {None}) > 1:
            return None
        text = _text(card)
        if len(text) > len(_text(anchor)) + 25 and (DAY.search(text) or card.select_one("time[datetime]")):
            return card
    return None


def parse_weblancer_page(page: str, now: datetime | None = None) -> list[FreelanceOrder]:
    soup = BeautifulSoup(page, "html.parser")
    now = now or datetime.now(timezone.utc)
    result, recognized = {}, False
    for anchor in soup.select("a[href]"):
        link = _link(WEBLANCER_URL, anchor["href"], WEBLANCER_PATH)
        if not link or not _text(anchor):
            continue
        ident, url = link
        card = _weblancer_card(anchor)
        if card is None:
            continue
        recognized = True
        status = card.select_one("[data-status], .status")
        if status and re.search(r"\b(закрыт\w*|заверш[её]н\w*|closed|completed)\b",
                                f"{status.get('data-status', '')} {_text(status)}", re.I):
            continue
        text = _text(card)
        date_tag = card.select_one("time[datetime]")
        stamp = _timestamp(date_tag["datetime"]) if date_tag else None
        minute, day = MINUTE.search(text), DAY.search(text)
        if stamp is None and minute:
            stamp = _timestamp(minute[0])
        if stamp is None and day and _too_old_day(day[0], now):
            continue
        body = max((_text(tag) for tag in card.select("p")), key=len, default="")
        if not body:
            body = text.removeprefix(_text(anchor)).strip()
            if day:
                body = body.split(day[0], 1)[0].strip()
        if len(body) < 20:
            continue
        budget = BUDGET.search(text[:len(_text(anchor)) + 45])
        result.setdefault(ident, FreelanceOrder(
            sources.WEBLANCER_ID_OFFSET + ident, sources.WEBLANCER, _text(anchor), body,
            budget[0].strip() if budget else "", 0, [], stamp, url,
        ))
    if not recognized and not re.search(r"\b0\s+заказов\b", soup.get_text(" ", strip=True)):
        raise ValueError("не найдена разметка списка Weblancer")
    return list(result.values())


def fetch_weblancer() -> list[FreelanceOrder]:
    return parse_weblancer_page(_get(WEBLANCER_URL))
