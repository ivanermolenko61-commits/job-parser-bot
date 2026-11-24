import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from unittest import mock

from freelance import ai_scorer, paid_budget
from tests.test_pipeline_cycle import CycleTestBase, make_order


class PaidBudgetTest(CycleTestBase):
    NOW = datetime(2026, 11, 23, 20, tzinfo=timezone.utc)
    BODY = {"messages": [{"role": "user", "content": "Оцени заказ"}], "max_tokens": 2500}

    def setUp(self):
        super().setUp()
        env = mock.patch.dict(os.environ, {"YANDEX_PAID_PILOT": "1", "YANDEX_PAID_PILOT_START": "2026-11-23"})
        env.start()
        self.addCleanup(env.stop)

    def reserve(self):
        return paid_budget.reserve(paid_budget.MODEL, self.BODY, self.NOW)

    def test_parallel_reservations_fit_month_limit_and_survive_unknown_usage(self):
        budget = mock.patch.object(paid_budget, "MONTHLY", 15 * paid_budget.RUB)
        budget.start()
        self.addCleanup(budget.stop)
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.reserve(), range(8)))
        tokens = [token for token in results if token]
        self.assertEqual(len(tokens), 3)
        paid_budget.finish(tokens[0])
        self.assertIsNone(self.reserve())
        self.assertAlmostEqual(paid_budget.summary(self.NOW)["held_rub"], 11.3304)
        paid_budget.finish(tokens[0], {"prompt_tokens": 1000, "completion_tokens": 100})
        self.assertIsNotNone(self.reserve())
        self.assertLessEqual(paid_budget.summary(self.NOW)["month_rub"], 15)

    def test_disabled_outside_window_wrong_model_and_oversized_body(self):
        with mock.patch.dict(os.environ, {"YANDEX_PAID_PILOT": "0"}):
            self.assertIsNone(self.reserve())
        self.assertIsNone(paid_budget.reserve(paid_budget.MODEL, self.BODY, self.NOW.replace(day=22)))
        self.assertIsNone(paid_budget.reserve("other", self.BODY, self.NOW))
        self.assertIsNone(paid_budget.reserve(paid_budget.MODEL, dict(self.BODY, max_tokens=3000), self.NOW))
        self.assertIsNone(paid_budget.reserve(paid_budget.MODEL, dict(self.BODY, messages=["я" * 10_000]), self.NOW))

    def test_overrun_fails_closed_and_start_cannot_be_reset(self):
        token = self.reserve()
        with mock.patch.dict(os.environ, {"YANDEX_PAID_PILOT_START": "2026-11-22"}):
            self.assertIsNone(self.reserve())
        with self.assertLogs(level="ERROR"):
            paid_budget.finish(token, {"prompt_tokens": 10**26, "completion_tokens": 100})
        self.assertIsNone(self.reserve())
        self.assertFalse(paid_budget.summary(self.NOW)["active"])

    def test_monthly_limit_resets_at_moscow_midnight(self):
        with mock.patch.object(paid_budget, "MONTHLY", 7 * paid_budget.RUB):
            self.assertIsNotNone(self.reserve())
            self.assertIsNone(self.reserve())
            next_month = self.NOW.replace(day=30, hour=21)
            self.assertIsNotNone(paid_budget.reserve(paid_budget.MODEL, self.BODY, next_month))


class LiveScorerTest(CycleTestBase):
    def test_paid_fallback_skipped_when_free_slot_temporarily_busy(self):
        def attempt(provider, order, deadline, *, window):
            self.assertEqual(provider.name, "cloudflare")
            window.free_busy = True
            return None

        providers = {n: mock.Mock(name=n) for n in ("cloudflare", "yandex")}
        providers["cloudflare"].name = "cloudflare"
        with mock.patch.object(ai_scorer, "PROVIDERS", providers), \
                mock.patch.object(ai_scorer, "_chain_active", return_value=["yandex", "cloudflare"]), \
                mock.patch.object(paid_budget, "active", return_value=True), \
                mock.patch.object(ai_scorer, "_ask_provider", side_effect=attempt) as ask:
            self.assertIsNone(ai_scorer.score_order(make_order(1), live=True))
        self.assertEqual(ask.call_count, 1)

    def test_live_chain_caps_posts_across_providers(self):
        attempted = []
        providers = {n: mock.Mock() for n in ("openrouter", "cloudflare", "github")}
        for name, provider in providers.items():
            provider.name = name

        def attempt(provider, order, deadline, *, window):
            self.assertGreater(deadline, time.monotonic())
            self.assertTrue(window.allows_post())
            window.attempts += 1
            attempted.append(provider.name)
            return None

        with mock.patch.object(ai_scorer, "PROVIDERS", providers), \
                mock.patch.object(ai_scorer, "_chain_active", return_value=list(providers)), \
                mock.patch.object(ai_scorer, "_ask_provider", side_effect=attempt):
            self.assertIsNone(ai_scorer.score_order(make_order(1), live=True))
        self.assertEqual(attempted, ["openrouter", "cloudflare"])
