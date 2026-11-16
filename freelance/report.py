"""Воронка, задержки и потери по журналу фриланс-заказов (freelance/journal.py).

Нужен ботом (статус, /export) и инструментом tools/loss_report.py. Только чтение БД.
Три измерения не смешиваются: техническая обработка (tech), доставка (delivery), этап сделки (lead).
Задержки разделены: источник (опубликован -> бот увидел), очередь AI (увидел -> оценён),
доставка (оценён -> показан). Дата публикации alot.pro для Kwork ненадёжна (время сбора alot) -
такие заказы в задержке источника не участвуют.
"""
import csv
import html
import io
import statistics
from datetime import datetime, timezone

import database
from config import FREELANCE_MIN_FIT
from freelance import journal as jr

_COLUMNS = (
    "order_id, site, origin, title, price_text, budget, url, published_at, seen_at, assessed_at, "
    "delivered_at, tech, queue_reason, reject_reason, verdict, dup_of, delivery, card_deleted_at, "
    "stale_reason, desc_changed, lead, lead_reason, fit, category, hours, snippet, "
    "COALESCE(pub_unreliable, 0) AS pub_unreliable"
)


def load_rows(days: int = 14) -> list[dict]:
    """Строки журнала, впервые увиденные за последние days дней."""
    with database._connect() as conn:
        rows = conn.execute(
            f"SELECT {_COLUMNS} FROM freelance_orders WHERE seen_at >= datetime('now', ?) ORDER BY seen_at",
            (f"-{int(days)} days",),
        ).fetchall()
        return [dict(r) for r in rows]


def load_excluded(days: int = 14) -> dict[str, int]:
    """{площадка: сколько заказов отсеяно как вакансионные доски} за последние days дней."""
    with database._connect() as conn:
        rows = conn.execute(
            "SELECT site, SUM(n) FROM freelance_funnel_daily WHERE day >= date('now', ?) GROUP BY site",
            (f"-{int(days)} days",),
        ).fetchall()
        return {r[0]: int(r[1] or 0) for r in rows}


def _dt(value) -> datetime | None:
    """Время из БД: SQLite ('YYYY-MM-DD HH:MM:SS', UTC) или ISO с часовым поясом."""
    if not value or value == "unknown":
        return None
    try:
        dt = datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        try:
            dt = datetime.fromisoformat(str(value))
        except ValueError:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _minutes(a, b) -> float | None:
    """Минут от a до b (None, если времени нет или оно в обратном порядке дальше погрешности)."""
    start, end = _dt(a), _dt(b)
    if start is None or end is None:
        return None
    value = (end - start).total_seconds() / 60
    return value if value > -5 else None


def source_label(row: dict) -> str:
    return f"{row.get('site') or '?'}/{row.get('origin') or '?'}"


def funnel(rows: list[dict]) -> dict[str, dict]:
    """Воронка по источникам: {источник: счётчики}. discovered - строки журнала (без вакансионных досок)."""
    result: dict[str, dict] = {}
    for r in rows:
        c = result.setdefault(source_label(r), {
            "discovered": 0, "filtered": 0, "duplicate": 0, "queued": 0, "stale": 0, "legacy": 0,
            "assessed": 0, "good": 0, "low_fit": 0, "card": 0, "digest": 0, "delivery_pending": 0,
            "delivery_unknown": 0, "leads": 0})
        c["discovered"] += 1
        tech = r.get("tech") if r.get("tech") in jr.TECH_STATES else jr.LEGACY
        c[tech] += 1 if tech != jr.ASSESSED else 0
        if tech == jr.ASSESSED or r.get("fit") is not None:
            c["assessed"] += 1
            if r.get("verdict") in (jr.V_LOW, jr.V_TRIAGE):
                c["low_fit"] += 1
            else:
                c["good"] += 1
        delivery = r.get("delivery")
        if delivery == jr.D_CARD:
            c["card"] += 1
        elif delivery == jr.D_DIGEST:
            c["digest"] += 1
        elif delivery == jr.D_PENDING:
            c["delivery_pending"] += 1
        elif delivery == jr.D_UNKNOWN:
            c["delivery_unknown"] += 1
        if r.get("lead"):
            c["leads"] += 1
    return result


