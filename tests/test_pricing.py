"""Тесты расчёта цены, разбора AI-ответа, миграции БД и форматирования заказа.

Запуск из корня проекта: python -m unittest tests.test_pricing -v
"""
import json
import os
import sqlite3
import tempfile
import unittest
from unittest import mock
from datetime import datetime, timezone

import config
import database
from freelance import deal_wait, pricing
from freelance.ai_scorer import parse_ai_json
from freelance.alot_client import FreelanceOrder
from freelance.pipeline import ScoredOrder, format_order


def ai(hours=4.0, category="tg_bot", clarity="ясно", unknowns=()):
    return {"hours": hours, "category": category, "clarity": clarity, "unknowns": list(unknowns)}


class CalcPriceTest(unittest.TestCase):
    def test_formula_and_spread(self):
        # 4.5 ч × 1000 × 1.15 = 5175 -> 5200, вилка ±15%
        r = pricing.calc_price(ai(4.5, clarity="частично"), rate=1000)
        self.assertEqual(r["price_mid"], 5200)
        self.assertEqual(r["price_low"], 4400)   # 5200 × 0.85 = 4420
        self.assertEqual(r["price_high"], 6000)  # 5200 × 1.15 = 5980
        self.assertIn("4.5 ч", r["price_basis"])
        self.assertIn("1.15", r["price_basis"])

    def test_unknowns_capped(self):
        r = pricing.calc_price(ai(10, clarity="ясно", unknowns=["a"] * 10), rate=1000)
        self.assertAlmostEqual(r["price_risk"], 1.3)
        self.assertEqual(r["price_mid"], 13000)

    def test_no_hours(self):
        self.assertEqual(pricing.calc_price({"hours": 0, "category": "tg_bot"}), {})

    def test_floor(self):
        r = pricing.calc_price(ai(1, "tg_bot"), rate=1000)
        self.assertEqual(r["price_mid"], 3000)  # минимум бота
        self.assertIn("минимум", r["price_basis"])

    def test_unknown_category_is_other(self):
        r = pricing.calc_price(ai(0.5, "что-то"), rate=1000)
        self.assertEqual(r["price_mid"], 1500)

    def test_budget_caps_price(self):
        r = pricing.calc_price(ai(6, "tg_bot"), budget=5000, rate=1000)
        self.assertEqual(r["price_mid"], 5500)   # бюджет × 1.1
        self.assertTrue(r["budget_capped"])
        self.assertFalse(r["budget_low"])        # 6000 < 5000 × 1.5

    def test_budget_low_flag(self):
        r = pricing.calc_price(ai(10, "tg_bot"), budget=3000, rate=1000)
        self.assertTrue(r["budget_low"])         # 10000 > 4500
        # 3300 = 330 ₽/ч - ниже минимальной ставки: предлагаем минимум 10 × 775
        self.assertEqual(r["price_mid"], 7800)
        self.assertFalse(r["budget_capped"])

    def test_budget_above_own_keeps_own(self):
        r = pricing.calc_price(ai(4, "tg_bot"), budget=20000, rate=1000)
        self.assertEqual(r["price_mid"], 4000)
        self.assertFalse(r["budget_capped"])

    def test_market_ignored_below_5_points(self):
        market = {"median": 10000, "p25": 8000, "p75": 12000, "n": 4}
        r = pricing.calc_price(ai(4), market=market, rate=1000)
        self.assertEqual(r["price_mid"], 4000)
        self.assertNotIn("market_low", r)

    def test_market_shift_with_5_points(self):
        market = {"median": 10000, "p25": 8000, "p75": 12000, "n": 5}
        r = pricing.calc_price(ai(4), market=market, rate=1000)
        # 4000 × 0.7 + 10000 × 0.3 = 5800
        self.assertEqual(r["price_mid"], 5800)
        self.assertEqual((r["market_low"], r["market_high"]), (8000, 12000))

    def test_market_not_used_when_budget(self):
        market = {"median": 10000, "p25": 8000, "p75": 12000, "n": 9}
        r = pricing.calc_price(ai(4), budget=20000, market=market, rate=1000)
        self.assertEqual(r["price_mid"], 4000)

    def test_rounding_steps(self):
        r = pricing.calc_price(ai(12, "tg_bot"), rate=1000)  # 12000: шаг 500
        self.assertEqual(r["price_mid"] % 500, 0)
        self.assertEqual(r["price_low"] % 500, 0)


