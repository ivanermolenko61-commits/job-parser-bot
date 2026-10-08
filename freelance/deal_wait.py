"""Ожидание ответа «За сколько договорились?» после кнопки «Взял» (чистая логика без aiogram).

Вопрос бота хранится как (chat_id, message_id вопроса) -> (order_id, время).
Ответ сопоставляется по reply_to_message; без reply допустим, только если в чате
ровно одно живое ожидание. Просроченные (DEAL_TTL_SEC) чистятся.
"""
import re
import time

DEAL_TTL_SEC = 30 * 60
DEAL_MAX = 10_000_000

# (chat_id, question_message_id) -> (order_id, created_ts)
Pending = dict[tuple[int, int], tuple[int, float]]


def parse_deal(text: str) -> int | None:
    """Число в рублях из ответа («5000», «5 000 ₽», «5000р»); None - не число."""
    cleaned = re.sub(r"(?i)(руб\.?|р\.?|₽)", "", text or "")
    cleaned = cleaned.replace(" ", "").replace(chr(0xA0), "")
    if not re.fullmatch(r"[0-9]+", cleaned):
        return None
    value = int(cleaned)
    return value if 0 < value <= DEAL_MAX else None


def purge(pending: Pending, now: float | None = None, ttl: float = DEAL_TTL_SEC) -> None:
    """Удаляет просроченные ожидания."""
    now = time.time() if now is None else now
    for key in [k for k, (_, ts) in pending.items() if now - ts > ttl]:
        pending.pop(key, None)


def add(pending: Pending, chat_id: int, question_id: int, order_id: int,
        now: float | None = None) -> None:
    pending[(chat_id, question_id)] = (order_id, time.time() if now is None else now)


def drop_order(pending: Pending, order_id: int) -> None:
    """«Не интересно»/skip заказа снимает его ожидание."""
    for key in [k for k, (oid, _) in pending.items() if oid == order_id]:
        pending.pop(key, None)


def ambiguous(pending: Pending, chat_id: int, reply_to_id: int | None,
              now: float | None = None, ttl: float = DEAL_TTL_SEC) -> bool:
    """Ответ без reply, а живых ожиданий в чате два и больше: нужно попросить reply."""
    purge(pending, now, ttl)
    return reply_to_id is None and sum(1 for k in pending if k[0] == chat_id) >= 2


def find(pending: Pending, chat_id: int, reply_to_id: int | None,
         now: float | None = None, ttl: float = DEAL_TTL_SEC) -> tuple[int, int] | None:
    """Ключ ожидания для ответа или None. Сначала чистит просроченные."""
    purge(pending, now, ttl)
    if reply_to_id is not None and (chat_id, reply_to_id) in pending:
        return (chat_id, reply_to_id)
    if reply_to_id is None:
        mine = [k for k in pending if k[0] == chat_id]
        if len(mine) == 1:
            return mine[0]
    return None