def _stats(values: list[float]) -> dict | None:
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    p90 = values[min(len(values) - 1, int(len(values) * 0.9))]
    return {"n": len(values), "median": statistics.median(values), "p90": p90}


def delays(rows: list[dict]) -> dict[str, dict]:
    """Задержки по источникам, минуты: source (опубликован -> увидел), ai_queue (увидел -> оценён),
    delivery (оценён -> показан). Каждая - {n, median, p90} или None."""
    groups: dict[str, dict[str, list]] = {}
    for r in rows:
        g = groups.setdefault(source_label(r), {"source": [], "ai_queue": [], "delivery": []})
        if not r.get("pub_unreliable"):
            g["source"].append(_minutes(r.get("published_at"), r.get("seen_at")))
        g["ai_queue"].append(_minutes(r.get("seen_at"), r.get("assessed_at")))
        g["delivery"].append(_minutes(r.get("assessed_at"), r.get("delivered_at")))
    return {k: {name: _stats(vals) for name, vals in g.items()} for k, g in groups.items()}


def losses(rows: list[dict], examples: int = 5) -> dict:
    """Где заказ мог потеряться: по причинам и примеры для ручной проверки.
    filtered - отсеяны правилом (по причинам); never_assessed - устарели без оценки;
    good_not_delivered - оценены хорошо, но не показаны ни карточкой, ни сводкой."""
    def sample(items):
        return [{"order_id": r["order_id"], "title": _mask(r.get("title")), "site": r.get("site")}
                for r in items[:examples]]

    filtered: dict[str, list] = {}
    for r in rows:
        if r.get("tech") == jr.FILTERED:
            filtered.setdefault((r.get("reject_reason") or "?").split(":", 1)[0], []).append(r)
    never = [r for r in rows if r.get("tech") == jr.STALE and r.get("fit") is None]
    not_delivered = [r for r in rows if r.get("tech") in (jr.ASSESSED, jr.STALE)
                     and r.get("verdict") == jr.V_OK and r.get("delivery") in (jr.D_NONE, jr.D_PENDING)]
    legacy = [r for r in rows if r.get("tech") == jr.LEGACY or r.get("delivery") == jr.D_UNKNOWN]
    queue: dict[str, int] = {}
    for r in rows:
        if r.get("tech") == jr.QUEUED:
            queue[r.get("queue_reason") or "?"] = queue.get(r.get("queue_reason") or "?", 0) + 1
    stale_reasons: dict[str, int] = {}
    for r in rows:
        if r.get("tech") == jr.STALE:
            stale_reasons[r.get("stale_reason") or "?"] = stale_reasons.get(r.get("stale_reason") or "?", 0) + 1
    return {
        "filtered": {k: {"n": len(v), "examples": sample(v)} for k, v in sorted(filtered.items(), key=lambda kv: -len(kv[1]))},
        "never_assessed": {"n": len(never), "examples": sample(never)},
        "good_not_delivered": {"n": len(not_delivered), "examples": sample(not_delivered)},
        "legacy_unknown": len(legacy),
        "queue_reasons": queue,
        "stale_reasons": stale_reasons,
    }


def _mask(text) -> str:
    from freelance.ai_scorer import mask_contacts
    return mask_contacts(str(text or ""))


def _fmt_stats(s: dict | None) -> str:
    if not s:
        return "—"
    def f(m):
        m = max(0.0, m)
        return f"{m:.0f} мин" if m < 120 else f"{m / 60:.1f} ч"
    return f"{f(s['median'])} (p90 {f(s['p90'])}, n={s['n']})"


