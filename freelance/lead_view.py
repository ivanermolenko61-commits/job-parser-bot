"""Ф08: отображение лидов - тексты и описание кнопок (чистая логика без aiogram, БД и сети).

Карточка лида строится ТОЛЬКО из строки БД и списка событий (database.freelance_lead_get /
freelance_lead_events), поэтому работает после удаления сообщения по TTL и после рестарта. Кнопки описаны
как Btn(text, data, url): бот превращает их в InlineKeyboardButton. Формат callback_data (до 64 байт):
  карточка заказа: fl_save:<id> fl_apply:<id> fl_np:<id> fl_back:<id> fl_lead:<id>
  карточка лида:   fl_st:<id>:<этап> fl_lost:<id> fl_lr:<id>:<n> fl_rm:<id> fl_rc:<id>:<срок> fl_undo:<id> fl_card:<id>
"""
import html
from datetime import datetime, timezone
from typing import NamedTuple
from zoneinfo import ZoneInfo

from freelance import journal as jr

MSK = ZoneInfo("Europe/Moscow")
INBOX_EMPTY = "Входящих нет: сохранённых лидов и необработанных интересных заказов нет"

STAGE_LABEL = {
    jr.LEAD_SAVED: "💾 Сохранён",
    jr.LEAD_APPLIED: "📨 Откликнулся",
    jr.LEAD_DISCUSSING: "💬 Обсуждаем",
    jr.LEAD_AGREED: "🤝 Договорились",
    jr.LEAD_DONE: "✅ Выполнен",
    jr.LEAD_LOST: "❌ Отказ",
}
NO_LEAD_LABEL = "🆕 Не обработан"
# Следующие этапы, доступные кнопками с карточки лида (отказ - отдельной кнопкой с причиной)
NEXT_STAGES = {
    None: (jr.LEAD_SAVED, jr.LEAD_APPLIED),
    jr.LEAD_SAVED: (jr.LEAD_APPLIED, jr.LEAD_DISCUSSING),
    jr.LEAD_APPLIED: (jr.LEAD_DISCUSSING, jr.LEAD_AGREED),
    jr.LEAD_DISCUSSING: (jr.LEAD_AGREED,),
    jr.LEAD_AGREED: (jr.LEAD_DONE,),
    jr.LEAD_DONE: (),
    jr.LEAD_LOST: (),
}
STAGE_BUTTON = {
    jr.LEAD_SAVED: "💾 Сохранить",
    jr.LEAD_APPLIED: "📨 Откликнулся",
    jr.LEAD_DISCUSSING: "💬 Обсуждаем",
    jr.LEAD_AGREED: "🤝 Договорились",
    jr.LEAD_DONE: "✅ Выполнен",
}
REMIND_BUTTONS = (("tomorrow", "Завтра 10:00 МСК"), ("3days", "Через 3 дня"), ("none", "Без напоминания"))
EVENT_LIMIT = 3


class Btn(NamedTuple):
    text: str
    data: str | None = None
    url: str | None = None


def _esc(value) -> str:
    return html.escape(str(value or ""))


