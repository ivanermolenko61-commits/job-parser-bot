"""Ёмкость AI-оценки после отключения Yandex (грант кончается 20.11.2026).

Грант не продлевается, оплата не включается: считаем, сколько оценок в сутки остаётся у бесплатных
провайдеров цепочки и хватает ли их на спрос. Чистая функция forecast() (без БД и сети) плюс чтение спроса
из журнала. Допущения названы в результате: это оценка, а не измерение.
"""
import math
from datetime import date, datetime, timezone

# Cloudflare Workers AI: нейроны общие на аккаунт, поэтому быстрый триаж (маленькая модель) и полная оценка
# (gpt-oss-120b) делят одну дневную квоту. ~185 нейронов на полную оценку замерены 11.10.2026 (комментарий
# к PROVIDER_SPECS в ai_scorer); стоимость триажа в нейронах НЕ измерена - это оценка по тарифу
# (~500 токенов входа и ~20 выхода у llama-3.1-8b), её надо подтвердить по панели Cloudflare.
FULL_ORDER_NEURONS = 185.0
TRIAGE_CALL_NEURONS = 3.0
SHARED_PROVIDER = "cloudflare"
# С IP BotHost подтверждённо отвечают только эти провайдеры (job-bot-fix, проверено 11.10.2026): github подменяет
# ответ, groq блокирует РФ, cerebras и mistral требуют карту, gemini недоступен из региона. Остальные, даже
# настроенные ключом, в ёмкость не входят - только в «не подтверждены»
CONFIRMED_FROM_BOTHOST = ("cloudflare", "openrouter")


# Допущение, пока нет замера: доля вызовов, потраченных зря (ошибка провайдера, повтор неудачной оценки).
# Замер - measure_overhead() по журналу (сумма неудачных попыток на один заказ); он подставляется вместо допущения.
DEFAULT_OVERHEAD = 0.10


def forecast(status: dict, demand_per_day: float, shadow_per_day: int, today: date, yandex_until: date,
             overhead: float | None = None) -> dict:
    """Прогноз ёмкости. status - ai_scorer.status() (провайдеры, ключи, лимиты); demand_per_day - сколько
    заказов в сутки доходит до оценки; shadow_per_day - сколько быстрых оценок в сутки тратит shadow-триаж.
    Возвращает словарь с разбивкой по провайдерам и итогом без Yandex."""
    providers = (status or {}).get("providers", {})
    live: dict[str, int] = {}
    unconfirmed: dict[str, int] = {}
    for name, p in providers.items():
        if name == "yandex" or not p.get("has_key") or not p.get("enabled") or p.get("dead"):
            continue
        if p.get("key_error") or p.get("blocked_sec"):
            continue
        (live if name in CONFIRMED_FROM_BOTHOST else unconfirmed)[name] = int(p.get("limit") or 0)
    # триаж забирает нейроны у общей квоты Cloudflare: пересчитываем в «полные оценки»
    triage_cost = 0.0
    if SHARED_PROVIDER in live and shadow_per_day > 0:
        triage_cost = shadow_per_day * TRIAGE_CALL_NEURONS / FULL_ORDER_NEURONS
    total = float(sum(live.values()))
    # чистая ёмкость: настройки провайдеров минус триаж и минус вызовы, потраченные на ошибки и повторы
    overhead_value = DEFAULT_OVERHEAD if overhead is None else max(0.0, float(overhead))
    net = max(0.0, (total - triage_cost) / (1.0 + overhead_value))
    yandex = providers.get("yandex") or {}
    yandex_on = bool(yandex.get("has_key") and yandex.get("enabled")) and today < yandex_until
    coverage = (net / demand_per_day) if demand_per_day > 0 else None
    return {
        "providers": live,
        "unconfirmed": unconfirmed,
        "total_without_yandex": total,
        "triage_cost_orders": round(triage_cost, 1),
        "overhead": round(overhead_value, 3),
        "overhead_measured": overhead is not None,
        "net_without_yandex": round(net, 1),
        "demand_per_day": demand_per_day,
        "coverage": coverage,                       # доля спроса, которую можно оценить; >= 1 - хватает
        "deficit_per_day": max(0.0, demand_per_day - net),
        "yandex_active_now": yandex_on,
        "yandex_until": yandex_until.isoformat(),
        "days_to_yandex_off": (yandex_until - today).days,
        "assumptions": [
            f"полная оценка ~{FULL_ORDER_NEURONS:.0f} нейронов (исторический замер 11.10), быстрый триаж "
            f"~{TRIAGE_CALL_NEURONS:.0f} нейрона (оценка по тарифу, не замер)",
            "доля вызовов на ошибки и повторы: " + (f"{overhead_value:.0%} (замер по журналу)" if overhead is not None
                                                    else f"{overhead_value:.0%} (допущение, замера нет)"),
            "лимиты провайдеров - из настроек бота (per_day), фактическая бесплатная квота может быть ниже; "
            "в ёмкость входят только провайдеры, подтверждённые с IP BotHost",
            "Gemini недоступен из региона BotHost и в сумму не входит",
        ],
    }


