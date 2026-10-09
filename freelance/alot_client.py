"""Клиент неофициального API alot.pro (агрегатор фриланс-заказов).

POST https://alot.pro/api/v2/  {"action": "getProjects", "ids": ["...", ...]}
Авторизация не нужна, до 100 id за запрос. id идут подряд, новые заказы —
с большими id, около половины id пустые. Запрос за пределами границы
возвращает пустой items.
"""
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

from config import ALOT_SEED_ID

API_URL = "https://alot.pro/api/v2/"
BATCH_SIZE = 100
REQUEST_TIMEOUT = 20
REQUEST_PAUSE_SEC = 0.7
RETRIES = 3
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Content-Type": "application/json",
    "Accept": "application/json",
}

# Красивые имена бирж
SITE_NAMES = {
    "youdoru": "YouDo", "kworkru": "Kwork", "flru": "FL.ru",
    "freelanceru": "Freelance.ru", "workzillaru": "Workzilla",
    "taskpayru": "TaskPay", "advegocom": "Advego", "etxtru": "eTXT",
    "turbotextru": "TurboText", "vkcom": "VK", "lclancerru": "LClancer",
    "hhru": "hh.ru", "trudvsemru": "Работа в России", "remotejobru": "RemoteJob",
    "getmatchru": "Getmatch", "geekjobru": "GeekJob", "habrcareer": "Хабр Карьера",
    "workspaceru": "Workspace", "joblabru": "JobLab", "superjobru": "SuperJob",
    "kadrofru": "Kadrof",
}


def site_name(site: str) -> str:
    return SITE_NAMES.get(site, site)


@dataclass
class FreelanceOrder:
    order_id: int
    site: str
    title: str
    body: str
    price_text: str
    price_value: float
    categories: list[str]
    published_at: datetime  # aware, UTC
    url: str
    is_suspicious: bool = False


@dataclass
class ScanResult:
    orders: list[FreelanceOrder] = field(default_factory=list)
    max_id: int = 0       # наибольший непустой id в просмотренном окне
    raw_count: int = 0    # сколько непустых заказов вернул API (для health)
    partial: bool = False # скан оборван сбоем API: заказы до сбоя сохранены, дальше - в следующем цикле


_session = requests.Session()
_session.headers.update(HEADERS)