class DealRateTest(unittest.TestCase):
    def test_default_below_5_deals(self):
        deals = [(8000, 4)] * 4
        self.assertEqual(pricing.rate_from_deals(deals, 1000), 1000)

    def test_median_rate_from_5_deals(self):
        # ставки: 2000, 2000, 3000, 2000, 1000 -> медиана 2000
        deals = [(8000, 4), (6000, 3), (9000, 3), (10000, 5), (7000, 7)]
        self.assertEqual(pricing.rate_from_deals(deals, 1000), 2000)

    def test_rate_clamped(self):
        deals = [(100000, 1)] * 5
        self.assertEqual(pricing.rate_from_deals(deals, 1000), 5000)

    def test_rate_changes_price(self):
        r = pricing.calc_price(ai(4), rate=2000)
        self.assertEqual(r["price_mid"], 8000)

    def test_accuracy_report(self):
        rows = [{"category": "tg_bot", "deal_price": 5000, "price_mid": 5500},
                {"category": "tg_bot", "deal_price": 5000, "price_mid": 4500},
                {"category": "parser", "deal_price": 2000, "price_mid": 0}]
        rep = pricing.accuracy_report(rows)
        self.assertEqual(rep["total"], 2)
        self.assertAlmostEqual(rep["by_category"]["tg_bot"]["mape"], 10.0)
        self.assertAlmostEqual(rep["by_category"]["tg_bot"]["bias"], 0.0)


class MinRateTest(unittest.TestCase):
    """Цена не ниже часы × FREELANCE_MIN_RATE_PER_HOUR (775 ₽/ч по умолчанию)."""

    def setUp(self):
        patcher = mock.patch.object(pricing, "FREELANCE_MIN_RATE_PER_HOUR", 775)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_low_rate_raised(self):
        r = pricing.calc_price(ai(10, "other"), rate=300)
        self.assertEqual(r["price_rate"], 775)
        self.assertGreaterEqual(r["price_low"], 7750)

    def test_deal_rate_not_below_min(self):
        deals = [(1000, 4)] * 5                  # 250 ₽/ч
        self.assertEqual(pricing.rate_from_deals(deals, 1000), 775)

    def test_market_does_not_pull_below_min(self):
        market = {"median": 1000, "p25": 800, "p75": 1500, "n": 10}
        r = pricing.calc_price(ai(10, "other"), market=market, rate=775)
        self.assertGreaterEqual(r["price_mid"], 7750)
        self.assertGreaterEqual(r["price_low"], 7750)

    def test_budget_cap_not_below_min(self):
        # бюджет 8000 ≥ минимума 7750: режем до потолка 8800, вилка не ниже минимума
        r = pricing.calc_price(ai(10, "other"), budget=8000, rate=1000)
        self.assertTrue(r["budget_capped"])
        self.assertEqual(r["price_mid"], 8800)
        self.assertGreaterEqual(r["price_low"], 7750)

    def test_budget_below_min_not_capped(self):
        # бюджет 7500 < 10 ч × 775: под него не опускаемся, предлагаем минимум
        r = pricing.calc_price(ai(10, "other"), budget=7500, rate=1000)
        self.assertFalse(r["budget_capped"])
        self.assertTrue(r["budget_low"])
        self.assertEqual(r["price_mid"], 7800)
        self.assertEqual(r["price_low"], 7800)

    def test_no_jump_at_budget_edge(self):
        below = pricing.calc_price(ai(10, "other"), budget=7749, rate=1000)
        above = pricing.calc_price(ai(10, "other"), budget=7750, rate=1000)
        self.assertLessEqual(below["price_mid"], above["price_mid"])

    def test_low_edge_of_range_not_below_min(self):
        r = pricing.calc_price(ai(4, "other"), rate=800)   # 3200, вилка -15% = 2700
        self.assertEqual(r["price_low"], 3100)            # 4 × 775 = 3100