def demand_per_day(days: int = 7) -> float | None:
    """Сколько заказов в сутки доходит до оценки (прошли правила и дедуп): строки журнала за days дней,
    кроме отсеянных правилом, дублей и устаревших при обнаружении. None - журнал пуст."""
    import database
    with database._connect() as conn:
        first = conn.execute(
            "SELECT MIN(seen_at) FROM freelance_orders WHERE seen_at >= datetime('now', ?)",
            (f"-{int(days)} days",)).fetchone()[0]
        if not first:
            return None
        n = conn.execute(
            "SELECT COUNT(*) FROM freelance_orders WHERE seen_at >= datetime('now', ?) "
            "AND tech IN ('queued', 'assessed') OR (seen_at >= datetime('now', ?) AND tech = 'stale' "
            "AND COALESCE(stale_reason, '') != 'old_at_discovery')",
            (f"-{int(days)} days", f"-{int(days)} days")).fetchone()[0]
    span_days = max(1.0, (datetime.now(timezone.utc).replace(tzinfo=None) - datetime.strptime(first, "%Y-%m-%d %H:%M:%S")).total_seconds() / 86400)
    return n / min(span_days, float(days))


def measure_overhead(days: int = 7) -> float | None:
    """Замер по журналу: неудачных попыток оценки на один заказ, дошедший до оценки (attempts пишется при
    ошибке провайдера). None - журнал пуст, остаётся допущение DEFAULT_OVERHEAD."""
    import database
    with database._connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(attempts), 0) FROM freelance_orders "
            "WHERE seen_at >= datetime('now', ?) AND tech IN ('queued', 'assessed', 'stale')",
            (f"-{int(days)} days",)).fetchone()
    if not row or not row[0]:
        return None
    return row[1] / row[0]


