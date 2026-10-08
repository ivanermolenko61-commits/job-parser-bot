"""Цена фриланс-заказа: считает код, а не модель.

Модель (ai_scorer) только описывает работу: часы по компонентам, категория,
ясность ТЗ, неизвестные. Здесь из этого получается цена:

  base = часы × ставка (общая или по вашим сделкам категории)
  risk = 1.0 / 1.15 / 1.3 по ясности ТЗ + 0.1 за каждое неизвестное (максимум +0.3)
  own  = max(base × risk, минимум категории)

Затем рыночный якорь: бюджет заказчика ограничивает цену сверху (×1.1), а без
бюджета цена слегка тянется к медиане бюджетов похожих заказов (вес 0.3).
Функции calc_price/accuracy_report - чистые (без БД); price_order тянет данные из БД.
Деньги считаются через Decimal.
"""
import logging
import statistics
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal

from config import FREELANCE_MIN_PRICE, FREELANCE_PRICE_SPREAD, FREELANCE_RATE_PER_HOUR

CLARITY_RISK = {"ясно": Decimal("1.0"), "частично": Decimal("1.15"), "размыто": Decimal("1.3")}
DEFAULT_RISK = Decimal("1.15")          # ясность не указана
UNKNOWN_STEP = Decimal("0.1")
UNKNOWN_MAX = Decimal("0.3")
BUDGET_CAP = Decimal("1.1")             # цена не выше бюджета × 1.1
BUDGET_LOW_RATIO = Decimal("1.5")       # своя цена > бюджета × 1.5 - «бюджет занижен»
MARKET_WEIGHT = Decimal("0.3")          # вес рыночной медианы, если бюджета нет
MIN_POINTS = 5                          # минимум точек для рынка и для ставки по сделкам
RATE_MIN_FACTOR = Decimal("0.3")        # ставка по сделкам: границы от общей ставки
RATE_MAX_FACTOR = Decimal("5")

CATEGORY_NAMES = {          # для строки «рынок ботов: ...»
    "tg_bot": "ботов", "parser": "парсеров", "landing": "лендингов",
    "site_fix": "правок сайтов", "wp": "WordPress", "script": "скриптов",
    "integration": "интеграций", "sheets": "таблиц", "other": "прочего",
}


def _dec(value) -> Decimal:
    try:
        d = Decimal(str(value if value is not None else 0))
    except Exception:
        return Decimal(0)
    return d if d.is_finite() else Decimal(0)


def _step(value: Decimal) -> Decimal:
    """Шаг округления: 100 ₽ до 10 000, дальше 500 ₽."""
    return Decimal(100) if value < 10000 else Decimal(500)


def _round_money(value: Decimal) -> int:
    step = _step(value)
    return int((value / step).quantize(Decimal(1), rounding=ROUND_HALF_UP) * step)


def _fmt_h(hours: Decimal) -> str:
    return f"{hours:.1f}".rstrip("0").rstrip(".")


def risk_factor(clarity: str, unknowns_count: int) -> Decimal:
    base = CLARITY_RISK.get(clarity or "", DEFAULT_RISK)
    extra = min(UNKNOWN_MAX, UNKNOWN_STEP * max(0, int(unknowns_count)))
    return base + extra


def rate_from_deals(deals: list, default_rate=None) -> Decimal:
    """Ставка категории: медиана deal_price / (hours × риск) по сделкам, если их ≥ MIN_POINTS.
    deals - [(deal_price, hours, risk)]; risk можно не указывать (тогда 1.0).
    Риск делим, потому что calc_price умножает на него ещё раз. Иначе общая ставка."""
    default = _dec(default_rate if default_rate is not None else FREELANCE_RATE_PER_HOUR)
    rates = []
    for deal in deals:
        price, hours = _dec(deal[0]), _dec(deal[1])
        risk = _dec(deal[2]) if len(deal) > 2 else Decimal(1)
        if risk <= 0:
            risk = Decimal(1)
        if price > 0 and hours > 0:
            rates.append(price / (hours * risk))
    if len(rates) < MIN_POINTS:
        return default
    rate = Decimal(str(statistics.median(rates)))
    return min(default * RATE_MAX_FACTOR, max(default * RATE_MIN_FACTOR, rate))