class MarketVerdictTest(unittest.TestCase):
    def test_verdicts(self):
        self.assertEqual(pricing.market_verdict(5000, 4000, 7000), "в рынке")
        self.assertEqual(pricing.market_verdict(8400, 4000, 7000), "выше рынка на 20%")
        self.assertEqual(pricing.market_verdict(3000, 4000, 7000), "ниже рынка на 25%")

    def test_verdict_and_budget_rate_in_result(self):
        market = {"median": 3000, "p25": 2000, "p75": 4000, "n": 8}
        r = pricing.calc_price(ai(10, "other"), budget=3000, market=market, rate=1000)
        self.assertEqual(r["budget_rate"], 300)
        self.assertTrue(r["market_verdict"].startswith("выше рынка"))
        line = _price_basis_line_for(r)
        self.assertIn("цена выше рынка", line)
        self.assertIn("≈ 300 ₽/ч, занижен", line)

    def test_few_market_points_shown(self):
        r = pricing.calc_price(ai(4), market={"median": 5000, "p25": 4000, "p75": 6000, "n": 2}, rate=1000)
        self.assertNotIn("market_verdict", r)
        self.assertIn("мало данных (2 из 5)", _price_basis_line_for(r))


def _price_basis_line_for(price: dict) -> str:
    from freelance.pipeline import _price_basis_line
    return _price_basis_line({**ai(10, "other"), **price})


class ParseAiJsonTest(unittest.TestCase):
    def test_new_format(self):
        text = json.dumps({
            "fit": 8, "difficulty": "средне", "category": "tg_bot",
            "components": [{"name": "меню", "hours": 2}, {"name": "таблица", "hours": 1.5}],
            "unknowns": ["нет API"], "clarity": "частично", "client_budget_ok": True,
            "summary": "s", "risks": "r", "questions": ["есть таблица?"],
            "hours": 99,
        }, ensure_ascii=False)
        r = parse_ai_json("```json\n" + text + "\n```")
        self.assertEqual(r["hours"], 3.5)  # сумма компонентов, не поле hours
        self.assertEqual(r["category"], "tg_bot")
        self.assertEqual(r["clarity"], "частично")
        self.assertIs(r["client_budget_ok"], True)
        self.assertEqual(r["questions"], ["есть таблица?"])

    def test_invalid_values(self):
        comps = [{"name": f"c{i}", "hours": 100 if i == 0 else 0.01} for i in range(12)]
        comps.append("мусор")
        r = parse_ai_json(json.dumps({"fit": 5, "category": "xxx", "components": comps,
                                      "clarity": "?", "client_budget_ok": "да",
                                      "unknowns": ["u" * 500] * 9, "questions": "один"}))
        self.assertEqual(r["category"], "other")
        self.assertEqual(len(r["components"]), 8)
        self.assertEqual(r["components"][0]["hours"], 40.0)
        self.assertTrue(all(c["hours"] >= 0.25 for c in r["components"]))
        self.assertEqual(r["clarity"], "")
        self.assertIsNone(r["client_budget_ok"])
        self.assertEqual(len(r["unknowns"]), 5)
        self.assertEqual(len(r["unknowns"][0]), 120)
        self.assertEqual(r["questions"], ["один"])

    def test_old_format(self):
        r = parse_ai_json('{"fit": 7, "difficulty": "легко", "hours": 5, '
                          '"price_rub": "8-12 тыс.", "summary": "x", "risks": "y"}')
        self.assertEqual(r["hours"], 5.0)
        self.assertEqual(r["price_rub"], "8-12 тыс.")
        self.assertEqual(r["category"], "other")
        self.assertEqual(r["components"], [])

    def test_nan_hours(self):
        self.assertEqual(parse_ai_json('{"fit": 7, "hours": NaN}')["hours"], 0.0)


