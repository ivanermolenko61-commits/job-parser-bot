"""Источники фриланс-заказов и их включение/выключение.

Три независимых источника: alot.pro (агрегатор), RSS FL.ru и список проектов Kwork.
alot.pro сам агрегирует FL.ru и Kwork, поэтому, пока он работает, прямые источники
дублируют заказы и тратят квоту AI. Включать и выключать источники можно из бота
(💼 Фриланс → 🔌 Источники); выбор хранится в БД (kv) и переживает перезапуск.
Переменные окружения ALOT_ENABLED / FL_RSS_ENABLED / KWORK_ENABLED = 0 выключают
источник насовсем (из бота он тогда не включается).
"""
import logging

from config import ALOT_ENABLED, FL_RSS_ENABLED, KWORK_ENABLED
from database import kv_get, kv_set

# order_id заказов FL.ru и Kwork = смещение + id на самой бирже: не пересекаются с id alot.pro
# (~1.7·10⁷) и друг с другом в общей таблице freelance_orders
FL_ID_OFFSET = 10 ** 10
KWORK_ID_OFFSET = 2 * 10 ** 10
FEED_ID_MIN = FL_ID_OFFSET

ALOT, FLRU, KWORK = "alot", "flru", "kwork"
ALL_SOURCES = (ALOT, FLRU, KWORK)
TITLES = {ALOT: "alot.pro", FLRU: "FL.ru", KWORK: "Kwork"}
_ENV_ON = {ALOT: ALOT_ENABLED, FLRU: FL_RSS_ENABLED, KWORK: KWORK_ENABLED}
_OFF_KEY = "freelance_sources_off"


def is_feed_id(order_id: int) -> bool:
    """Заказ пришёл напрямую с FL.ru/Kwork (а не из alot.pro) - его нельзя дозапросить по id."""
    return order_id >= FEED_ID_MIN


def _disabled_in_db() -> set[str]:
    raw = kv_get(_OFF_KEY, "") or ""
    return {s for s in raw.split(",") if s in ALL_SOURCES}


def forced_off(name: str) -> bool:
    """Выключен переменной окружения: из бота не включается."""
    return not _ENV_ON[name]


def is_enabled(name: str) -> bool:
    if forced_off(name):
        return False
    try:
        return name not in _disabled_in_db()
    except Exception:
        # БД не прочиталась: не гасим заказы из-за разового сбоя чтения
        logging.exception("[SOURCES] не удалось прочитать настройки источников")
        return True


def set_enabled(name: str, on: bool) -> bool:
    """Включает/выключает источник. False - нельзя (выключен переменной окружения)."""
    if name not in ALL_SOURCES or (on and forced_off(name)):
        return False
    off = _disabled_in_db()
    off.discard(name) if on else off.add(name)
    kv_set(_OFF_KEY, ",".join(s for s in ALL_SOURCES if s in off))
    return True


def states() -> list[tuple[str, str, bool, bool]]:
    """[(имя, название, включён, выключен окружением)] для меню и статуса."""
    return [(n, TITLES[n], is_enabled(n), forced_off(n)) for n in ALL_SOURCES]
