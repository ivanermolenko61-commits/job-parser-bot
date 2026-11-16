"""Бюджет объявления: сумма, валюта и тип. Чистые функции, без БД и сети.

Бюджет объявления - не цена сделки: это пожелание заказчика (потолок, диапазон, ориентир). Неизвестную
валюту нельзя считать рублями; сравнивать с рублёвой ценой можно только рублёвый бюджет (или иностранный
с курсом из freelance/fx.py). Подозрительно большие суммы (например, склеенные цифры диапазона
«5 000 ₽ – 10 000 ₽» -> 500010000 в старых строках FL.ru) в расчёты не попадают.
"""
import re

MAX_PLAUSIBLE = 10_000_000.0   # выше - считаем испорченным значением, а не бюджетом

# Знаки валют. Короткие буквенные («р», «р.») ищем только сразу после числа, иначе «договор.» стал бы рублями.
_CURRENCY_PATTERNS = (
    ("RUB", re.compile(r"₽|\bруб|\brub\b|\brur\b|рублей|(?<=\d)\s*р(?![а-яёa-z])", re.IGNORECASE)),
    ("UAH", re.compile(r"₴|\bгрн|\buah\b|гривн", re.IGNORECASE)),
    ("USD", re.compile(r"\$|\busd\b|доллар", re.IGNORECASE)),
    ("EUR", re.compile(r"€|\beur\b|\bевро\b", re.IGNORECASE)),
    ("KZT", re.compile(r"₸|\bkzt\b|тенге", re.IGNORECASE)),
)
_KNOWN = {name for name, _ in _CURRENCY_PATTERNS}
# знаки и коды валют, которых нет в списке выше: бюджет в них не рубли и не распознан («100 £», «5000 zł»)
_OTHER_CURRENCY = re.compile(
    r"[£¥₹₺₩₪฿₦₫₭₮₼₾]|\b(?:gbp|cny|jpy|try|pln|byn|uzs|gel|azn|amd|czk|chf|kgs|rmb)\b|zł"
    r"|\b(?:юан|фунт|йен|драм)|\b(?:лир[аы]?|лари)\b",
    re.IGNORECASE)
# число: цифры с пробельными разделителями тысяч и необязательной десятичной частью, затем множитель (тыс, к, k, млн)
_NUMBER_RE = re.compile(
    r"(\d(?:[\d   ]*\d)?(?:[.,]\d{1,2})?)(?![\d])"
    r"(?:\s?(тыс\b\.?|т\.\s?р\b\.?|к(?![а-яёa-z])|k\b|млн\b\.?))?", re.IGNORECASE)
_MULTIPLIERS = {"тыс": 1_000.0, "т.р": 1_000.0, "т. р": 1_000.0, "к": 1_000.0, "k": 1_000.0, "млн": 1_000_000.0}
_HOURLY_RE = re.compile(r"(?:в|за|/)\s*час\b|\bчас(?:овая|овой)\b|/\s*ч\b|per hour|/h\b|hourly", re.IGNORECASE)


def detect_currency(text: str, hint: str = "") -> str:
    """Код валюты по подсказке источника (поле currency alot.pro) или по знакам в тексте; "" - неизвестна.
    Подсказка с неизвестным кодом валюту не угадывает: возвращается "" (а не рубли)."""
    hint = (hint or "").strip().upper()
    found = [code for code, pat in _CURRENCY_PATTERNS if pat.search(text or "")]
    if _OTHER_CURRENCY.search(text or ""):
        return ""     # валюта, которую не умеем сравнивать, или смесь с известной: не рубли и не угадываем
    if hint in _KNOWN:
        # знак в самом тексте важнее подсказки источника: «500 $» с подсказкой RUB не рубли и не угадывается
        return hint if not found or found == [hint] else ""
    # два разных знака валют в одном бюджете («5000 ₽ или 60 $») - однозначно сравнивать нельзя
    return found[0] if len(found) == 1 else ""


def _mult(token: str | None) -> float:
    if not token:
        return 1.0
    return _MULTIPLIERS.get(re.sub(r"[\s.]+$", "", token.lower()).replace("  ", " "), 1.0)


def _numbers(text: str) -> list[float]:
    """Числа бюджета. Множитель у последнего числа («5-10 тыс.») относится и к предыдущим без множителя."""
    raw: list[tuple[float, float, bool]] = []
    for m in _NUMBER_RE.finditer(text or ""):
        digits = re.sub(r"[   ]", "", m.group(1)).replace(",", ".")
        try:
            value = float(digits)
        except ValueError:
            continue
        raw.append((value, _mult(m.group(2)), bool(m.group(2))))
    if len(raw) >= 2 and raw[-1][2]:
        last_mult = raw[-1][1]
        raw = [(v, last_mult if not has else mult, True) for v, mult, has in raw]
    return [v * mult for v, mult, _ in raw]


def parse_budget(price_text: str, currency_hint: str = "") -> dict:
    """Разбор бюджета объявления.

    Возвращает {min, max, currency, type, suspicious}: type - fixed (одно число), max («до N»), min («от N»),
    range (два числа), hourly (ставка в час, суммой работы не является), unknown (нет суммы или «по договорённости»).
    min/max - 0.0, если сумма неизвестна или испорчена; currency - "" при неизвестной валюте."""
    text = (price_text or "").strip()
    out = {"min": 0.0, "max": 0.0, "currency": detect_currency(text, currency_hint), "type": "unknown",
           "suspicious": False}
    if not text:
        return out
    values = _numbers(text)
    if not values:
        return out
    if any(v > MAX_PLAUSIBLE for v in values):
        out["suspicious"] = True
        return out
    low = text.lower()
    if _HOURLY_RE.search(text):
        out.update(min=min(values), max=max(values), type="hourly")
        return out
    if len(values) >= 2:
        out.update(min=min(values), max=max(values), type="range" if min(values) != max(values) else "fixed")
    elif re.search(r"\bдо\b|\bup to\b|\bmax\b", low):
        out.update(max=values[0], type="max")
    elif re.search(r"\bот\b|\bfrom\b|\bmin\b", low):
        out.update(min=values[0], type="min")
    else:
        out.update(min=values[0], max=values[0], type="fixed")
    return out


def rub_amount(b: dict) -> float:
    """Рублёвая сумма бюджета для сравнения с ценой работы: верхняя граница (для «от N» - нижняя).
    0.0 - сравнивать нельзя (валюта не рубли или неизвестна, сумма не названа, ставка в час, значение испорчено)."""
    if b.get("suspicious") or b.get("type") in ("unknown", "hourly") or b.get("currency") != "RUB":
        return 0.0
    return float(b.get("max") or b.get("min") or 0.0)


def is_corrupted_budget(price_text: str, stored_budget) -> bool:
    """Старая строка с испорченным бюджетом: сумма, сохранённая в БД, не совпадает с разбором текста бюджета
    (диапазон FL.ru когда-то склеивался в миллиарды) либо нереалистично велика."""
    try:
        stored = float(stored_budget or 0)
    except (TypeError, ValueError):
        return True
    if stored > MAX_PLAUSIBLE:
        return True
    parsed = parse_budget(price_text)
    if parsed["suspicious"]:
        return True
    return stored > 0 and parsed["max"] > 0 and abs(parsed["max"] - stored) > 1.0 and stored > parsed["max"] * 1.5
