"""Цена фриланс-заказа: считает код, а не модель.

Модель (ai_scorer) только описывает работу: часы по компонентам, категория,
ясность ТЗ, неизвестные. Здесь из этого получается цена полного объёма:

  hours = часы компонентов + доля на уточнение, проверку, правки и передачу (если их нет среди компонентов)
  base  = hours × ставка (общая или по вашим сделкам категории)
  risk  = 1.0 / 1.15 / 1.3 по ясности ТЗ + 0.1 за каждое неизвестное (максимум +0.3)
  own   = max(base × risk, минимум категории, hours × минимальная ставка)

Цена НЕ зависит от бюджета заказчика и от рынка бюджетов: одна и та же задача стоит одинаково при любом
бюджете. Рынок - справка. Бюджет сравнивается с ценой отдельно (compare_budget): хватает ли его на полный
объём, на минимально приемлемую цену, а при нехватке предлагается меньший объём или диагностика.
Функции calc_price/compare_budget/accuracy_report - чистые (без БД); price_order тянет данные из БД.
Деньги считаются через Decimal.
"""
import logging
import re
import statistics
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal

from config import (
    FREELANCE_FX_RATES, FREELANCE_MIN_PRICE, FREELANCE_MIN_RATE_PER_HOUR, FREELANCE_OVERHEAD_SHARE,
    FREELANCE_PRICE_SPREAD, FREELANCE_RATE_PER_HOUR,
)
from freelance import fx

CLARITY_RISK = {"ясно": Decimal("1.0"), "частично": Decimal("1.15"), "размыто": Decimal("1.3")}
DEFAULT_RISK = Decimal("1.15")          # ясность не указана
UNKNOWN_STEP = Decimal("0.1")
UNKNOWN_MAX = Decimal("0.3")
VAGUE_UNKNOWNS = 3                      # столько неизвестных (или ясность «размыто») - цена лишь ориентир
VAGUE_SPREAD_FACTOR = Decimal("2")      # у ориентира вилка вдвое шире
MAX_SPREAD = Decimal("0.5")
# этапы работы, кроме разработки: если среди компонентов модели их нет, код добавляет долю часов отдельно.
# Этапом считается компонент, НАЗВАНИЕ КОТОРОГО НАЧИНАЕТСЯ со слова этапа («проверка на данных клиента»), а не
# функция продукта («бот для проверки орфографии»); учтённой работа считается при двух разных группах этапов
STAGE_GROUPS = (
    re.compile(r"^\W*(?:уточнени|анализ|доступ|сбор требовани)", re.IGNORECASE),
    re.compile(r"^\W*(?:проверк|тестирован|приёмк|приемк)", re.IGNORECASE),
    re.compile(r"^\W*(?:правк|доработк по замечани|исправлени)", re.IGNORECASE),
    re.compile(r"^\W*(?:передач|запуск|деплой|внедрени|сдач|документац)", re.IGNORECASE),
)
STAGES_LABEL = "уточнение, проверка на данных клиента, правки, передача"
MIN_POINTS = 5                          # минимум точек для рынка и для ставки по сделкам
RATE_MIN_FACTOR = Decimal("0.3")        # ставка по сделкам: границы от общей ставки
RATE_MAX_FACTOR = Decimal("5")
PRICE_VERSION = 4                       # растёт при смене формулы: старые pending-оценки пересчитываются

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


def _ceil_money(value: Decimal) -> int:
    step = _step(value)
    return int((value / step).quantize(Decimal(1), rounding=ROUND_CEILING) * step)


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
    rate = min(default * RATE_MAX_FACTOR, max(default * RATE_MIN_FACTOR, rate))
    return max(rate, _dec(FREELANCE_MIN_RATE_PER_HOUR))


def stage_overhead(ai: dict, hours: Decimal) -> Decimal:
    """Часы на уточнение, проверку на данных клиента, правки и передачу результата. Если модель сама вынесла
    такие этапы в компоненты, они уже в часах и не добавляются повторно."""
    comps = ai.get("components")
    names = [str(c.get("name") or "") for c in comps if isinstance(c, dict)] if isinstance(comps, list) else []
    covered = sum(1 for group in STAGE_GROUPS if any(group.search(n) for n in names))
    if covered >= 2:
        return Decimal(0)
    return (hours * _dec(FREELANCE_OVERHEAD_SHARE)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)