def calc_price(ai: dict, budget=0, market: dict | None = None, rate=None) -> dict:
    """Рекомендуемая цена по структурной оценке модели.

    ai     - результат parse_ai_json (hours, category, clarity, unknowns);
    budget - бюджет заказчика, ₽ (0 = не указан);
    market - market_stats(): {"median", "p25", "p75", "n"} или None;
    rate   - ставка ₽/ч (по умолчанию FREELANCE_RATE_PER_HOUR).
    Возвращает {} если часов нет (считать нечего).
    """
    hours = _dec(ai.get("hours"))
    if hours <= 0:
        return {}
    category = ai.get("category") if ai.get("category") in FREELANCE_MIN_PRICE else "other"
    rate_d = _dec(rate) if rate is not None and _dec(rate) > 0 else _dec(FREELANCE_RATE_PER_HOUR)
    unknowns = ai.get("unknowns") or []
    risk = risk_factor(ai.get("clarity") or "", len(unknowns) if isinstance(unknowns, list) else 0)
    floor = _dec(FREELANCE_MIN_PRICE.get(category, 0))

    own = max(hours * rate_d * risk, floor)
    risk_s = f"{risk:.2f}".rstrip("0").rstrip(".")
    basis = f"{_fmt_h(hours)} ч × {rate_d:.0f} ₽ × {risk_s}"
    if own == floor and hours * rate_d * risk < floor:
        basis += f" · минимум {floor:.0f} ₽"

    out: dict = {}
    budget_d = _dec(budget)
    market_ok = bool(market) and int(market.get("n") or 0) >= MIN_POINTS and _dec(market.get("median")) > 0
    if budget_d <= 0 and market_ok:
        own = max(own * (1 - MARKET_WEIGHT) + _dec(market["median"]) * MARKET_WEIGHT, floor)
        out["price_market_applied"] = True
    if market_ok:
        out["market_low"] = int(_dec(market.get("p25")))
        out["market_high"] = int(_dec(market.get("p75")))
        out["market_n"] = int(market["n"])

    mid = own
    out["budget_low"] = False
    out["budget_capped"] = False
    if budget_d > 0:
        out["budget"] = int(budget_d)
        if budget_d < floor:
            # бюджет ниже нашего минимума: не ограничиваем им, показываем свою цену
            out["budget_low"] = True
        else:
            if own > budget_d * BUDGET_LOW_RATIO:
                out["budget_low"] = True
            cap = budget_d * BUDGET_CAP
            if own > cap:
                mid = cap
                out["budget_capped"] = True

    mid_i = _round_money(mid)
    if out["budget_capped"]:
        # округление не должно вернуть цену выше потолка
        cap_i = int((budget_d * BUDGET_CAP / _step(mid)).quantize(Decimal(1), rounding=ROUND_FLOOR) * _step(mid))
        mid_i = min(mid_i, cap_i)
    mid_i = max(mid_i, int(floor), int(_step(Decimal(0))))
    spread = _dec(FREELANCE_PRICE_SPREAD)
    low_i = min(mid_i, max(_round_money(Decimal(mid_i) * (1 - spread)), int(_step(Decimal(0)))))
    high_i = max(mid_i, _round_money(Decimal(mid_i) * (1 + spread)))
    out.update({
        "price_low": low_i,
        "price_mid": mid_i,
        "price_high": high_i,
        "price_basis": basis,
        "price_rate": int(rate_d),
        "price_risk": float(risk),
    })
    return out


def price_order(ai: dict, budget=0) -> dict:
    """calc_price + данные из БД (рынок категории, ваша ставка по сделкам).
    Сбой БД не должен ломать оценку: тогда считаем по формуле без якорей."""
    market, rate = None, None
    category = ai.get("category") or "other"
    try:
        from database import freelance_deals, market_stats
        market = market_stats(category)
        deals = freelance_deals(category)
        if len(deals) >= MIN_POINTS:
            rate = rate_from_deals(deals)
    except Exception:
        logging.exception("[FREELANCE] не удалось получить рынок/сделки для цены")
    return calc_price(ai, budget, market, rate)


def accuracy_report(rows: list[dict]) -> dict:
    """Точность цен: rows - [{category, deal_price, price_mid}] (только со сделкой).
    Возвращает {"total": N, "by_category": {cat: {"n", "mape", "bias"}}}.
    mape - средняя |ошибка| в % от сделки; bias - средний знак: >0 мы завышали."""
    groups: dict[str, list[float]] = {}
    for r in rows:
        deal, mid = _dec(r.get("deal_price")), _dec(r.get("price_mid"))
        if deal <= 0 or mid <= 0:
            continue
        groups.setdefault(r.get("category") or "other", []).append(float((mid - deal) / deal * 100))
    by_cat = {
        cat: {"n": len(errs),
              "mape": sum(abs(e) for e in errs) / len(errs),
              "bias": sum(errs) / len(errs)}
        for cat, errs in groups.items()
    }
    return {"total": sum(c["n"] for c in by_cat.values()), "by_category": by_cat}
