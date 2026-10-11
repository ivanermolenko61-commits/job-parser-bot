"""Бюджет гранта Yandex Cloud: дата отключения, равномерный расход, потолки. Без сети.

Запуск из корня проекта: python -m unittest tests.test_yandex_budget -v
"""
import os
import tempfile
import unittest
from datetime import date, datetime
from unittest import mock

import database
from freelance import yandex_budget as yb

MODEL = "aliceai-llm-flash"
MAX_TOKENS = 2500


def at(day: str) -> datetime:
    return datetime.fromisoformat(f"{day}T12:00:00").replace(tzinfo=yb.MSK)


class BudgetTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old_path = database.DB_PATH
        database.DB_PATH = os.path.join(self.tmp.name, "t.db")
        database.init_db()
        self.addCleanup(setattr, database, "DB_PATH", self.old_path)
        for p in (mock.patch.object(yb, "_state", None),
                  mock.patch.object(yb, "_pending", 0.0),
                  mock.patch.object(yb, "_load_error_logged", 0.0),
                  mock.patch.dict(os.environ, {}, clear=False)):
            p.start()
            self.addCleanup(p.stop)
        os.environ.pop("YANDEX_UNTIL", None)
        os.environ.pop("YANDEX_BUDGET_RUB", None)

    def spend(self, rub: float, day: str):
        """Списать примерно rub рублей ответами с usage (по 10 000 входящих токенов = 1 ₽)."""
        while rub > 1e-9:
            part = min(rub, 1.0)
            reserved = yb.reserve(MODEL, MAX_TOKENS, now=at(day))
            self.assertIsNotNone(reserved)
            yb.settle(MODEL, reserved, {"prompt_tokens": part * 10000, "completion_tokens": 0}, now=at(day))
            rub -= part


class DatesTest(BudgetTestBase):
    def test_days_left_and_expiry(self):
        self.assertEqual(yb.days_left(at("2026-10-11")), 40)
        self.assertFalse(yb.expired(at("2026-11-19")))
        self.assertTrue(yb.expired(at("2026-11-20")))
        self.assertIsNone(yb.reserve(MODEL, MAX_TOKENS, now=at("2026-11-20")))

    def test_until_cannot_be_moved_later(self):
        os.environ["YANDEX_UNTIL"] = "2027-01-01"
        self.assertEqual(yb.until(), date(2026, 11, 20))
        os.environ["YANDEX_UNTIL"] = "2026-11-01"
        self.assertEqual(yb.until(), date(2026, 11, 1))
        os.environ["YANDEX_UNTIL"] = "мусор"
        self.assertEqual(yb.until(), date(2026, 11, 20))

    def test_budget_cannot_exceed_default(self):
        os.environ["YANDEX_BUDGET_RUB"] = "100000"
        self.assertEqual(yb.budget(), yb.DEFAULT_BUDGET_RUB)
        os.environ["YANDEX_BUDGET_RUB"] = "1000"
        self.assertEqual(yb.budget(), 1000.0)


