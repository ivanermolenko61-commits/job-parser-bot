"""Пауза автоматических HTTP-запросов в момент наступления 22:00 МСК."""
import threading
import time
import unittest
from unittest import mock

from freelance import ai_scorer, paid_budget, schedule
from tests.test_providers import ORDER, resp


class AiScheduleTest(unittest.TestCase):
    def test_transport_rechecks_schedule_and_returns_unused_paid_reservation(self):
        provider = ai_scorer._build_providers({})["yandex"]
        caller = threading.current_thread()
        window = ai_scorer._LiveWindow(time.monotonic() + 20)
        with (schedule.automatic_requests(),
              mock.patch.object(schedule, "automatic_open", side_effect=lambda: threading.current_thread() is caller),
              mock.patch.object(ai_scorer, "_reserve_slot", return_value=0),
              mock.patch.object(paid_budget, "reserve", return_value="ticket") as reserve,
              mock.patch.object(paid_budget, "finish") as finish,
              mock.patch.object(ai_scorer, "_give_back_slot") as cancel,
              mock.patch("aiohttp.ClientSession.post") as post):
            result = ai_scorer._ask_provider_model(provider, paid_budget.MODEL, ORDER, window=window)
        self.assertEqual(result, "stop")
        self.assertEqual(window.attempts, 0)
        post.assert_not_called()
        reserve.assert_called_once()
        finish.assert_called_once_with("ticket", definitely_not_sent=True)
        cancel.assert_called_once_with(provider, live=True)

    def test_provider_does_not_retry_after_nightfall(self):
        provider = ai_scorer._build_providers({})["groq"]
        with (schedule.automatic_requests(),
              mock.patch.object(schedule, "automatic_open", return_value=True) as daytime,
              mock.patch.object(ai_scorer, "_reserve_slot", return_value=0) as reserve,
              mock.patch("requests.post") as post):
            def unsupported_format(*args, **kwargs):
                daytime.return_value = False
                return resp(400, text="response_format not supported")

            post.side_effect = unsupported_format
            result = ai_scorer._ask_provider_model(provider, provider.models[0], ORDER)
        self.assertEqual(result, "stop")
        reserve.assert_called_once()
        post.assert_called_once()

    def test_gemini_rechecks_after_waiting_for_a_slot(self):
        with (schedule.automatic_requests(),
              mock.patch.object(schedule, "automatic_open", return_value=True) as daytime,
              mock.patch.object(ai_scorer, "_throttle", side_effect=lambda: setattr(daytime, "return_value", False)),
              mock.patch("requests.post") as post):
            ai_scorer._ask_gemini_model("gemini", ORDER)
        post.assert_not_called()

    def test_triage_rechecks_after_preparing_request_and_restores_count(self):
        provider = ai_scorer._build_providers({"CLOUDFLARE_API_KEY": "test", "CLOUDFLARE_ACCOUNT_ID": "test"})
        with (schedule.automatic_requests(),
              mock.patch.object(ai_scorer, "PROVIDERS", provider),
              mock.patch.object(ai_scorer, "CHAIN", ["cloudflare"]),
              mock.patch.object(ai_scorer, "_triage_loaded", True),
              mock.patch.object(ai_scorer, "_triage_day", ai_scorer._today()),
              mock.patch.object(ai_scorer, "_triage_disabled", False),
              mock.patch.object(ai_scorer, "_triage_paused_until", 0),
              mock.patch.object(ai_scorer, "_triage_count", 0),
              mock.patch.object(ai_scorer, "_triage_save_count") as save,
              mock.patch.object(schedule, "automatic_open", side_effect=[True, False]),
              mock.patch("requests.post") as post):
            self.assertIsNone(ai_scorer.triage_order(ORDER))
            self.assertEqual(ai_scorer._triage_count, 0)
            self.assertEqual(save.call_args.args[0], f"{ai_scorer._today()}:0")
        post.assert_not_called()