def _fetch_items(ids: list[int]) -> list[dict]:
    """Один запрос getProjects. Ретраи с backoff; при неудаче бросает исключение."""
    payload = {"action": "getProjects", "ids": [str(i) for i in ids]}
    last_exc: Exception | None = None
    for attempt in range(RETRIES):
        try:
            resp = _session.post(API_URL, json=payload, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
            if data.get("responseCode", 0) != 0:
                raise RuntimeError(f"alot.pro responseCode={data.get('responseCode')}")
            items = data.get("items")
            if not isinstance(items, list):
                raise RuntimeError("alot.pro: в ответе нет items")
            return items
        except (requests.RequestException, ValueError, RuntimeError) as e:
            last_exc = e
            logging.warning(f"[ALOT] попытка {attempt + 1}/{RETRIES}: {type(e).__name__}: {e}")
            if attempt < RETRIES - 1:
                time.sleep(2 ** attempt * 2)
    assert last_exc is not None
    raise last_exc


def _fetch_range(start: int, count: int = BATCH_SIZE) -> list[dict]:
    # Лимит API — 100 id за запрос; больший диапазон режем на пачки
    items: list[dict] = []
    for s in range(start, start + count, BATCH_SIZE):
        n = min(BATCH_SIZE, start + count - s)
        if s != start:
            time.sleep(REQUEST_PAUSE_SEC)
        items.extend(_fetch_items(list(range(s, s + n))))
    return items


def _clean_body(html_text: str) -> str:
    text = BeautifulSoup(html_text or "", "html.parser").get_text("\n")
    return re.sub(r"\n\s*\n+", "\n", text).strip()


def _parse_item(item: dict) -> FreelanceOrder | None:
    try:
        order_id = int(item["internalId"])
        ts = int(item.get("date") or 0) / 1000
        published = datetime.fromtimestamp(ts, tz=timezone.utc)
        try:
            price_value = float(item.get("internalPrice") or 0)
        except (TypeError, ValueError):
            price_value = 0.0
        return FreelanceOrder(
            order_id=order_id,
            site=str(item.get("site") or ""),
            title=str(item.get("title") or "").strip(),
            body=_clean_body(item.get("body") or ""),
            price_text=str(item.get("price") or "").strip(),
            price_value=price_value,
            categories=list(item.get("alotCategories") or []),
            published_at=published,
            url=str(item.get("url") or f"https://alot.pro/projects/{order_id}/"),
            is_suspicious=bool(item.get("reports")),
        )
    except (KeyError, TypeError, ValueError, OSError, OverflowError):
        logging.warning(f"[ALOT] не удалось разобрать заказ: {str(item)[:120]}")
        return None


def find_upper_bound(seed: int = ALOT_SEED_ID) -> int:
    """Находит наибольший существующий id: экспоненциальный, затем бинарный поиск."""
    # Якорь должен давать данные; если нет (ушли за границу) — идём назад
    lo = seed
    for _ in range(20):
        if _fetch_range(lo):
            break
        time.sleep(REQUEST_PAUSE_SEC)
        lo -= 5000
    else:
        raise RuntimeError("alot.pro: не нашли непустое окно рядом с ALOT_SEED_ID")

    # Экспоненциальный рост: пока окно [lo+step, lo+step+99] непустое
    step = 256
    hi = None
    while hi is None:
        time.sleep(REQUEST_PAUSE_SEC)
        if _fetch_range(lo + step):
            lo += step
            step *= 2
        else:
            hi = lo + step
    # Бинарный поиск: окно у lo непустое, окно у hi пустое
    while hi - lo > BATCH_SIZE:
        mid = (lo + hi) // 2
        time.sleep(REQUEST_PAUSE_SEC)
        if _fetch_range(mid):
            lo = mid
        else:
            hi = mid
    # Максимальный id: смотрим от lo до hi (+ хвост пачки)
    time.sleep(REQUEST_PAUSE_SEC)
    items = _fetch_range(lo, hi - lo + BATCH_SIZE)
    ids = [int(i["internalId"]) for i in items if "internalId" in i]
    return max(ids) if ids else lo


def fetch_new_orders(last_id: int, max_ahead: int = 300, look_back: int = 100) -> ScanResult:
    """Сканирует окно [last_id - look_back, last_id + max_ahead] пачками по 100.

    Отступ назад подбирает заказы, появившиеся с задержкой. Останавливается,
    когда три пачки подряд выше last_id пустые (дошли до границы).
    Сбой пачки (alot.pro при перегрузке отвечает responseCode=8) не выбрасывает уже
    собранное: скан обрывается с partial=True, max_id - по собранному, и следующий цикл
    продолжит с этого места. Если не собрано ничего - исключение, как раньше.
    """
    result = ScanResult()
    start = max(1, last_id - look_back)
    end = last_id + max_ahead
    first = True
    empty_streak = 0  # пустых пачек подряд выше last_id
    while start <= end:
        if not first:
            time.sleep(REQUEST_PAUSE_SEC)
        first = False
        try:
            items = _fetch_items(list(range(start, start + BATCH_SIZE)))
        except (requests.RequestException, ValueError, RuntimeError) as e:
            if not result.orders:
                raise
            logging.warning(f"[ALOT] скан оборван на id {start} ({type(e).__name__}), "
                            f"собранные {len(result.orders)} заказов сохраняем")
            result.partial = True
            break
        parsed = [o for o in (_parse_item(i) for i in items) if o]
        if parsed:
            empty_streak = 0
            result.orders.extend(parsed)
            result.raw_count += len(parsed)
            result.max_id = max(result.max_id, max(o.order_id for o in parsed))
        elif start > last_id:
            # Граница: пустая пачка и ещё две следом (на случай дыры в id)
            empty_streak += 1
            if empty_streak >= 3:
                break
        start += BATCH_SIZE
    return result


if __name__ == "__main__":
    # Ручной прогон: граница и ~20 последних заказов после префильтра
    from freelance.filters import is_candidate

    logging.basicConfig(level=logging.INFO)
    top = find_upper_bound()
    print(f"Верхняя граница id: {top}")
    scan = fetch_new_orders(top - 300, max_ahead=400)
    print(f"Непустых заказов в окне: {scan.raw_count}, max_id={scan.max_id}")
    passed = [o for o in scan.orders if is_candidate(o)]
    passed.sort(key=lambda o: o.order_id, reverse=True)
    print(f"Прошли префильтр: {len(passed)}")
    for o in passed[:20]:
        t = o.published_at.astimezone().strftime("%d.%m %H:%M")
        print(f"{o.order_id} [{site_name(o.site)}] {t} | {o.price_text or '-'} | "
              f"{','.join(o.categories)} | {o.title[:70]}")