def calc_price(ai: dict, budget=None, market: dict | None = None, rate=None) -> dict:
    """Расчётная цена полного объёма по структурной оценке модели.

    ai     - результат parse_ai_json (hours, components, category, clarity, unknowns);
    budget - принимается только ради совместимости вызовов и на цену НЕ влияет (сравнение - compare_budget);
    market - market_stats(): справка «где цена относительно бюджетов похожих заказов», цену не двигает;
    rate   - ставка ₽/ч (по умолчанию FREELANCE_RATE_PER_HOUR).
    Возвращает {} если часов нет (считать нечего).
    """
    base_hours = _dec(ai.get("hours"))
    if base_hours <= 0:
        return {}
    overhead = stage_overhead(ai, base_hours)
    hours = base_hours + overhead
    category = ai.get("category") if ai.get("category") in FREELANCE_MIN_PRICE else "other"
    rate_d = _dec(rate) if rate is not None and _dec(rate) > 0 else _dec(FREELANCE_RATE_PER_HOUR)
    rate_d = max(rate_d, _dec(FREELANCE_MIN_RATE_PER_HOUR))
    unknowns = ai.get("unknowns") or []
    unknowns = [str(u) for u in unknowns if str(u).strip()] if isinstance(unknowns, list) else []
    risk = risk_factor(ai.get("clarity") or "", len(unknowns))
    cat_floor = _dec(FREELANCE_MIN_PRICE.get(category, 0))
    # ниже минимальной ставки за час не работаем: ни рынок, ни бюджет цену под неё не опустят
    floor = max(cat_floor, hours * _dec(FREELANCE_MIN_RATE_PER_HOUR))

    own = max(hours * rate_d * risk, floor)
    risk_s = f"{risk:.2f}".rstrip("0").rstrip(".")
    basis = f"{_fmt_h(hours)} ч × {rate_d:.0f} ₽ × {risk_s}"
    if overhead > 0:
        basis += f" (в часах {_fmt_h(overhead)} ч: {STAGES_LABEL})"
    if own == floor and hours * rate_d * risk < floor:
        basis += f" · минимум {cat_floor:.0f} ₽"

    out: dict = {}
    market_ok = bool(market) and int(market.get("n") or 0) >= MIN_POINTS and _dec(market.get("median")) > 0
    if market:
        out["market_n"] = int(_dec(market.get("n")))
    if market_ok:
        out["market_low"] = int(_dec(market.get("p25")))
        out["market_high"] = int(_dec(market.get("p75")))

    vague = (ai.get("clarity") or "") == "размыто" or len(unknowns) >= VAGUE_UNKNOWNS
    spread = _dec(FREELANCE_PRICE_SPREAD)
    if vague:
        spread = min(MAX_SPREAD, spread * VAGUE_SPREAD_FACTOR)
    floor_i = max(_ceil_money(floor), int(_step(Decimal(0))))
    mid_i = max(_round_money(own), floor_i)
    low_i = min(mid_i, max(_round_money(Decimal(mid_i) * (1 - spread)), floor_i))
    high_i = max(mid_i, _round_money(Decimal(mid_i) * (1 + spread)))
    if market_ok:
        out["market_verdict"] = market_verdict(mid_i, out["market_low"], out["market_high"])
    out.update({
        "price_low": low_i,
        "price_mid": mid_i,
        "price_high": high_i,
        "price_full": mid_i,            # цена полного объёма (то же, что price_mid)
        "price_min": floor_i,           # минимально приемлемая цена
        "price_basis": basis,
        "price_rate": int(rate_d),
        "price_risk": float(risk),
        "price_hours": float(hours),
        "price_overhead_hours": float(overhead),
        "hours_low": float(hours),
        "hours_high": float((hours * risk).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)),
        "price_vague": vague,
        "price_conditions": unknowns[:5],
        "price_v": PRICE_VERSION,
    })
    return out


BUDGET_NOTES = {
    "ok": "бюджет покрывает полный объём",
    "below_full": "бюджет ниже расчётной цены полного объёма: предложить меньший объём или этапы",
    "below_min": "бюджет ниже минимально приемлемой цены: предложить диагностику или меньший объём; "
                 "цену неизменённого объёма не снижать",
    "open_low": "указано «от N» ниже расчётной цены: верхняя граница неизвестна, уточнить бюджет",
    "none": "бюджет не указан (это не признак плохого заказа): назвать цену и условия оценки",
    "uncertain_currency": "валюта бюджета неизвестна: сравнить с ценой нельзя",
    "uncertain_fx": "валюта иностранная, надёжного курса (источник и дата) нет: сравнение не выполнено",
    "suspicious": "значение бюджета выглядит испорченным: в расчёт не берётся",
    "hourly": "почасовая ставка",
}


