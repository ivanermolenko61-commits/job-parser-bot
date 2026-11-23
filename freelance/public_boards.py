"""Открытые списки объявлений Freelance.ru и Weblancer, без авторизации."""
import logging
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from freelance import sources
from freelance.alot_client import FreelanceOrder


FREELANCE_URL = "https://freelance.ru/task"
WEBLANCER_URL = "https://www.weblancer.net/freelance/"
MSK = ZoneInfo("Europe/Moscow")
FREELANCE_PATH = re.compile(r"/task/view/(\d{1,9})/?")
WEBLANCER_PATH = re.compile(r"/freelance/[^/]+/[^/]+-(\d{1,9})/?")
DAY = re.compile(r"\d{2}\.\d{2}\.\d{4}")
MINUTE = re.compile(r"\d{2}\.\d{2}\.\d{4} \d{2}:\d{2}")
BUDGET = re.compile(r"(?:[$€]\s*[\d ]+|[\d ]+\s*[$€₽])")
PUBLISHED = "[itemprop='datePublished'], [data-published-at], .published-at, .date-published"
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
        body = _text(card.select_one(".task-card__desc"))
        orders.append(FreelanceOrder(
            sources.FREELANCE_RU_ID_OFFSET + ident, sources.FREELANCE_RU, _text(anchor),
            body, _text(card.select_one(".task-card__budget .bold")), 0,
            [], _timestamp(timestamp), url, needs_full_description=not body or body.endswith(("...", "…")),
        ))
    if not orders and not re.search(r"нет открытых заданий|заданий не найдено", _text(soup), re.I):
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
    return parse_freelance_page(_get(FREELANCE_URL))


def fetch_freelance_description(url: str) -> str:
    """Пустой результат оставляет заказ ждать следующей попытки."""
    link = _link(FREELANCE_URL, url, FREELANCE_PATH)
    if not link:
        return ""
    try:
        return _freelance_description(_get(link[1]))
    except (requests.RequestException, RuntimeError, ValueError) as error:
        logging.warning("[FREELANCE] детали Freelance.ru %s недоступны: %s", link[0], type(error).__name__)
        return ""


def _weblancer_card(anchor):
    for card in anchor.parents:
        if card.name in ("main", "body", "html"):
            return None
        links = {_link(WEBLANCER_URL, tag["href"], WEBLANCER_PATH)
                 for tag in card.select("a[href]")}
        if len(links - {None}) > 1:
            return None
        text = _text(card)
        if len(text) > len(_text(anchor)) + 25 and (card.name == "article" or card.select_one(PUBLISHED)):
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
        date_tag = card.select_one(PUBLISHED)
        raw_date = (date_tag.get("datetime") or date_tag.get("content") or date_tag.get("data-published-at")
                    or _text(date_tag)) if date_tag else ""
        stamp = _timestamp(raw_date)
        if DAY.fullmatch(raw_date) and _too_old_day(raw_date, now):
            continue
        body = max((_text(tag) for tag in card.select("p")), key=len, default="")
        if not body:
            if date_tag:
                date_tag.extract()
            body = _text(card).removeprefix(_text(anchor)).strip()
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