def format_funnel(rows: list[dict], excluded: dict[str, int] | None = None, days: int = 1) -> str:
    """Текст воронки и задержек по источникам для Telegram (HTML)."""
    if not rows and not excluded:
        return "Журнал за выбранный срок пуст."
    fn, dl = funnel(rows), delays(rows)
    lines = [f"🔎 <b>Воронка за {days} дн.</b> (источник: биржа/канал доставки)"]
    if excluded:
        lines.append(f"Вакансионные доски отсеяны по площадке: {sum(excluded.values())}")
    for key in sorted(fn, key=lambda k: -fn[k]["discovered"]):
        c, d = fn[key], dl.get(key, {})
        lines.append(
            f"\n<b>{html.escape(key)}</b>: увидено {c['discovered']} → отсеяно правилом {c['filtered']}, "
            f"дубли {c['duplicate']}, устарели {c['stale']}, ждут оценки {c['queued']} → оценено {c['assessed']} "
            f"(подходят {c['good']}, ниже порога {c['low_fit']}) → карточкой {c['card']}, сводкой {c['digest']}, "
            f"ждут доставки {c['delivery_pending']}")
        lines.append(f"  задержка источника {_fmt_stats(d.get('source'))}; очередь AI {_fmt_stats(d.get('ai_queue'))}; "
                     f"доставка {_fmt_stats(d.get('delivery'))}")
    return "\n".join(lines)


EXPORT_FIELDS = (
    "order_id", "source", "published_at", "first_seen_at", "assessed_at", "delivered_at", "tech", "reason",
    "verdict", "fit", "delivery", "card_deleted_at", "lead", "lead_reason", "desc_changed", "title",
    "price_text", "url",
)


def export_csv(rows: list[dict]) -> str:
    """CSV журнала для проверки потерь. Название обезличено (телефоны, почта, токены -> метки),
    описания нет. Время - как в БД (UTC)."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(EXPORT_FIELDS)
    for r in rows:
        reason = (r.get("reject_reason") or r.get("stale_reason")
                  or (f"dup_of:{r['dup_of']}" if r.get("dup_of") else "")
                  or (f"queue:{r['queue_reason']}" if r.get("queue_reason") else ""))
        w.writerow([
            r["order_id"], source_label(r), r.get("published_at") or "", r.get("seen_at") or "",
            r.get("assessed_at") or "", r.get("delivered_at") or "", r.get("tech") or "", reason,
            r.get("verdict") or "", "" if r.get("fit") is None else r["fit"], r.get("delivery") or "",
            r.get("card_deleted_at") or "", r.get("lead") or "", r.get("lead_reason") or "",
            r.get("desc_changed") or 0, _mask(r.get("title")), r.get("price_text") or "", r.get("url") or "",
        ])
    return buf.getvalue()


def format_losses(rep: dict) -> str:
    """Текст отчёта потерь (для tools/loss_report.py)."""
    out = ["Отсеяно правилом (проверьте примеры: нет ли среди них подходящих):"]
    for reason, d in rep["filtered"].items():
        out.append(f"  {reason}: {d['n']}")
        for e in d["examples"]:
            out.append(f"      {e['order_id']} [{e['site']}] {e['title'][:90]}")
    out.append(f"Устарели без оценки: {rep['never_assessed']['n']}")
    for e in rep["never_assessed"]["examples"]:
        out.append(f"      {e['order_id']} [{e['site']}] {e['title'][:90]}")
    out.append(f"Оценены хорошо, но не показаны: {rep['good_not_delivered']['n']}")
    for e in rep["good_not_delivered"]["examples"]:
        out.append(f"      {e['order_id']} [{e['site']}] {e['title'][:90]}")
    if rep["queue_reasons"]:
        out.append(f"Сейчас в очереди на оценку: {rep['queue_reasons']}")
    if rep["stale_reasons"]:
        out.append(f"Устарели по причинам: {rep['stale_reasons']}")
    out.append(f"Старые записи без известной причины/способа доставки: {rep['legacy_unknown']}")
    return "\n".join(out)