class PacingTest(BudgetTestBase):
    def test_daily_allowance_is_even(self):
        info = yb.summary(at("2026-10-11"))
        self.assertAlmostEqual(info["allowance"], 3300 / 40, places=2)

    def test_day_limit_blocks_and_next_day_resumes(self):
        os.environ["YANDEX_BUDGET_RUB"] = "440"          # 11 ₽ в день на 40 дней
        self.spend(10, "2026-10-11")
        self.assertIsNone(yb.reserve(MODEL, MAX_TOKENS, now=at("2026-10-11")))   # худший запрос 1.1 ₽ не влезет
        self.assertFalse(yb.can_spend_today(MODEL, MAX_TOKENS, now=at("2026-10-11")))
        self.assertIsNotNone(yb.reserve(MODEL, MAX_TOKENS, now=at("2026-10-12")))

    def test_unspent_rolls_over(self):
        os.environ["YANDEX_BUDGET_RUB"] = "400"
        # 11.10 ничего не тратили: 12.10 лимит = 400 / 39
        self.assertAlmostEqual(yb.summary(at("2026-10-12"))["allowance"], 400 / 39, places=2)

    def test_rollover_capped_at_two_even_days(self):
        os.environ["YANDEX_BUDGET_RUB"] = "400"          # ровная доля - 10 ₽ в день
        # последний день, ничего не тратили: остаток 400, но в день не больше 20 ₽
        self.assertAlmostEqual(yb.summary(at("2026-11-19"))["allowance"], 20, places=2)

    def test_total_budget_cap(self):
        os.environ["YANDEX_BUDGET_RUB"] = "400"
        with mock.patch.object(yb, "_state", {"total": 399.5, "day": "2026-10-11", "day_spent": 0.0}):
            self.assertIsNone(yb.reserve(MODEL, MAX_TOKENS, now=at("2026-10-11")))
            self.assertTrue(yb.summary(at("2026-10-11"))["exhausted"])   # самый дешёвый запрос не влезет

    def test_pending_reservations_count(self):
        os.environ["YANDEX_BUDGET_RUB"] = "400"          # 10 ₽ в день, худший запрос 1.1 ₽
        got = [yb.reserve(MODEL, MAX_TOKENS, now=at("2026-10-11")) for _ in range(12)]
        self.assertEqual(sum(r is not None for r in got), 9)   # 9 * 1.1 = 9.9 <= 10
        for r in got:
            if r is not None:
                yb.release(r)
        self.assertEqual(yb._pending, 0.0)

    def test_unknown_model_never_reserved(self):
        self.assertIsNone(yb.reserve("gpt-oss-120b", MAX_TOKENS, now=at("2026-10-11")))


class CostTest(BudgetTestBase):
    def test_usage_cost_with_cache(self):
        usage = {"prompt_tokens": 3000, "completion_tokens": 400,
                 "prompt_tokens_details": {"cached_tokens": 1000}}
        self.assertAlmostEqual(yb.usage_cost(MODEL, usage), 0.305)
        self.assertAlmostEqual(yb.usage_cost("aliceai-llm", usage), (2000 * 0.5 + 1000 * 0.5 + 400 * 1.2) / 1000)

    def test_bad_usage(self):
        self.assertIsNone(yb.usage_cost(MODEL, None))
        self.assertIsNone(yb.usage_cost(MODEL, {"prompt_tokens": "x"}))
        self.assertIsNone(yb.usage_cost(MODEL, {}))

    def test_settle_without_usage_charges_reserve(self):
        reserved = yb.reserve(MODEL, MAX_TOKENS, now=at("2026-10-11"))
        self.assertAlmostEqual(yb.settle(MODEL, reserved, None, now=at("2026-10-11")), reserved)


class PersistenceTest(BudgetTestBase):
    def test_restart_keeps_spent(self):
        self.spend(5, "2026-10-11")
        yb._state = None                                   # «рестарт процесса»
        info = yb.summary(at("2026-10-11"))
        self.assertAlmostEqual(info["total"], 5, places=2)
        self.assertAlmostEqual(info["today"], 5, places=2)
        self.assertAlmostEqual(yb.summary(at("2026-10-12"))["today"], 0)

    def test_broken_kv_value_fails_closed(self):
        database.kv_set(yb.KV_KEY, "не json")
        with self.assertLogs(level="ERROR"):
            self.assertIsNone(yb.reserve(MODEL, MAX_TOKENS, now=at("2026-10-11")))
            self.assertFalse(yb.can_spend_today(MODEL, MAX_TOKENS, now=at("2026-10-11")))
            self.assertTrue(yb.summary(at("2026-10-11"))["unknown"])
        self.assertEqual(database.kv_get(yb.KV_KEY), "не json")   # настоящий учёт не затёрт

    def test_db_error_fails_closed_then_recovers(self):
        self.spend(5, "2026-10-11")
        yb._state = None
        with mock.patch("database.kv_get", side_effect=OSError("database is locked")),                 self.assertLogs(level="ERROR"):
            self.assertIsNone(yb.reserve(MODEL, MAX_TOKENS, now=at("2026-10-11")))
        self.assertIsNone(yb._state)                                  # сбой не закеширован
        self.assertAlmostEqual(yb.summary(at("2026-10-11"))["total"], 5, places=2)


if __name__ == "__main__":
    unittest.main()
