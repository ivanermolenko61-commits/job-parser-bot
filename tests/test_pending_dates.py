import database
from freelance import journal, manual, pipeline
from tests.test_pipeline_cycle import CycleTestBase, make_order


class PendingDateTests(CycleTestBase):
    def test_unknown_date_uses_seen_time_in_mixed_queue(self):
        oid, _ = manual.store(102, "A detailed automation project requiring a reporting pipeline for weekly data.")
        pipeline._journal_new(make_order(101), journal.QUEUED, full=True)
        with database._connect() as conn:
            conn.execute("UPDATE freelance_orders SET seen_at='2026-11-23 12:00:00' WHERE order_id=?", (oid,))
            conn.execute("UPDATE freelance_orders SET published_at='2026-11-23T11:00:00+00:00' WHERE order_id=101")
        self.assertEqual(database.freelance_pending(limit=1)[0]["order_id"], oid)