def simulate_queue(demand_per_day: float, net_per_day: float, mix: dict, good_share: dict,
                   *, days: int = 14, warmup_days: int = 3, reserve: dict | None = None,
                   aging_hours: float = 12.0, aging_boost: float = 1.0, queue_max_hours: float = 48.0,
                   quota_reset_hour: int = 3, skip_share: dict | None = None,
                   triage_miss: float = 0.0) -> dict:
    """Устойчивый спрос выше ёмкости: что успевает оцениваться, сколько ждёт и сколько истекает.

    Почасовая модель правил очереди бота: группы приоритета 1-3 (mix - доли спроса), ранг = группа минус
    бонус старения, внутри ранга новые первыми, резерв квоты (reserve: группа -> доля, ниже которой она не
    оценивается), суточный сброс квоты в quota_reset_hour (МСК), истечение через queue_max_hours.
    good_share - доля подходящих среди заказов группы (допущение или замер): по ней считаются потерянные
    подходящие. skip_share - доля группы, снимаемая до полной оценки (например отсекающим триажем).
    Первые warmup_days в итог не входят. Детерминированная, без случайных чисел."""
    reserve = reserve if reserve is not None else {2: 0.2, 3: 0.5}
    skip_share = skip_share or {}
    groups = (1, 2, 3)
    total_mix = sum(mix.get(g, 0.0) for g in groups) or 1.0
    hours = days * 24
    warm = warmup_days * 24
    carry = {g: 0.0 for g in groups}
    pending: list = []   # (час поступления, группа)
    stat = {g: {"arrived": 0, "scored": 0, "expired": 0, "waits": []} for g in groups}
    quota = net_per_day
    for h in range(hours):
        if h % 24 == quota_reset_hour:
            quota = net_per_day
        for g in groups:
            carry[g] += demand_per_day * mix.get(g, 0.0) / total_mix / 24.0 * (1.0 - skip_share.get(g, 0.0))
            n = int(carry[g])
            carry[g] -= n
            pending.extend((h, g) for _ in range(n))
            if h >= warm:
                stat[g]["arrived"] += n
        keep = []
        for item in pending:
            if h - item[0] > queue_max_hours:
                if item[0] >= warm:
                    stat[item[1]]["expired"] += 1
            else:
                keep.append(item)

        def rank(item, now=h):
            return item[1] - min(aging_boost, (now - item[0]) / aging_hours)

        keep.sort(key=lambda it: (rank(it), -it[0]))
        rest = []
        for item in keep:
            eff = max(1, math.ceil(rank(item) - 1e-9))
            share = reserve.get(eff)
            blocked = share is not None and quota <= net_per_day * share
            if quota >= 1.0 and not blocked:
                quota -= 1.0
                if item[0] >= warm:
                    stat[item[1]]["scored"] += 1
                    stat[item[1]]["waits"].append(h - item[0])
            else:
                rest.append(item)
        pending = rest
    out: dict = {"groups": {}, "days": days - warmup_days}
    lost_good = arrived_good = triage_lost = total_good = 0.0
    window = days - warmup_days
    for g in groups:
        st = stat[g]
        waits = sorted(st["waits"])
        left = sum(1 for it in pending if it[1] == g and it[0] >= warm)
        gs = good_share.get(g, 0.0)
        # все подходящие группы за окно и те из них, что триаж ошибочно снял (triage_miss - ДОПУЩЕНИЕ: доля
        # ВСЕХ подходящих заказов группы, потерянных триажем; замера нет). Остальные подходящие идут в очередь
        group_good = demand_per_day * mix.get(g, 0.0) / total_mix * window * gs
        lost_by_triage = group_good * triage_miss if skip_share.get(g, 0.0) > 0 else 0.0
        q_share = gs if not lost_by_triage else ((group_good - lost_by_triage) / st["arrived"]
                                                 if st["arrived"] else 0.0)
        triage_lost += lost_by_triage
        total_good += group_good
        # потеря - только истёкшие без оценки; ещё ждущие (хвост на конец прогона) показаны отдельно
        unscored = st["expired"]
        lost_good += unscored * q_share
        arrived_good += st["arrived"] * q_share
        out["groups"][g] = {
            "arrived": st["arrived"], "scored": st["scored"], "expired": st["expired"], "waiting": left,
            "wait_median_h": waits[len(waits) // 2] if waits else None,
            "wait_p90_h": waits[int(len(waits) * 0.9)] if waits else None,
            "lost_good": round(unscored * q_share, 1),
        }
    arrived = sum(v["arrived"] for v in out["groups"].values())
    out["arrived"] = arrived
    out["scored"] = sum(v["scored"] for v in out["groups"].values())
    out["expired"] = sum(v["expired"] for v in out["groups"].values())
    out["waiting_at_end"] = sum(v["waiting"] for v in out["groups"].values())
    out["good_arrived"] = round(arrived_good, 1)
    out["good_lost"] = round(lost_good, 1)
    out["good_lost_share"] = round(lost_good / arrived_good, 3) if arrived_good else None
    out["triage_good_lost"] = round(triage_lost, 1)
    out["good_total"] = round(total_good, 1)
    out["loss_total_share"] = round((lost_good + triage_lost) / total_good, 3) if total_good else None
    return out


def format_forecast(f: dict) -> str:
    lines = []
    prov = ", ".join(f"{k} {v}" for k, v in sorted(f["providers"].items())) or "нет активных провайдеров"
    lines.append(f"Без Yandex: {f['total_without_yandex']:.0f} оценок/сутки ({prov})")
    if f.get("unconfirmed"):
        lines.append("Настроены, но не подтверждены с BotHost (не в сумме): "
                     + ", ".join(f"{k} {v}" for k, v in sorted(f["unconfirmed"].items())))
    if f["triage_cost_orders"]:
        lines.append(f"shadow-триаж забирает ≈{f['triage_cost_orders']} оценок/сутки "
                     f"из общей квоты Cloudflare → чистая ёмкость ≈{f['net_without_yandex']:.0f}")
    if f["demand_per_day"] > 0:
        cover = f["coverage"] * 100 if f["coverage"] is not None else 0
        lines.append(f"Спрос: ≈{f['demand_per_day']:.0f} заказов/сутки до оценки → покрытие {cover:.0f}%"
                     + (f", нехватка ≈{f['deficit_per_day']:.0f}/сутки" if f["deficit_per_day"] > 0 else ""))
    else:
        lines.append("Спрос: журнал пока пуст, оценить покрытие нельзя")
    lines.append(f"Yandex отключается {f['yandex_until']} (через {f['days_to_yandex_off']} дн.)"
                 + (" - ещё включён" if f["yandex_active_now"] else ""))
    lines.append("Допущения: " + "; ".join(f["assumptions"]))
    return "\n".join(lines)
