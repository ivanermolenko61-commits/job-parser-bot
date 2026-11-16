"""Ф07: бюджет и валюта (разбор, курсы), цена полного объёма независимо от бюджета, сравнение бюджета с ценой,
ложная точность, калибровка часов.

Запуск из корня проекта: python -m unittest tests.test_money -v
"""
import unittest
from datetime import date
from unittest import mock

from freelance import fx, pricing
from freelance.budget import is_corrupted_budget, parse_budget, rub_amount

TODAY = date(2026, 11, 16)
USD = {"USD": {"rate": 90.0, "source": "ЦБ РФ", "date": "2026-11-15"}}


def ai(hours=4.0, category="tg_bot", clarity="ясно", unknowns=(), components=None):
    out = {"hours": hours, "category": category, "clarity": clarity, "unknowns": list(unknowns)}
    if components is not None:
        out["components"] = components
    return out


class ParseBudgetTest(unittest.TestCase):
    def test_types_and_amounts(self):
        cases = [
            ("5 000 ₽ – 10 000 ₽", "range", 5000, 10000, "RUB"),
            ("до 5000 руб.", "max", 0, 5000, "RUB"),
            ("от 3000 ₽", "min", 3000, 0, "RUB"),
            ("3000 ₽", "fixed", 3000, 3000, "RUB"),
            ("5-10 тыс. руб", "range", 5000, 10000, "RUB"),
            ("50к ₽", "fixed", 50000, 50000, "RUB"),
            ("2 млн ₽", "fixed", 2000000, 2000000, "RUB"),
            ("500 ₽/час", "hourly", 500, 500, "RUB"),
            ("100 $", "fixed", 100, 100, "USD"),
            ("120 грн", "fixed", 120, 120, "UAH"),
        ]
        for text, kind, low, high, cur in cases:
            b = parse_budget(text)
            self.assertEqual((b["type"], b["min"], b["max"], b["currency"]), (kind, low, high, cur), text)

    def test_no_amount_is_unknown(self):
        for text in ("", "по договорённости", "Договорная"):
            b = parse_budget(text)
            self.assertEqual((b["type"], b["min"], b["max"]), ("unknown", 0.0, 0.0), text)

    def test_currency_never_defaults_to_rubles(self):
        for text in ("1000", "50 тыс", "от 3000"):
            b = parse_budget(text)
            self.assertEqual(b["currency"], "", text)
            self.assertEqual(rub_amount(b), 0.0, text)

    def test_currency_word_inside_other_word_is_not_rubles(self):
        self.assertEqual(parse_budget("договор. 5000")["currency"], "")
        self.assertEqual(parse_budget("5000 р.")["currency"], "RUB")

    def test_hint_never_overrides_sign_in_text(self):
        self.assertEqual(parse_budget("500 $", "RUB")["currency"], "")
        self.assertEqual(rub_amount(parse_budget("500 $", "RUB")), 0.0)
        self.assertEqual(parse_budget("1000 ₽ или 20 $", "RUB")["currency"], "")
        self.assertEqual(parse_budget("1000 ₽", "RUB")["currency"], "RUB")
        self.assertEqual(parse_budget("1000", "RUB")["currency"], "RUB")

    def test_unlisted_currency_is_unknown_even_with_hint(self):
        for text in ("100 £", "5000 zł", "300 юаней", "5000 ₽ или 100 £", "до 50 CNY", "500 ₱", "2000 ₹", "100 ¥",
                     "10 ₿ ", "3 ₾"):
            for hint in ("", "RUB"):
                b = parse_budget(text, hint)
                self.assertEqual(b["currency"], "", (text, hint))
                self.assertEqual(rub_amount(b), 0.0, (text, hint))

    def test_two_currencies_are_ambiguous(self):
        b = parse_budget("1000 ₽ или 20 $")
        self.assertEqual(b["currency"], "")
        self.assertEqual(rub_amount(b), 0.0)

    def test_unknown_hint_does_not_guess_rubles(self):
        self.assertEqual(parse_budget("100", "XYZ")["currency"], "")
        self.assertEqual(parse_budget("100", "eur")["currency"], "EUR")

    def test_foreign_and_hourly_are_not_comparable_rubles(self):
        self.assertEqual(rub_amount(parse_budget("100 $")), 0.0)
        self.assertEqual(rub_amount(parse_budget("500 ₽/час")), 0.0)
        self.assertEqual(rub_amount(parse_budget("до 5000 ₽")), 5000.0)

    def test_glued_range_is_suspicious_and_excluded(self):
        b = parse_budget("500010000 ₽")
        self.assertTrue(b["suspicious"])
        self.assertEqual(rub_amount(b), 0.0)
        self.assertTrue(is_corrupted_budget("5 000 ₽ – 10 000 ₽", 500010000))
        self.assertFalse(is_corrupted_budget("5 000 ₽ – 10 000 ₽", 10000))