def make_order(price_value=0.0):
    return FreelanceOrder(1, "youdoru", "Бот <для> салона", "тело", "до 15 000 ₽", price_value, [],
                          datetime.now(timezone.utc), "https://example.com/1")


class FormatOrderTest(unittest.TestCase):
    def test_old_ai_json(self):
        old = {"fit": 7, "difficulty": "легко", "hours": 5, "price_rub": "8-12 тыс.",
               "summary": "s", "risks": "r"}
        text = format_order(ScoredOrder(make_order(), old))
        self.assertIn("предложить 8-12 тыс.", text)
        self.assertNotIn("Предложить:", text)

    def test_new_format(self):
        a = {"fit": 8, "difficulty": "средне", "hours": 4.5, "category": "tg_bot",
             "components": [{"name": "меню <b>", "hours": 2}], "questions": ["есть таблица?"],
             "summary": "s", "risks": "r"}
        a.update(pricing.calc_price(dict(a, clarity="частично", unknowns=[]), budget=15000,
                                    market={"median": 5500, "p25": 4000, "p75": 7000, "n": 6},
                                    rate=1000))
        text = format_order(ScoredOrder(make_order(15000), a))
        self.assertIn("💵 Предложить:", text)
        self.assertIn("рынок ботов: 4–7 тыс.", text)
        self.assertIn("рынок ботов: 4–7 тыс. — цена в рынке", text)
        self.assertIn("бюджет 15 000 ₽ ≈ 3 333 ₽/ч, ок", text)
        self.assertIn("🧩 меню &lt;b&gt; 2 ч", text)
        self.assertIn("❓ Уточнить: есть таблица?", text)
        self.assertIn("Бот &lt;для&gt;", text)
        compact = format_order(ScoredOrder(make_order(15000), a), compact=True)
        self.assertIn("💵 Предложить:", compact)
        self.assertNotIn("🧩", compact)
        self.assertLessEqual(len(text), 4000)


class DatabaseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = database.DB_PATH
        database.DB_PATH = os.path.join(self.tmp.name, "t.db")

    def tearDown(self):
        database.DB_PATH = self.old_path
        self.tmp.cleanup()

    def save(self, oid, **kw):
        database.freelance_save(oid, "youdoru", "t", "u", "p", "2026-01-01T00:00:00+00:00",
                                f"k{oid}", 8, "{}", "sent", **kw)

    def test_migration_from_old_schema(self):
        conn = sqlite3.connect(database.DB_PATH)
        conn.execute("CREATE TABLE freelance_orders (order_id INTEGER PRIMARY KEY, site TEXT, "
                     "title TEXT, url TEXT, price_text TEXT, fit INTEGER, ai_json TEXT, "
                     "published_at TEXT, dedup_key TEXT, "
                     "seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, sent_at TIMESTAMP)")
        conn.execute("INSERT INTO freelance_orders (order_id, title, ai_json) VALUES (1, 'old', '{}')")
        conn.commit()
        conn.close()
        database.init_db()
        database.init_db()  # повторный запуск не падает
        with database._connect() as c:
            cols = {r[1] for r in c.execute("PRAGMA table_info(freelance_orders)")}
            self.assertTrue({"category", "budget", "hours", "deal_price"} <= cols)
            self.assertEqual(c.execute("SELECT title FROM freelance_orders").fetchone()[0], "old")

    def test_market_stats_threshold(self):
        database.init_db()
        for i in range(4):
            self.save(i + 1, category="tg_bot", budget=1000.0 * (i + 1))
        self.assertIsNone(database.market_stats("tg_bot"))
        self.assertEqual(database.market_stats("tg_bot", report_small=True), {"n": 4})
        r = pricing.price_order(ai(4, "tg_bot"))      # через настоящую БД
        self.assertEqual(r["market_n"], 4)
        self.assertNotIn("market_verdict", r)
        self.save(5, category="tg_bot", budget=5000.0)
        self.save(6, category="parser", budget=9999.0)
        self.save(7, category="tg_bot", budget=None)
        st = database.market_stats("tg_bot")
        self.assertEqual(st["n"], 5)
        self.assertEqual(st["median"], 3000)
        self.assertEqual((st["p25"], st["p75"]), (2000, 4000))

    def test_deal_price_and_deals(self):
        database.init_db()
        self.save(1, category="tg_bot", budget=None, hours=4.0)
        self.assertTrue(database.freelance_set_deal_price(1, 8000))
        self.assertFalse(database.freelance_set_deal_price(99, 1))
        self.assertEqual(database.freelance_deals("tg_bot"), [(8000.0, 4.0, 1.0)])
        # повторное сохранение не затирает category/hours
        self.save(1)
        self.assertEqual(database.freelance_deals("tg_bot"), [(8000.0, 4.0, 1.0)])

    def test_cleanup_keeps_budget_rows(self):
        database.init_db()
        self.save(1, category="tg_bot", budget=5000.0)
        self.save(2, category="tg_bot")
        with database._connect() as c:
            c.execute("UPDATE freelance_orders SET seen_at = datetime('now', '-30 days')")
        database.cleanup_old()
        with database._connect() as c:
            ids = [r[0] for r in c.execute("SELECT order_id FROM freelance_orders")]
        self.assertEqual(ids, [1])


