"""Независимые источники. Выбор в меню сохраняется в БД; флаг среды = 0 имеет приоритет."""
import logging

from config import CONTRACTIS_ENABLED, KADROF_ENABLED
from config import ALOT_ENABLED, FL_RSS_ENABLED, FREELANCE_RU_ENABLED, KWORK_ENABLED, WEBLANCER_ENABLED
from database import kv_get, kv_set

# order_id заказов FL.ru и Kwork = смещение + id на самой бирже: не пересекаются с id alot.pro
# (~1.7·10⁷) и друг с другом в общей таблице freelance_orders
FL_ID_OFFSET = 10 ** 10
KWORK_ID_OFFSET = 2 * 10 ** 10
FREELANCE_RU_ID_OFFSET = 3 * 10 ** 10
WEBLANCER_ID_OFFSET = 4 * 10 ** 10
CONTRACTIS_ID_OFFSET = 5 * 10 ** 10
KADROF_ID_OFFSET = 6 * 10 ** 10
MANUAL_ID_OFFSET = 1 << 40
FEED_ID_MIN = FL_ID_OFFSET

ALOT, FLRU, KWORK, FREELANCE_RU, WEBLANCER = "alot", "flru", "kwork", "freelanceru", "weblancer"
CONTRACTIS, KADROF = "contractis", "kadrof"
ALL_SOURCES = (ALOT, FLRU, KWORK, FREELANCE_RU, WEBLANCER, CONTRACTIS, KADROF)
TITLES = {ALOT: "alot.pro", FLRU: "FL.ru", KWORK: "Kwork", FREELANCE_RU: "Freelance.ru", WEBLANCER: "Weblancer",
          CONTRACTIS: "CONTRACTIS", KADROF: "Kadrof"}
_ENV_ON = {ALOT: ALOT_ENABLED, FLRU: FL_RSS_ENABLED, KWORK: KWORK_ENABLED,
           FREELANCE_RU: FREELANCE_RU_ENABLED, WEBLANCER: WEBLANCER_ENABLED,
           CONTRACTIS: CONTRACTIS_ENABLED, KADROF: KADROF_ENABLED}
_OFF_KEY = "freelance_sources_off"


def is_feed_id(order_id: int) -> bool:
    """Заказ прямого источника нельзя дозапросить в alot.pro по этому ID."""
    return FEED_ID_MIN <= order_id < MANUAL_ID_OFFSET


def source_of(order_id: int) -> str:
    """Источник заказа по его order_id (диапазоны смещений)."""
    if order_id >= MANUAL_ID_OFFSET:
        return "manual"
    for source, (low, high) in _RANGES.items():
        if low <= order_id < high:
            return source
    return ALOT


_RANGES = {ALOT: (0, FL_ID_OFFSET), FLRU: (FL_ID_OFFSET, KWORK_ID_OFFSET), KWORK: (KWORK_ID_OFFSET, FREELANCE_RU_ID_OFFSET),
           FREELANCE_RU: (FREELANCE_RU_ID_OFFSET, WEBLANCER_ID_OFFSET), WEBLANCER: (WEBLANCER_ID_OFFSET, CONTRACTIS_ID_OFFSET),
           CONTRACTIS: (CONTRACTIS_ID_OFFSET, KADROF_ID_OFFSET), KADROF: (KADROF_ID_OFFSET, MANUAL_ID_OFFSET)}


def id_ranges(names) -> list[tuple[int, int]]:
    """Полуинтервалы order_id заказов указанных источников."""
    return [_RANGES[n] for n in names]


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
