"""Часы автоматических оценок и уведомлений о фриланс-заказах."""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from zoneinfo import ZoneInfo

MSK = ZoneInfo("Europe/Moscow")
OPEN_HOUR = 7
CLOSE_HOUR = 22
_automatic = ContextVar("automatic_freelance_request", default=False)


def automatic_open(now: datetime | None = None) -> bool:
    local = now.astimezone(MSK) if now is not None else datetime.now(MSK)
    return OPEN_HOUR <= local.hour < CLOSE_HOUR


@contextmanager
def automatic_requests(enabled: bool = True):
    token = _automatic.set(enabled)
    try:
        yield
    finally:
        _automatic.reset(token)


def request_allowed() -> bool:
    return not _automatic.get() or automatic_open()
