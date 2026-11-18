"""Ф08: действия с лидом (этапы, следующее действие, напоминание, отмена) поверх БД и метки триажа.

Единственное место, где состояние лида и разметка триажа меняются вместе: каждое действие и каждая отмена
пересчитывают метку заказа по итоговому состоянию (правило - journal.lead_label). Бот (кнопки, /lead_<id>,
/next) вызывает только эти функции. Заказчикам ничего не отправляется: здесь нет сетевых вызовов.
"""
from datetime import datetime, timedelta, timezone

import database
from freelance import journal as jr
from freelance import pipeline

# Варианты срока напоминания в кнопке «⏰»
REMIND_TOMORROW, REMIND_3DAYS, REMIND_NONE = "tomorrow", "3days", "none"
REMIND_CHOICES = (REMIND_TOMORROW, REMIND_3DAYS, REMIND_NONE)
REMIND_HOUR_MSK = 10


def _utc(now: datetime | None) -> datetime:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc)


def to_sql(moment: datetime | None) -> str | None:
    """Момент времени -> строка БД (UTC 'YYYY-MM-DD HH:MM:SS')."""
    return None if moment is None else moment.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def remind_for_choice(choice: str, now: datetime | None = None) -> str | None:
    """Срок напоминания по кнопке: завтра 10:00 МСК / через 3 дня / без напоминания (None)."""
    now = _utc(now)
    if choice == REMIND_TOMORROW:
        msk_day = now.astimezone(pipeline.MSK).date() + timedelta(days=1)
        return to_sql(datetime(msk_day.year, msk_day.month, msk_day.day, REMIND_HOUR_MSK, tzinfo=pipeline.MSK))
    if choice == REMIND_3DAYS:
        return to_sql(now + timedelta(days=3))
    if choice == REMIND_NONE:
        return None
    raise ValueError(f"неизвестный срок напоминания: {choice}")


def default_next(lead: str, now: datetime | None = None) -> tuple[str | None, str | None]:
    """Следующее действие и срок по умолчанию для этапа; у done/lost - (None, None)."""
    spec = jr.LEAD_DEFAULT_NEXT.get(lead)
    if spec is None:
        return None, None
    text, days = spec
    return text, to_sql(_utc(now) + timedelta(days=days))


def _relabel(order_id: int, res: dict) -> dict:
    """Метка триажа по состоянию ДО и ПОСЛЕ действия (при отмене - так же, по итоговому состоянию)."""
    if res.get("ok") and res.get("changed"):
        before, after = res["before"], res["after"]
        res["label"] = pipeline.retarget_triage_label(
            order_id, jr.lead_label(before.get("lead"), before.get("lead_reason")),
            jr.lead_label(after.get("lead"), after.get("lead_reason")))
    return res


def set_stage(order_id: int, lead: str, reason: str | None = None, now: datetime | None = None) -> dict:
    """Перевод на этап: действие и срок по умолчанию (journal.LEAD_DEFAULT_NEXT), событие, метка триажа.
    Неизвестный этап или причина - ValueError, ничего не меняется. Результат: см. database.freelance_lead_stage
    плюс label ('good'/'bad'/'miss'/'removed'/None)."""
    now = _utc(now)
    text, remind = default_next(lead, now)
    res = database.freelance_lead_stage(order_id, lead, reason, next_action=text, remind_at=remind, now=to_sql(now))
    return _relabel(order_id, res)


def undo(order_id: int, now: datetime | None = None) -> dict:
    """«↩️ Отменить последнее» / «↩️ Вернуть»: возврат состояния и пересчёт метки. Нечего отменять -
    {'ok': False, 'reason': 'nothing'}."""
    return _relabel(order_id, database.freelance_lead_undo(order_id, now=to_sql(_utc(now))))


def set_next(order_id: int, text: str | None, now: datetime | None = None) -> dict:
    """Своё следующее действие (/next <id> <текст>); пустой текст убирает действие."""
    return database.freelance_lead_next(order_id, text, now=to_sql(_utc(now)))


def set_remind(order_id: int, choice: str, now: datetime | None = None) -> dict:
    """Срок напоминания по кнопке «⏰» (REMIND_*)."""
    return database.freelance_lead_remind(order_id, remind_for_choice(choice, now), now=to_sql(_utc(now)))
