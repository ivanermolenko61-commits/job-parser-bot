import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from unittest import mock

import database
from freelance import journal as jr, pipeline, sources
from tests.test_pipeline_cycle import CycleTestBase, ai_result, make_order


class LiveFreelanceTest(CycleTestBase):
    def order(self, oid, age=3):
        order = make_order(oid, f"Разработать Telegram-бота для учёта заявок {oid}")
        order.site = "flru"
        order.published_at = datetime.now(timezone.utc) - timedelta(minutes=age) if age is not None else None
        return order

    def intake(self, *orders):
        return pipeline._intake(orders, 0, set(), live=True)[1]

    def assess(self, oid):
        row = database.freelance_get(oid)
        return database.freelance_live_score_result(row, fit=8, ai_json=json.dumps(ai_result(8)))

    def test_expired_backlog_not_replayed_and_unknown_uses_seen_time(self):
        self.intake(self.order(1, 5 * 60), self.order(2, 10 * 60), self.order(3, 24 * 60),
                    self.order(4, None), self.order(5, 5))
        self.assertEqual({r["order_id"] for r in pipeline.live_rows("score", 10)}, {4, 5})
        self.assertIn("Возраст неизвестен", pipeline.format_order(
            pipeline.scored_from_row(database.freelance_get(4))))
        with database._connect() as conn:
            conn.execute("UPDATE freelance_orders SET seen_at=datetime('now','-61 minutes') WHERE order_id=4")
        self.intake(self.order(4, None))
        self.assertEqual(database.freelance_expire_live(), 1)
        self.assertEqual(database.freelance_get(4)["tech"], jr.STALE)
        with mock.patch.object(pipeline.ai_scorer, "score_order") as score:
            pipeline.score_live(database.freelance_get(4))
        score.assert_not_called()

    def test_ready_row_visible_behind_hundreds_of_newer_queued_rows(self):
        self.intake(self.order(1, 20))
        self.assertTrue(self.assess(1))
        with mock.patch.object(pipeline, "_find_duplicate", return_value=None):
            self.intake(*(self.order(i) for i in range(2, 333)))
        self.assertEqual([r["order_id"] for r in pipeline.live_rows("deliver", 8)], [1])
        self.assertEqual(len(pipeline.live_rows("score", 400)), 331)

    def test_claim_ack_and_failed_delivery_survive_new_connections(self):
        self.intake(self.order(10))
        self.assertTrue(self.assess(10))
        with ThreadPoolExecutor(max_workers=2) as pool:
            claims = list(pool.map(database.freelance_claim_delivery, [10, 10]))
        row, = [claim for claim in claims if claim]
        self.assertFalse(database.freelance_finish_delivery(10, "other", 123))
        self.assertTrue(database.freelance_finish_delivery(10, row["delivery_token"], None))
        self.assertIsNone(database.freelance_claim_delivery(10))
        with database._connect() as conn:
            conn.execute("UPDATE freelance_orders SET delivery_retry_at=datetime('now','-1 second') WHERE order_id=10")
        row = database.freelance_claim_delivery(10)
        self.assertTrue(database.freelance_finish_delivery(10, row["delivery_token"], 123))
        self.assertEqual(database.freelance_get(10)["message_id"], 123)
        self.assertIsNone(database.freelance_claim_delivery(10))
        self.assertEqual(database.freelance_live_daily_metrics()["under_five"], 1)

    def test_late_confirmation_after_another_claim_keeps_both_message_ids(self):
        self.intake(self.order(10))
        self.assertTrue(self.assess(10))
        first = database.freelance_claim_delivery(10)
        with database._connect() as conn:
            conn.execute("UPDATE freelance_orders SET delivery_retry_at=datetime('now','-1 second') WHERE order_id=10")
        second = database.freelance_claim_delivery(10)
        self.assertTrue(database.freelance_finish_delivery(10, second["delivery_token"], 200))
        self.assertTrue(database.freelance_finish_delivery(10, first["delivery_token"], 100))
        self.assertEqual(database.freelance_get(10)["message_id"], 200)
        with database._connect() as conn:
            conn.execute("UPDATE freelance_delivery_attempts SET confirmed_at=datetime('now','-25 hours')")
        expired = database.freelance_expired_messages(24)
        self.assertEqual([(r["message_id"], r["duplicate_card"]) for r in expired], [(100, 1)])
        self.assertTrue(database.freelance_mark_card_deleted(10, 100))
        self.assertEqual(database.freelance_expired_messages(24), [])
        self.assertIsNone(database.freelance_get(10)["card_deleted_at"])

    def test_late_confirmation_after_expiry_registers_the_card(self):
        self.intake(self.order(10))
        self.assertTrue(self.assess(10))
        claimed = database.freelance_claim_delivery(10)
        with database._connect() as conn:
            conn.execute("UPDATE freelance_orders SET delivery_retry_at=datetime('now','-1 second'), "
                         "published_at=datetime('now','-61 minutes') WHERE order_id=10")
        self.assertEqual(database.freelance_expire_live(), 1)
        self.assertTrue(database.freelance_finish_delivery(10, claimed["delivery_token"], 100))
        self.assertEqual(database.freelance_get(10)["message_id"], 100)
        self.assertEqual(database.freelance_get(10)["delivery"], jr.D_CARD)
        self.assertIsNone(database.freelance_claim_delivery(10))

    def test_reinit_recovers_a_claim_created_before_the_attempt_journal(self):
        self.intake(self.order(10))
        self.assertTrue(self.assess(10))
        with database._connect() as conn:
            conn.execute("UPDATE freelance_orders SET delivery_token='legacy', "
                         "delivery_retry_at=datetime('now','+1 minute') WHERE order_id=10")
        database.init_db()
        self.assertTrue(database.freelance_finish_delivery(10, "legacy", 100))
        self.assertEqual(database.freelance_get(10)["message_id"], 100)

    def test_reinit_replaces_old_tombstone_trigger_without_losing_ids(self):
        self.intake(self.order(10))
        with database._connect() as conn:
            conn.execute("UPDATE freelance_orders SET tech='filtered', listing_key='fingerprint' WHERE order_id=10")
            conn.execute("DROP TRIGGER freelance_remember_retired")
            conn.execute("""CREATE TRIGGER freelance_remember_retired AFTER DELETE ON freelance_orders BEGIN
                            INSERT OR IGNORE INTO freelance_retired(order_id,listing_key)
                            VALUES (OLD.order_id,OLD.listing_key); END""")
        database.init_db()
        with database._connect() as conn:
            conn.execute("DELETE FROM freelance_orders WHERE order_id=10")
        self.assertEqual(database.freelance_previous_listing(10, None), 10)
        self.assertIsNone(database.freelance_previous_listing(20, "fingerprint"))

    def test_unreliable_alias_cannot_revive_old_direct_listing(self):
        alias = self.order(11, None)
        alias.url = "https://freelance.ru/task/view/12?tracking=1"
        alias.site = sources.FREELANCE_RU
        self.intake(alias)
        self.assess(11)
        direct = self.order(sources.FREELANCE_RU_ID_OFFSET + 12, 310)
        direct.url = "https://www.freelance.ru/task/view/12"
        direct.site = sources.FREELANCE_RU
        self.assertEqual(self.intake(direct)["duplicate"], 1)
        self.assertEqual(pipeline.live_rows("deliver", 8), [])
        self.assertEqual(database.freelance_expire_live(), 1)

    def test_stale_native_id_remembered_after_cleanup(self):
        order = self.order(45, 360)
        self.intake(order)
        with database._connect() as conn:
            conn.execute("UPDATE freelance_orders SET seen_at=datetime('now','-300 days') WHERE order_id=45")
        database.cleanup_old()
        self.assertIsNone(database.freelance_get(45))
        self.intake(self.order(45, None))
        self.assertIsNone(database.freelance_get(45))

    def test_ai_result_for_edited_text_is_discarded(self):
        order = self.order(50)
        self.intake(order)
        previous = database.freelance_get(50)
        order.body = "Нужна автоматизация импорта заявок и интеграция API"
        self.intake(order)
        self.assertFalse(database.freelance_live_score_result(previous, fit=9, ai_json='{"fit":9}'))
        self.assertEqual(database.freelance_get(50)["tech"], jr.QUEUED)

    def test_ready_card_delivered_while_another_analysis_is_blocked(self):
        import bot

        self.intake(self.order(60), self.order(61))
        self.assess(60)
        blocked, release = threading.Event(), threading.Event()

        def wait_score(row):
            blocked.set()
            release.wait(timeout=8)
            return pipeline.CycleResult()

        async def run():
            await bot._pump_freelance_ai(force=True)
            pending = tuple(bot._live_ai_tasks.values())
            try:
                self.assertTrue(await asyncio.to_thread(blocked.wait, 3))
                await bot._deliver_freelance(force=True)
                self.assertEqual(database.freelance_get(60)["message_id"], 321)
            finally:
                release.set()
                await asyncio.gather(*pending)

        with mock.patch.object(bot, "score_live", side_effect=wait_score), \
                mock.patch.object(bot, "_send_order", new=mock.AsyncMock(return_value=321)):
            asyncio.run(run())

    def test_source_failure_leaves_ready_order_eligible(self):
        self.intake(self.order(70))
        self.assess(70)
        with mock.patch.dict(pipeline._source_retry_at, clear=True), \
                mock.patch.object(pipeline, "fetch_kwork_orders", side_effect=RuntimeError("403")), \
                self.assertLogs(level="ERROR"):
            result = pipeline.collect_live(sources.KWORK)
        self.assertTrue(result.error)
        self.assertIsNone(database.kv_get("freelance_last_ok"))
        self.assertEqual([r["order_id"] for r in pipeline.live_rows("deliver", 8)], [70])