class FxTest(unittest.TestCase):
    def test_rate_needs_source_and_date(self):
        self.assertEqual(fx.load_rates({"USD": {"rate": 90}}), {})
        self.assertEqual(fx.load_rates({"USD": {"rate": 90, "source": "ЦБ"}}), {})
        self.assertEqual(fx.load_rates({"USD": {"rate": 90, "date": "2026-11-15"}}), {})
        self.assertEqual(fx.load_rates({"USD": {"rate": 0, "source": "ЦБ", "date": "2026-11-15"}}), {})
        self.assertEqual(fx.load_rates("не json"), {})
        self.assertIn("USD", fx.load_rates('{"usd": {"rate": 90, "source": "ЦБ", "date": "2026-11-15"}}'))

    def test_to_rub_returns_source_date_rate(self):
        r = fx.to_rub(100, "USD", USD, TODAY)
        self.assertEqual((r["rub"], r["rate"], r["source"], r["date"]), (9000.0, 90.0, "ЦБ РФ", "2026-11-15"))

    def test_stale_future_missing_or_unknown_currency_gives_none(self):
        self.assertIsNone(fx.to_rub(100, "USD", USD, date(2026, 12, 31)))      # курс устарел
        self.assertIsNone(fx.to_rub(100, "USD", USD, date(2026, 11, 1)))       # дата курса позже «сегодня»
        self.assertIsNone(fx.to_rub(100, "EUR", USD, TODAY))                   # курса нет
        self.assertIsNone(fx.to_rub(100, "", USD, TODAY))                      # валюта неизвестна
        self.assertIsNone(fx.to_rub(100, "USD", None, TODAY))
        self.assertEqual(fx.to_rub(100, "RUB", None, TODAY)["rub"], 100.0)