class SmallBudgetTest(unittest.TestCase):
    def test_budget_below_floor_not_capping(self):
        # минимум tg_bot 3000, бюджет 500: цену бюджетом не ограничиваем
        r = pricing.calc_price(ai(1), budget=500)
        self.assertGreaterEqual(r["price_mid"], config.FREELANCE_MIN_PRICE["tg_bot"])
        self.assertTrue(r["budget_low"])
        self.assertFalse(r["budget_capped"])

    def test_tiny_budget_invariants(self):
        for budget in (1, 50, 100, 900, 2999):
            for cat in ("tg_bot", "site_fix", "other"):
                r = pricing.calc_price(ai(0.25, category=cat), budget=budget)
                self.assertGreater(r["price_low"], 0)
                self.assertLessEqual(r["price_low"], r["price_mid"])
                self.assertLessEqual(r["price_mid"], r["price_high"])
                self.assertGreaterEqual(r["price_mid"], config.FREELANCE_MIN_PRICE[cat])

    def test_zero_floor_still_positive(self):
        with mock.patch.dict(pricing.FREELANCE_MIN_PRICE, {"tg_bot": 0}):
            r = pricing.calc_price(ai(0.25), budget=10)
        self.assertGreaterEqual(r["price_mid"], 100)
        self.assertGreater(r["price_low"], 0)


class RateRiskTest(unittest.TestCase):
    def test_risk_divided_out(self):
        # цена 6900 за 4 ч при риске 1.15 -> ставка 1500, а не 1725
        deals = [(6900, 4, 1.15)] * 5
        self.assertEqual(pricing.rate_from_deals(deals, 1000), 1500)

    def test_roundtrip_no_double_risk(self):
        a = ai(4, clarity="частично")
        first = pricing.calc_price(a, rate=1000)
        rate = pricing.rate_from_deals([(first["price_mid"], 4, first["price_risk"])] * 5, 1000)
        second = pricing.calc_price(a, rate=rate)
        self.assertAlmostEqual(second["price_mid"], first["price_mid"], delta=100)

    def test_bad_risk_is_one(self):
        self.assertEqual(pricing.rate_from_deals([(8000, 4, 0)] * 5, 1000), 2000)


