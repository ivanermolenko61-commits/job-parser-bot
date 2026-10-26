"""Клиент Kwork - ещё один источник фриланс-заказов, независимый от alot.pro.

GET https://kwork.ru/projects?c=11&page=N (категория «Разработка и IT», по 12 проектов на
странице, авторизация не нужна). Данные лежат в JSON внутри страницы: window.stateData=
{... "wantsListData": {"wants": [...]}}. Заказы отдаются тем же FreelanceOrder, что и alot:
order_id = KWORK_ID_OFFSET + id проекта на Kwork. Время на Kwork московское.
"""
import html
import json
import logging
import time
from datetime import datetime, timedelta, timezone

import requests

from config import KWORK_PAGES, KWORK_URL
from freelance.alot_client import FreelanceOrder, _clean_body
from freelance.sources import KWORK_ID_OFFSET

KWORK_SITE = "kworkru"
DEV_CATEGORY = 11            # «Разработка и IT»
REQUEST_TIMEOUT = 20
PAGE_PAUSE_SEC = 1.0
MSK = timezone(timedelta(hours=3))
_STATE_MARK = "window.stateData="
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "ru-RU,ru;q=0.9",
}

# Префильтр (freelance/filters.py) опирается на коды категорий alot.pro. Код разработки
# ставим только для однозначно IT-подкатегорий Kwork: сайты, вёрстка, десктоп, мобильные,
# серверы, тесты. «Скрипты, боты и mini apps» (41) и «Игры» (40) забиты ИИ-генерацией
# картинок/видео - их решают ключевые слова, иначе лишние заказы тратят квоту AI.
_WEB_CATEGORIES = {"37", "38", "79"}
_CODE_CATEGORIES = {"80", "39", "255", "81"}


def kwork_url(project_id: int) -> str:
    return f"https://kwork.ru/projects/{project_id}/view"


def is_kwork_id(order_id: int) -> bool:
    return order_id >= KWORK_ID_OFFSET


def _dev_codes(category: str) -> list[str]:
    if category in _WEB_CATEGORIES:
        return ["WEB"]
    if category in _CODE_CATEGORIES:
        return ["PROG_OTHER"]
    return []


def _price(raw) -> tuple[str, float]:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return "", 0.0
    if value <= 0:
        return "", 0.0
    return f"до {int(value):,} ₽".replace(",", " "), value


def _parse_want(want: dict) -> FreelanceOrder | None:
    try:
        if want.get("status") != "active":
            return None
        project_id = int(want["id"])
        created = datetime.strptime(str(want["date_create"]), "%Y-%m-%d %H:%M:%S")
        category = str(want.get("category_id") or "")
        price_text, price_value = _price(want.get("priceLimit"))
        return FreelanceOrder(
            order_id=KWORK_ID_OFFSET + project_id,
            site=KWORK_SITE,
            title=html.unescape(str(want.get("name") or "")).strip(),
            body=_clean_body(str(want.get("description") or "")),
            price_text=price_text,
            price_value=price_value,
            categories=_dev_codes(category),
            published_at=created.replace(tzinfo=MSK).astimezone(timezone.utc),
            url=kwork_url(project_id),
        )
    except (KeyError, TypeError, ValueError, OverflowError):
        # содержимое заказа в лог не пишем: там могут быть контакты заказчика
        logging.warning("[KWORK] не удалось разобрать проект")
        return None


def parse_page_counted(page_html: str) -> tuple[list[FreelanceOrder], int]:
    """(заказы страницы, сколько проектов было на ней вообще). Нет window.stateData/списка
    проектов (капча, смена вёрстки) - исключение: источник сломан, а не пуст."""
    start = page_html.find(_STATE_MARK)
    if start < 0:
        raise RuntimeError("Kwork: в странице нет window.stateData (блокировка или смена вёрстки)")
    try:
        state, _ = json.JSONDecoder().raw_decode(page_html[start + len(_STATE_MARK):])
        wants = state["wantsListData"]["wants"]
    except (ValueError, KeyError, TypeError) as e:
        raise RuntimeError(f"Kwork: не удалось прочитать список проектов ({type(e).__name__})") from e
    if not isinstance(wants, list):
        raise RuntimeError("Kwork: список проектов не массив")
    orders = [o for o in (_parse_want(w) for w in wants if isinstance(w, dict)) if o]
    return orders, len(wants)


def parse_page(page_html: str) -> list[FreelanceOrder]:
    return parse_page_counted(page_html)[0]


def _fetch_page(page: int) -> tuple[list[FreelanceOrder], int]:
    resp = requests.get(KWORK_URL, params={"c": DEV_CATEGORY, "page": page},
                        headers=HEADERS, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return parse_page_counted(resp.text)


def fetch_kwork_orders(pages: int = KWORK_PAGES) -> list[FreelanceOrder]:
    """Свежие заказы Kwork (первые страницы категории). Сбой первой страницы - исключение;
    сбой следующих не теряет уже собранное (скан просто короче)."""
    orders: dict[int, FreelanceOrder] = {}
    for page in range(1, max(1, pages) + 1):
        if page > 1:
            time.sleep(PAGE_PAUSE_SEC)
        try:
            found, raw_count = _fetch_page(page)
        except (requests.RequestException, RuntimeError) as e:
            if not orders:
                raise
            logging.warning(f"[KWORK] страница {page} не получена ({type(e).__name__}), "
                            f"собранные {len(orders)} заказов сохраняем")
            break
        if raw_count == 0 and page > 1:
            break   # проекты кончились; страница из одних неактивных/неразобранных - не конец
        for o in found:
            orders[o.order_id] = o
    return list(orders.values())


if __name__ == "__main__":
    from freelance.filters import is_candidate

    logging.basicConfig(level=logging.INFO)
    got = fetch_kwork_orders()
    passed = [o for o in got if is_candidate(o)]
    print(f"Проектов получено: {len(got)}, прошли префильтр: {len(passed)}")
    for o in sorted(passed, key=lambda o: o.published_at, reverse=True)[:20]:
        t = o.published_at.astimezone().strftime("%d.%m %H:%M")
        print(f"{o.order_id} {t} | {o.price_text or '-'} | {o.title[:70]}")