def compare_budget(price: dict, budget: dict | None, rates=None, today=None, rate=None) -> dict:
    """Сравнение бюджета клиента с расчётной ценой (цену не меняет).

    price  - результат calc_price (price_mid, price_min); budget - budget.parse_budget();
    rates  - курсы fx.load_rates (по умолчанию из настройки FREELANCE_FX_RATES); today - дата для проверки курса.
    Возвращает {budget_status, budget_note, budget_type, budget_currency, budget_rub, budget_fx, ...}."""
    b = budget or {}
    rates = fx.load_rates(FREELANCE_FX_RATES) if rates is None else rates
    out = {"budget_type": b.get("type") or "unknown", "budget_currency": b.get("currency") or "",
           "budget_rub": None, "budget_fx": None}

    def done(status: str, **extra) -> dict:
        out["budget_status"] = status
        out["budget_note"] = BUDGET_NOTES[status]
        out.update(extra)
        return out

    if b.get("suspicious"):
        return done("suspicious")
    kind = b.get("type") or "unknown"
    if kind == "unknown":
        return done("none")
    currency = b.get("currency") or ""
    if not currency:
        return done("uncertain_currency")
    amount = float(b.get("max") or b.get("min") or 0)
    if amount <= 0:
        return done("none")
    conv = fx.to_rub(amount, currency, rates, today)
    if conv is None:
        return done("uncertain_fx")
    rub = conv["rub"]
    fx_info = None if currency == "RUB" else {k: conv[k] for k in ("rate", "source", "date")}
    if kind == "hourly":
        own_rate = _dec(rate) if rate is not None and _dec(rate) > 0 else _dec(FREELANCE_RATE_PER_HOUR)
        level = ("ok" if _dec(rub) >= own_rate else
                 "below_rate" if _dec(rub) >= _dec(FREELANCE_MIN_RATE_PER_HOUR) else "below_min")
        text = {"ok": "не ниже вашей ставки", "below_rate": "ниже вашей ставки, но выше минимальной",
                "below_min": "ниже минимальной ставки"}[level]
        return done("hourly", budget_per_hour=int(rub), hourly_level=level, budget_fx=fx_info,
                    budget_note=f"почасовая ставка {int(rub)} ₽/ч: {text}")
    mid, minimum = _dec(price.get("price_mid")), _dec(price.get("price_min"))
    if mid <= 0:
        return done("none")
    extra = {"budget_rub": int(rub), "budget_fx": fx_info}
    if kind == "min":
        return done("ok" if _dec(rub) >= mid else "open_low", **extra)
    if _dec(rub) >= mid:
        return done("ok", **extra)
    if _dec(rub) >= minimum:
        return done("below_full", **extra)
    return done("below_min", **extra)


def market_verdict(price: int, p25: int, p75: int) -> str:
    """Где цена относительно рынка (P25–P75 бюджетов похожих заказов)."""
    if p75 > 0 and price > p75:
        return f"выше рынка на {round((price / p75 - 1) * 100)}%"
    if p25 > 0 and price < p25:
        return f"ниже рынка на {round((1 - price / p25) * 100)}%"
    return "в рынке"


def price_order(ai: dict, budget=None) -> dict:
    """calc_price + данные из БД (рынок категории - справка, ваша ставка по сделкам) + сравнение бюджета.
    budget - результат budget.parse_budget (или None). Сбой БД не должен ломать оценку: тогда считаем по формуле."""
    market, rate = None, None
    category = ai.get("category") or "other"
    try:
        from database import freelance_deals, market_stats
        market = market_stats(category, report_small=True)
        deals = freelance_deals(category)
        if len(deals) >= MIN_POINTS:
            rate = rate_from_deals(deals)
    except Exception:
        logging.exception("[FREELANCE] не удалось получить рынок/сделки для цены")
    price = calc_price(ai, None, market, rate)
    if price:
        price.update(compare_budget(price, budget if isinstance(budget, dict) else None, rate=rate))
    return price


def hours_calibration(rows: list[dict], min_points: int = MIN_POINTS) -> dict:
    """Калибровка трудозатрат по фактическим часам. rows - [{category, hours_est, hours_actual}].
    Возвращает {категория: {"n", "ratio"}}: ratio - медиана факт/оценка (>1: оценка занижена). Категории
    с числом точек меньше min_points не попадают: по малой выборке поправку не выдаём."""
    groups: dict[str, list[float]] = {}
    for r in rows:
        est, actual = _dec(r.get("hours_est")), _dec(r.get("hours_actual"))
        if est > 0 and actual > 0:
            groups.setdefault(r.get("category") or "other", []).append(float(actual / est))
    return {cat: {"n": len(v), "ratio": round(statistics.median(v), 2)}
            for cat, v in groups.items() if len(v) >= min_points}


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
