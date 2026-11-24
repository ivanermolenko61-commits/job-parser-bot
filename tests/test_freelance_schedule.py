import asyncio
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

os.environ.setdefault("BOT_TOKEN", "123456:TEST")

import bot
import database
from freelance import pipeline, schedule, sources
from tests.test_pipeline_cycle import CycleTestBase, ai_result, make_order


class MoscowClockTest(unittest.TestCase):
    def test_boundaries_in_utc(self):
        for h, m, s, expected in [(3, 59, 59, False), (4, 0, 0, True),
                                   (18, 59, 59, True), (19, 0, 0, False)]:
            with self.subTest(hour=h, minute=m):
                now = datetime(2026, 11, 24, h, m, s, tzinfo=timezone.utc)
                self.assertEqual(schedule.automatic_open(now), expected)


class ScheduleJobsTest(CycleTestBase):
    def test_bulk_check_at_night_services_only_explicit_manual_order(self):
        public_id, manual_id = 20, sources.MANUAL_ID_OFFSET + 1
        pipeline._intake([make_order(public_id), make_order(manual_id, "Разработка интеграции CRM и кассы")],
                         0, set(), live=True)
        database._update_states(manual_id, origin="manual")
        calls = []

        def score(order, **kwargs):
            self.assertTrue(schedule.request_allowed())
            calls.append(order.order_id)
            return ai_result(9)

        async def run():
            await bot._pump_freelance_ai(force=True)
            await asyncio.gather(*bot._live_ai_tasks.values())
            await asyncio.gather(*(t for t in asyncio.all_tasks() if t is not asyncio.current_task()))

        with mock.patch.object(schedule, "automatic_open", return_value=False), \
                mock.patch.object(bot, "_send_order", new=mock.AsyncMock(return_value=123)), \
                mock.patch.object(pipeline.ai_scorer, "score_order", side_effect=score):
            self.assertEqual(pipeline.score_live(database.freelance_get(public_id)).ai_attempted, 0)
            asyncio.run(run())
        self.assertEqual(calls, [manual_id])
        self.assertIsNone(database.freelance_get(public_id)["message_id"])
        self.assertEqual(database.freelance_get(manual_id)["message_id"], 123)

    def test_crossing_22_during_telegram_fallback(self):
        pipeline._intake([make_order(1)], 0, set(), live=True)
        database.freelance_live_score_result(database.freelance_get(1), fit=9, ai_json='{"fit":9}')
        with mock.patch.object(schedule, "automatic_open", return_value=True) as opened:
            async def fail_once(**kwargs):
                opened.return_value = False
                raise bot.TelegramBadRequest(method=mock.Mock(), message="invalid HTML")

            with mock.patch.object(bot.bot, "send_message", new=mock.AsyncMock(side_effect=fail_once)) as send:
                asyncio.run(bot._deliver_freelance(force=True))
        self.assertEqual(send.await_count, 1)
        self.assertIsNone(database.freelance_get(1)["message_id"])

    def test_offers_release_if_night_starts_after_claim(self):
        row = {"order_id": 25}
        with mock.patch.object(schedule, "automatic_open", side_effect=[True, False]), \
                mock.patch.object(bot, "freelance_claim_offers", return_value=("token", [row])), \
                mock.patch.object(bot, "freelance_release_offers") as release, \
                mock.patch.object(bot.bot, "send_message", new=mock.AsyncMock()) as send:
            asyncio.run(bot.send_lead_offers())
        send.assert_not_awaited()
        release.assert_called_once_with("token", [25])

    def test_no_automatic_analysis_reminders_offers_or_summary_at_night(self):
        with mock.patch.object(schedule, "automatic_open", return_value=False), \
                mock.patch.object(bot.bot, "send_message", new=mock.AsyncMock()) as send, \
                mock.patch.object(pipeline, "live_rows") as evaluate, \
                mock.patch.object(bot, "freelance_reminders_due") as reminders, \
                mock.patch.object(bot, "freelance_claim_offers") as claim:
            pipeline.reassess_live()
            asyncio.run(bot.send_lead_reminders())
            asyncio.run(bot.send_lead_offers())
            asyncio.run(bot.send_freelance_daily_summary())
        for blocked in (send, evaluate, reminders, claim):
            blocked.assert_not_called()

    def test_morning_freshness_known_and_unknown_publication(self):
        morning = datetime(2026, 11, 24, 7, tzinfo=schedule.MSK)
        for unknown in (False, True):
            for minutes, expected in [(59, True), (61, False), (9 * 60, False)]:
                seen = (morning - timedelta(minutes=minutes)).astimezone(timezone.utc)
                row = {"seen_at": seen.strftime("%Y-%m-%d %H:%M:%S"),
                       "published_at": None if unknown else seen.isoformat()}
                with self.subTest(unknown=unknown, minutes=minutes):
                    self.assertEqual(pipeline.live_fresh(row, now=morning), expected)