class DealWaitTest(unittest.TestCase):
    def test_parse_deal(self):
        for text, want in (("5000", 5000), ("5 000 ₽", 5000), ("5000р", 5000),
                           ("5" + chr(0xA0) + "000", 5000), ("abc", None), ("0", None),
                           ("-5", None), ("", None), ("50000000", None),
                           ("٣٣٣", None), ("²", None), ("5.5", None)):
            self.assertEqual(deal_wait.parse_deal(text), want, text)

    def test_match_by_reply(self):
        p = {}
        deal_wait.add(p, 1, 10, 100, now=0)
        deal_wait.add(p, 1, 11, 101, now=0)
        self.assertEqual(deal_wait.find(p, 1, 11, now=1), (1, 11))
        self.assertIsNone(deal_wait.find(p, 1, 99, now=1))  # reply на другое сообщение
        self.assertIsNone(deal_wait.find(p, 1, None, now=1))  # два ожидания - без reply нельзя

    def test_single_without_reply(self):
        p = {}
        deal_wait.add(p, 1, 10, 100, now=0)
        self.assertEqual(deal_wait.find(p, 1, None, now=60), (1, 10))
        self.assertIsNone(deal_wait.find(p, 2, None, now=60))  # другой чат

    def test_no_pending_no_intercept(self):
        self.assertIsNone(deal_wait.find({}, 1, None))

    def test_expiry_and_purge(self):
        p = {}
        deal_wait.add(p, 1, 10, 100, now=0)
        self.assertIsNone(deal_wait.find(p, 1, None, now=deal_wait.DEAL_TTL_SEC + 1))
        self.assertEqual(p, {})

    def test_drop_order(self):
        p = {}
        deal_wait.add(p, 1, 10, 100, now=0)
        deal_wait.add(p, 1, 11, 101, now=0)
        deal_wait.drop_order(p, 100)
        self.assertEqual(list(p), [(1, 11)])


class HoursParseTest(unittest.TestCase):
    def hours(self, value):
        return parse_ai_json(json.dumps({"fit": 5, "hours": value}))["hours"]

    def test_string_hours(self):
        self.assertEqual(self.hours("2-3"), 2.0)
        self.assertEqual(self.hours("2 ч"), 2.0)
        self.assertEqual(self.hours("1,5 часа"), 1.5)
        self.assertEqual(self.hours("много"), 0.0)

    def test_fallback_hours_capped(self):
        self.assertEqual(self.hours(100000), 320.0)

    def test_component_string_hours(self):
        r = parse_ai_json(json.dumps({"fit": 5, "components": [
            {"name": "a", "hours": "2-3"}, {"name": "b", "hours": "1,5 ч"}]}))
        self.assertEqual(r["hours"], 3.5)


class ConfigClampTest(unittest.TestCase):
    def env(self, **kw):
        return mock.patch.dict(os.environ, kw)

    def test_spread(self):
        for raw, want in (("0.2", 0.2), ("0", 0.0), ("0.5", 0.5), ("0.9", 0.15),
                          ("-0.1", 0.15), ("abc", 0.15)):
            with self.env(FREELANCE_PRICE_SPREAD=raw):
                self.assertEqual(config._price_spread(), want, raw)

    def test_rate(self):
        for raw, want in (("1500", 1500.0), ("0", 1000.0), ("-5", 1000.0), ("x", 1000.0)):
            with self.env(FREELANCE_RATE_PER_HOUR=raw):
                self.assertEqual(config._rate_per_hour(), want, raw)

    def test_min_price(self):
        with self.env(FREELANCE_MIN_PRICE_TG_BOT="-100"):
            self.assertEqual(config._min_price("tg_bot", 3000), 3000)
        with self.env(FREELANCE_MIN_PRICE_TG_BOT="0"):
            self.assertEqual(config._min_price("tg_bot", 3000), 0)