class IndependentPriceTest(unittest.TestCase):
    def test_same_task_same_price_for_any_budget(self):
        base = pricing.calc_price(ai(4), rate=1000)
        for text in ("1000 ₽", "до 2 000 ₽", "50 000 ₽", "", "100 $", "по договорённости", "500 ₽/час", "от 99999 ₽"):
            r = pricing.calc_price(ai(4), parse_budget(text), rate=1000)
            for key in ("price_low", "price_mid", "price_high", "price_full", "price_min"):
                self.assertEqual(r[key], base[key], (text, key))

    def test_budget_in_price_order_does_not_move_price(self):
        with mock.patch("database.market_stats", return_value=None), mock.patch("database.freelance_deals", return_value=[]):
            prices = {t: pricing.price_order(ai(4), parse_budget(t)) for t in ("1000 ₽", "90000 ₽", "")}
        self.assertEqual({p["price_mid"] for p in prices.values()}, {prices["1000 ₽"]["price_mid"]})
        self.assertEqual(prices["1000 ₽"]["budget_status"], "below_min")
        self.assertEqual(prices["90000 ₽"]["budget_status"], "ok")
        self.assertEqual(prices[""]["budget_status"], "none")

    def test_market_is_reference_only(self):
        market = {"median": 1000, "p25": 800, "p75": 1500, "n": 10}
        without = pricing.calc_price(ai(10, "other"), rate=1000)
        with_market = pricing.calc_price(ai(10, "other"), market=market, rate=1000)
        self.assertEqual(without["price_mid"], with_market["price_mid"])
        self.assertEqual(with_market["market_verdict"], "выше рынка на %d%%" % round((with_market["price_mid"] / 1500 - 1) * 100))

    def test_formula_includes_stage_overhead(self):
        with mock.patch.object(pricing, "FREELANCE_OVERHEAD_SHARE", 0.2):
            r = pricing.calc_price(ai(5, "sheets", clarity="ясно"), rate=1000)
        # 5 ч + 20% = 6 ч × 1000 × 1.0
        self.assertEqual(r["price_mid"], 6000)
        self.assertEqual(r["price_overhead_hours"], 1.0)
        self.assertIn("проверка", r["price_basis"])

    def test_no_double_overhead_when_stages_are_components(self):
        comps = [{"name": "разработка", "hours": 3}, {"name": "проверка на данных клиента", "hours": 1},
                 {"name": "передача результата", "hours": 1}]
        with mock.patch.object(pricing, "FREELANCE_OVERHEAD_SHARE", 0.2):
            r = pricing.calc_price(ai(5, "sheets", components=comps), rate=1000)
        self.assertEqual(r["price_overhead_hours"], 0.0)
        self.assertEqual(r["price_mid"], 5000)

    def test_product_feature_is_not_a_work_stage(self):
        comps = [{"name": "разработка бота для проверки орфографии", "hours": 5}]
        with mock.patch.object(pricing, "FREELANCE_OVERHEAD_SHARE", 0.2):
            r = pricing.calc_price(ai(5, "tg_bot", components=comps), rate=1000)
        self.assertEqual(r["price_overhead_hours"], 1.0)

    def test_one_stage_component_is_not_enough(self):
        comps = [{"name": "разработка", "hours": 4}, {"name": "проверка результата", "hours": 1}]
        with mock.patch.object(pricing, "FREELANCE_OVERHEAD_SHARE", 0.2):
            r = pricing.calc_price(ai(5, "tg_bot", components=comps), rate=1000)
        self.assertGreater(r["price_overhead_hours"], 0)

    def test_min_price_is_floor_and_never_above_full(self):
        r = pricing.calc_price(ai(1, "tg_bot"), rate=1000)
        self.assertEqual(r["price_min"], 3000)
        self.assertLessEqual(r["price_min"], r["price_mid"])
        self.assertLessEqual(r["price_low"], r["price_mid"])
        self.assertLessEqual(r["price_mid"], r["price_high"])

    def test_hours_range_and_calendar_not_mixed(self):
        r = pricing.calc_price(ai(10, "script", clarity="частично", unknowns=["доступ"]), rate=1000)
        self.assertLess(r["hours_low"], r["hours_high"])
        self.assertNotIn("days", r)


class VagueSpecTest(unittest.TestCase):
    def test_vague_has_wider_range_and_conditions(self):
        clear = pricing.calc_price(ai(10, "other"), rate=1000)
        vague = pricing.calc_price(ai(10, "other", clarity="размыто", unknowns=["доступ к API", "макеты"]), rate=1000)
        self.assertFalse(clear["price_vague"])
        self.assertTrue(vague["price_vague"])
        self.assertGreater(vague["price_high"] - vague["price_low"], (clear["price_high"] - clear["price_low"]) * 1.5)
        self.assertEqual(vague["price_conditions"], ["доступ к API", "макеты"])

    def test_many_unknowns_make_estimate_vague_even_if_text_clear(self):
        r = pricing.calc_price(ai(6, "script", clarity="ясно", unknowns=["a", "b", "c"]), rate=1000)
        self.assertTrue(r["price_vague"])

    def test_empty_unknowns_are_not_counted(self):
        r = pricing.calc_price(ai(6, "script", clarity="ясно", unknowns=["", "  ", "x"]), rate=1000)
        self.assertFalse(r["price_vague"])
        self.assertEqual(r["price_conditions"], ["x"])