def _parse_sql(value: str | None) -> datetime | None:
    """UTC 'YYYY-MM-DD HH:MM:SS' (формат БД) -> aware datetime; мусор - None."""
    if not value:
        return None
    try:
        return datetime.strptime(str(value)[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def fmt_when(value: str | None, now: datetime | None = None) -> str:
    """Срок/время по МСК: «сегодня 10:00», «завтра 10:00», «19.11 10:00»; прошедший срок - с пометкой."""
    moment = _parse_sql(value)
    if moment is None:
        return ""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    local, today = moment.astimezone(MSK), now.astimezone(MSK).date()
    days = (local.date() - today).days
    clock = local.strftime("%H:%M")
    if days == 0:
        text = f"сегодня {clock}"
    elif days == 1:
        text = f"завтра {clock}"
    else:
        text = local.strftime("%d.%m %H:%M")
    return f"просрочено, было {text}" if moment <= now else text


def _title_link(row: dict, limit: int = 70) -> str:
    title = _esc((row.get("title") or f"Заказ {row.get('order_id')}")[:limit])
    url = row.get("url")
    return f'<a href="{_esc(url)}">{title}</a>' if url else title


def stage_label(lead: str | None) -> str:
    return STAGE_LABEL.get(lead, NO_LEAD_LABEL) if lead else NO_LEAD_LABEL


def money(value) -> str:
    try:
        return f"{int(round(float(value))):,}".replace(",", " ") + " ₽"
    except (TypeError, ValueError):
        return ""


def can_undo(events: list[dict]) -> bool:
    """Есть ли что отменять: хотя бы одно событие не отменено и не само отмена."""
    return any(not e.get("undone") and e.get("kind") != "undo" for e in events or [])


def event_line(ev: dict) -> str:
    kind = ev.get("kind")
    if kind == "stage":
        text = f"→ {stage_label(ev.get('to_lead'))}"
        if ev.get("to_lead") == jr.LEAD_LOST and ev.get("reason"):
            text += f" ({_esc(ev['reason'])})"
    elif kind == "next":
        text = f"действие: {_esc(ev.get('reason')) or 'убрано'}"
    elif kind == "remind":
        text = "срок напоминания изменён"
    else:
        what = {"stage": "этапа", "next": "действия", "remind": "срока"}.get(ev.get("reason"), "")
        text = f"↩️ отмена {what}".strip()
    when = _parse_sql(ev.get("at"))
    stamp = when.astimezone(MSK).strftime("%d.%m %H:%M") if when else ""
    if ev.get("undone"):
        text = f"<s>{text}</s>"
    return f"{stamp} {text}".strip()


def lead_card_text(lead: dict, events: list[dict], prev_price: float | None = None,
                   now: datetime | None = None) -> str:
    """Текст карточки лида /lead_<id>: этап, следующее действие, срок, цена, последние события.
    events - новые первыми; показываются EVENT_LIMIT последних."""
    stage = lead.get("lead")
    oid = lead.get("order_id")
    lines = [f"📋 <b>Лид {oid}</b>: {_title_link(lead, 90)}"]
    head = stage_label(stage)
    if stage == jr.LEAD_LOST and lead.get("lead_reason"):
        head += f" — {_esc(lead['lead_reason'])}"
    if lead.get("fit") is not None:
        head += f" · оценка {lead['fit']}/10"
    lines.append(head)
    if stage in jr.LEAD_ACTIVE:
        lines.append(f"Дальше: {_esc(lead.get('next_action')) or 'не задано'}   (/next {oid} текст)")
        when = fmt_when(lead.get("remind_at"), now)
        lines.append(f"⏰ {when}" if when else "⏰ без напоминания")
    if stage in jr.LEAD_PRICE_STAGES:
        if lead.get("deal_price"):
            lines.append(f"💰 {money(lead['deal_price'])}")
        elif prev_price:
            lines.append(f"💰 цена не указана (было {money(prev_price)})")
        else:
            lines.append("💰 цена не указана")
    if lead.get("card_deleted_at"):
        lines.append("Сообщение с заказом удалено по сроку, данные сохранены.")
    shown = (events or [])[:EVENT_LIMIT]
    if shown:
        lines.append("\nИстория:")
        lines.extend(f"• {event_line(e)}" for e in shown)
    return "\n".join(lines)


def lead_card_buttons(lead: dict, events: list[dict], *, undo_available: bool | None = None) -> list[list[Btn]]:
    """Кнопки карточки лида: следующие этапы, отказ с причиной, напоминание, отмена последнего."""
    oid, stage = lead["order_id"], lead.get("lead")
    rows: list[list[Btn]] = []
    forward = [Btn(STAGE_BUTTON[s], f"fl_st:{oid}:{s}") for s in NEXT_STAGES.get(stage, ())]
    if forward:
        rows.append(forward)
    tail = []
    if stage is None or stage in jr.LEAD_ACTIVE:
        tail.append(Btn("❌ Отказ", f"fl_lost:{oid}"))   # с карточки лида отказ всегда с причиной
    if stage in jr.LEAD_ACTIVE:
        tail.append(Btn("⏰ Напомнить", f"fl_rm:{oid}"))
    if tail:
        rows.append(tail)
    bottom = []
    if (can_undo(events) if undo_available is None else undo_available):
        bottom.append(Btn("↩️ Отменить последнее", f"fl_undo:{oid}"))
    if lead.get("url"):
        bottom.append(Btn("🔗 Открыть", url=lead["url"]))
    if bottom:
        rows.append(bottom)
    return rows


def lost_reason_buttons(order_id: int) -> list[list[Btn]]:
    """Выбор причины отказа: по два в ряд, номер причины - индекс в journal.LOST_REASONS."""
    buttons = [Btn(reason.capitalize(), f"fl_lr:{order_id}:{i}") for i, reason in enumerate(jr.LOST_REASONS)]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    rows.append([Btn("◀️ Назад", f"fl_card:{order_id}")])
    return rows


def remind_buttons(order_id: int) -> list[list[Btn]]:
    rows = [[Btn(text, f"fl_rc:{order_id}:{choice}")] for choice, text in REMIND_BUTTONS]
    rows.append([Btn("◀️ Назад", f"fl_card:{order_id}")])
    return rows



def order_card_buttons(order_id: int, url: str | None, lead: str | None = None, detailed: bool = False,
                       actions: bool = True) -> list[list[Btn]]:
    """Кнопки карточки заказа в чате. Без решения: «Сохранить / Откликнулся / Не подходит»; с решением -
    кнопка карточки лида (и быстрый «Откликнулся» у сохранённого). actions=False - закрытый заказ."""
    more = Btn("🔼 Свернуть", f"fl_less:{order_id}") if detailed else Btn("ℹ️ Подробнее", f"fl_more:{order_id}")
    top = [Btn("🔗 Открыть", url=url), more] if url else [more]
    rows = [top]
    if not actions or lead == jr.LEAD_LOST:
        return rows
    if lead is None:
        rows.append([Btn("💾 Сохранить", f"fl_save:{order_id}"), Btn("📨 Откликнулся", f"fl_apply:{order_id}"),
                     Btn("❌ Не подходит", f"fl_np:{order_id}")])
    else:
        row = []
        if lead == jr.LEAD_SAVED:
            row.append(Btn("📨 Откликнулся", f"fl_apply:{order_id}"))
        row.append(Btn(f"📋 Лид · {stage_label(lead)}", f"fl_lead:{order_id}"))
        rows.append(row)
    return rows


def collapsed_text(row: dict) -> str:
    """Свёрнутая строка после «Не подходит» на новой карточке (удаляется по TTL, как обычная карточка)."""
    title = _esc((row.get("title") or f"Заказ {row.get('order_id')}")[:60])
    return f"❌ Не подходит: {title}"


def collapsed_buttons(order_id: int) -> list[list[Btn]]:
    return [[Btn("↩️ Вернуть", f"fl_back:{order_id}")]]


def _lead_line(row: dict, now: datetime | None, with_action: bool = True) -> str:
    text = f"• {_title_link(row, 60)} /lead_{row['order_id']}"
    if with_action and row.get("lead") in jr.LEAD_ACTIVE:
        when = fmt_when(row.get("remind_at"), now)
        extra = " · ".join(x for x in (_esc(row.get("next_action")), f"⏰ {when}" if when else "") if x)
        if extra:
            text += f"\n   {extra}"
    elif row.get("lead") == jr.LEAD_LOST and row.get("lead_reason"):
        text += f"\n   {STAGE_LABEL[jr.LEAD_LOST]}: {_esc(row['lead_reason'])}"
    elif row.get("lead") == jr.LEAD_DONE:
        text += f"\n   {STAGE_LABEL[jr.LEAD_DONE]}"
    return text


def _unprocessed_line(row: dict) -> str:
    fit = f"{row['fit']}/10 · " if row.get("fit") is not None else ""
    return f"• {fit}{_title_link(row, 60)} /lead_{row['order_id']}"


def inbox_text(active: list[dict], unprocessed: list[dict], closed: list[dict],
               now: datetime | None = None) -> str:
    """«📥 Входящие»: активные лиды по этапам (с действием и сроком), необработанные интересные,
    закрытые за 7 дней (со ссылкой на карточку для исправления). Пусто - INBOX_EMPTY."""
    if not (active or unprocessed or closed):
        return INBOX_EMPTY
    blocks = ["📥 <b>Входящие</b>"]
    if active:
        for stage in jr.LEAD_ACTIVE:
            group = [r for r in active if r.get("lead") == stage]
            if group:
                blocks.append(f"\n<b>{STAGE_LABEL[stage]}</b> ({len(group)})")
                blocks.extend(_lead_line(r, now) for r in group)
    else:
        blocks.append("\nАктивных лидов нет.")
    if unprocessed:
        blocks.append(f"\n<b>Необработанные интересные</b> ({len(unprocessed)})")
        blocks.extend(_unprocessed_line(r) for r in unprocessed)
    if closed:
        blocks.append(f"\n<b>Закрытые за 7 дней</b> ({len(closed)})")
        blocks.extend(_lead_line(r, now, with_action=False) for r in closed)
    return "\n".join(blocks)


def offers_text(rows: list[dict]) -> str:
    """Ежедневная подборка необработанных интересных заказов (до 10)."""
    lines = [f"📥 <b>Необработанные интересные</b> ({len(rows)})",
             "Решите по каждому: /lead_номер откроет карточку с кнопками.\n"]
    lines.extend(_unprocessed_line(r) for r in rows)
    return "\n".join(lines)


def reminder_text(row: dict) -> str:
    action = _esc(row.get("next_action")) or "проверить лид"
    return (f"⏰ <b>Напоминание</b>: {_title_link(row, 80)}\n"
            f"{stage_label(row.get('lead'))} · {action}\n/lead_{row['order_id']}")


def reminder_buttons(order_id: int) -> list[list[Btn]]:
    return [[Btn("📋 Лид", f"fl_card:{order_id}"), Btn("⏰ Отложить", f"fl_rm:{order_id}")]]

