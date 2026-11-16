"""Ёмкость AI-оценки после отключения Yandex (грант кончается 20.11.2026).

Грант не продлевается, оплата не включается: считаем, сколько оценок в сутки остаётся у бесплатных
провайдеров цепочки и хватает ли их на спрос. Чистая функция forecast() (без БД и сети) плюс чтение спроса
из журнала. Допущения названы в результате: это оценка, а не измерение.
"""
from datetime import date, datetime

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


def forecast(status: dict, demand_per_day: float, shadow_per_day: int, today: date, yandex_until: date) -> dict:
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
    net = max(0.0, total - triage_cost)
    yandex = providers.get("yandex") or {}
    yandex_on = bool(yandex.get("has_key") and yandex.get("enabled")) and today < yandex_until
    coverage = (net / demand_per_day) if demand_per_day > 0 else None
    return {
        "providers": live,
        "unconfirmed": unconfirmed,
        "total_without_yandex": total,
        "triage_cost_orders": round(triage_cost, 1),
        "net_without_yandex": round(net, 1),
        "demand_per_day": demand_per_day,
        "coverage": coverage,                       # доля спроса, которую можно оценить; >= 1 - хватает
        "deficit_per_day": max(0.0, demand_per_day - net),
        "yandex_active_now": yandex_on,
        "yandex_until": yandex_until.isoformat(),
        "days_to_yandex_off": (yandex_until - today).days,
        "assumptions": [
            f"полная оценка ~{FULL_ORDER_NEURONS:.0f} нейронов (замер 11.10), быстрый триаж ~{TRIAGE_CALL_NEURONS:.0f} "
            f"нейрона (оценка по тарифу, не замер)",
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
    span_days = max(1.0, (datetime.utcnow() - datetime.strptime(first, "%Y-%m-%d %H:%M:%S")).total_seconds() / 86400)
    return n / min(span_days, float(days))


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
