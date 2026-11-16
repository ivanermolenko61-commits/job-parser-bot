"""Курсы валют для сравнения бюджета с ценой: только заданные явно, с источником и датой.

Курсы не берутся из сети и не угадываются. Они лежат в настройке FREELANCE_FX_RATES (JSON):
{"USD": {"rate": 90.5, "source": "ЦБ РФ", "date": "2026-11-16"}} - рублей за единицу валюты. Курс считается
надёжным, если указаны источник и дата и дата не старше FX_MAX_AGE_DAYS. Без надёжного курса пересчёта нет,
и сравнение бюджета с ценой остаётся неопределённым (его не заменяют рублями «как есть»).
"""
import json
import math
from datetime import date, datetime, timezone

FX_MAX_AGE_DAYS = 7


def _parse_date(value) -> date | None:
    try:
        return datetime.strptime(str(value).strip(), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def load_rates(raw) -> dict:
    """Разбор настройки: {код: {rate, source, date}}. Строка или готовый dict; негодное - пропускается."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw) if raw.strip() else {}
        except ValueError:
            return {}
    if not isinstance(raw, dict):
        return {}
    rates = {}
    for code, item in raw.items():
        if not isinstance(item, dict):
            continue
        try:
            rate = float(item.get("rate"))
        except (TypeError, ValueError):
            continue
        day = _parse_date(item.get("date"))
        source = str(item.get("source") or "").strip()
        if not math.isfinite(rate) or rate <= 0 or day is None or not source:
            continue
        rates[str(code).strip().upper()] = {"rate": rate, "source": source[:60], "date": day.isoformat()}
    return rates


def to_rub(amount: float, currency: str, rates: dict | None, today: date | None = None) -> dict | None:
    """Пересчёт в рубли: {"rub", "rate", "source", "date"} или None, если курса нет, он без источника/даты
    либо старше FX_MAX_AGE_DAYS. Рубли пересчёта не требуют (курс 1, источник «рубли»)."""
    code = (currency or "").strip().upper()
    if not code:
        return None
    if code == "RUB":
        return {"rub": float(amount), "rate": 1.0, "source": "рубли", "date": ""}
    item = (rates or {}).get(code)
    if not item:
        return None
    day = _parse_date(item.get("date"))
    today = today or datetime.now(timezone.utc).date()
    if day is None or not str(item.get("source") or "").strip() or day > today:
        return None
    if (today - day).days > FX_MAX_AGE_DAYS:
        return None
    try:
        rate = float(item["rate"])
    except (KeyError, TypeError, ValueError):
        return None
    if not math.isfinite(rate) or rate <= 0:
        return None
    return {"rub": float(amount) * rate, "rate": rate, "source": str(item["source"]), "date": day.isoformat()}