class MarketAndCleanupTest(DatabaseTest):
    def put(self, oid, fit=8, dedup=None, budget=1000.0, cat="tg_bot", ai_json="{}"):
        database.freelance_save(oid, "youdoru", "t", "u", "p", "2026-01-01T00:00:00+00:00",
                                dedup or f"k{oid}", fit, ai_json, "sent",
                                category=cat, budget=budget)

    def test_market_ignores_low_fit(self):
        database.init_db()
        for i in range(5):
            self.put(i + 1, fit=config.FREELANCE_MIN_FIT - 1)
        self.assertIsNone(database.market_stats("tg_bot"))
        self.put(6)
        self.assertIsNone(database.market_stats("tg_bot"))  # годных всего 1

    def test_market_one_row_per_dedup(self):
        database.init_db()
        for i in range(5):
            self.put(i + 1, dedup="same")  # один заказ, 5 кросспостов
        self.assertIsNone(database.market_stats("tg_bot"))
        for i in range(4):
            self.put(10 + i, budget=2000.0 * (i + 1))
        st = database.market_stats("tg_bot")
        self.assertEqual(st["n"], 5)

    def test_cleanup_drops_ai_json_of_stat_rows(self):
        database.init_db()
        self.put(1, ai_json='{"x": 1}')
        self.put(2, ai_json='{"x": 2}')
        database.freelance_set_deal_price(2, 5000)
        with database._connect() as c:
            c.execute("UPDATE freelance_orders SET seen_at = datetime('now', '-30 days')")
        database.cleanup_old()
        with database._connect() as c:
            rows = {r[0]: r[1] for r in c.execute("SELECT order_id, ai_json FROM freelance_orders")}
        self.assertEqual(rows, {1: "", 2: '{"x": 2}'})

    def test_deals_skip_budget_capped_and_use_risk(self):
        database.init_db()
        self.put(1, ai_json=json.dumps({"price_risk": 1.15}))
        self.put(2, ai_json=json.dumps({"budget_capped": True}))
        for oid in (1, 2):
            database.freelance_set_deal_price(oid, 5000)
            with database._connect() as c:
                c.execute("UPDATE freelance_orders SET hours = 4 WHERE order_id = ?", (oid,))
        self.assertEqual(database.freelance_deals("tg_bot"), [(5000.0, 4.0, 1.15)])


class ExtraReviewTest(MarketAndCleanupTest):
    def test_inf_config(self):
        with mock.patch.dict(os.environ, {"FREELANCE_RATE_PER_HOUR": "inf"}):
            self.assertEqual(config._rate_per_hour(), 1000.0)
        with mock.patch.dict(os.environ, {"FREELANCE_MIN_PRICE_TG_BOT": "inf"}):
            self.assertEqual(config._min_price("tg_bot", 3000), 3000)
        with mock.patch.dict(os.environ, {"FREELANCE_RATE_PER_HOUR": "nan"}):
            self.assertEqual(config._rate_per_hour(), 1000.0)

    def test_cleanup_low_fit_budget_row_deleted(self):
        database.init_db()
        self.put(1, fit=config.FREELANCE_MIN_FIT)
        self.put(2, fit=config.FREELANCE_MIN_FIT - 1)
        with database._connect() as c:
            c.execute("UPDATE freelance_orders SET seen_at = datetime('now', '-30 days')")
        database.cleanup_old()
        with database._connect() as c:
            ids = [r[0] for r in c.execute("SELECT order_id FROM freelance_orders")]
        self.assertEqual(ids, [1])

    def test_ambiguous(self):
        p = {}
        self.assertFalse(deal_wait.ambiguous(p, 1, None))
        deal_wait.add(p, 1, 10, 100, now=0)
        self.assertFalse(deal_wait.ambiguous(p, 1, None, now=1))
        deal_wait.add(p, 1, 11, 101, now=0)
        self.assertTrue(deal_wait.ambiguous(p, 1, None, now=1))
        self.assertFalse(deal_wait.ambiguous(p, 1, 10, now=1))  # есть reply
        self.assertFalse(deal_wait.ambiguous(p, 2, None, now=1))  # другой чат
        self.assertFalse(deal_wait.ambiguous(p, 1, None, now=deal_wait.DEAL_TTL_SEC + 1))


if __name__ == "__main__":
    unittest.main()