class CompareBudgetTest(unittest.TestCase):
    def setUp(self):
        self.price = pricing.calc_price(ai(10, "other"), rate=1000)   # mid ~12000, min ~ 8000

    def cmp(self, text, rates=None):
        return pricing.compare_budget(self.price, parse_budget(text), rates=rates or {}, today=TODAY)

    def test_statuses(self):
        self.assertEqual(self.cmp("200 000 ₽")["budget_status"], "ok")
        self.assertEqual(self.cmp(f"{self.price['price_mid']} ₽")["budget_status"], "ok")
        self.assertEqual(self.cmp(f"{self.price['price_min']} ₽")["budget_status"], "below_full")
        self.assertEqual(self.cmp("1000 ₽")["budget_status"], "below_min")
        self.assertEqual(self.cmp("")["budget_status"], "none")
        self.assertEqual(self.cmp("от 1000 ₽")["budget_status"], "open_low")
        self.assertEqual(self.cmp("500010000 ₽")["budget_status"], "suspicious")

    def test_low_budget_shows_mismatch_not_lower_price(self):
        low = self.cmp("1000 ₽")
        self.assertIn("не снижать", low["budget_note"])
        self.assertEqual(self.price["price_mid"], pricing.calc_price(ai(10, "other"), rate=1000)["price_mid"])

    def test_missing_budget_is_not_bad_profile(self):
        self.assertIn("не признак плохого заказа", self.cmp("")["budget_note"])

    def test_unknown_currency_is_not_rubles(self):
        r = self.cmp("100")
        self.assertEqual(r["budget_status"], "uncertain_currency")
        self.assertIsNone(r["budget_rub"])

    def test_foreign_currency_without_reliable_rate_is_uncertain(self):
        for rates in ({}, {"USD": {"rate": 90, "source": "", "date": "2026-11-15"}},
                      {"USD": {"rate": 90, "source": "ЦБ РФ", "date": "2026-01-01"}}):
            r = self.cmp("100000 $", rates)
            self.assertEqual(r["budget_status"], "uncertain_fx", rates)
            self.assertIsNone(r["budget_rub"])

    def test_foreign_currency_with_rate_reports_source_and_date(self):
        r = self.cmp("1000 $", USD)
        self.assertEqual(r["budget_status"], "ok")           # 90 000 ₽
        self.assertEqual(r["budget_rub"], 90000)
        self.assertEqual((r["budget_fx"]["source"], r["budget_fx"]["date"], r["budget_fx"]["rate"]),
                         ("ЦБ РФ", "2026-11-15", 90.0))
        self.assertEqual(self.cmp("10 $", USD)["budget_status"], "below_min")   # 900 ₽: не путать с 10 рублями

    def test_ruble_budget_needs_no_rate(self):
        self.assertIsNone(self.cmp("200 000 ₽")["budget_fx"])

    def test_hourly_compared_with_rate_not_with_price(self):
        r = self.cmp("500 ₽/час")
        self.assertEqual((r["budget_status"], r["hourly_level"]), ("hourly", "below_min"))
        self.assertIsNone(r["budget_rub"])
        self.assertEqual(self.cmp("1500 ₽/час")["hourly_level"], "ok")
        self.assertEqual(self.cmp("30 $/час", USD)["hourly_level"], "ok")             # 2 700 ₽/ч
        self.assertEqual(self.cmp("30 $/час")["budget_status"], "uncertain_fx")

    def test_range_uses_upper_bound(self):
        self.assertEqual(self.cmp("5 000 – 200 000 ₽")["budget_status"], "ok")


class HoursCalibrationTest(unittest.TestCase):
    def rows(self, n, est=4.0, actual=6.0, category="script"):
        return [{"category": category, "hours_est": est, "hours_actual": actual} for _ in range(n)]

    def test_small_sample_gives_no_correction(self):
        self.assertEqual(pricing.hours_calibration(self.rows(4)), {})

    def test_median_ratio_from_five_points(self):
        r = pricing.hours_calibration(self.rows(5))
        self.assertEqual(r["script"], {"n": 5, "ratio": 1.5})

    def test_zero_or_missing_values_are_skipped(self):
        rows = self.rows(5) + [{"category": "script", "hours_est": 0, "hours_actual": 3},
                               {"category": "script", "hours_est": 3, "hours_actual": None}]
        self.assertEqual(pricing.hours_calibration(rows)["script"]["n"], 5)


if __name__ == "__main__":
    unittest.main()
