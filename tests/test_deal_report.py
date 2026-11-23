from freelance import journal as jr, leads, pipeline, report
import database
from tests.test_pipeline_cycle import CycleTestBase, make_order


class DealReportTests(CycleTestBase):
    def test_history_undo_duplicates_and_weighted_return(self):
        for oid in (41, 42, 43, 44, 45):
            pipeline._journal_new(make_order(oid), jr.QUEUED, full=True)
        for oid, price, hours, expenses in ((41, 5000, 2, 0), (42, 6000, 3, 1000), (43, 7000, 1, None)):
            leads.set_stage(oid, jr.LEAD_APPLIED)
            leads.set_stage(oid, jr.LEAD_DISCUSSING)
            leads.set_stage(oid, jr.LEAD_AGREED)
            database.freelance_set_deal_price(oid, price)
            leads.set_stage(oid, jr.LEAD_DONE)
            database.freelance_set_actuals(oid, hours, expenses)
        leads.set_stage(44, jr.LEAD_APPLIED)
        leads.undo(44)
        leads.set_stage(44, jr.LEAD_LOST, jr.LOST_LEGACY)
        rows = report.load_rows()
        totals, = report.funnel(rows + rows).values()
        self.assertEqual([totals[k] for k in ("leads", "applied", "discussing", "agreed", "done")], [3]*5)
        self.assertEqual(totals["lost_reasons"], {jr.LOST_LEGACY: 1})
        self.assertEqual(totals["priced"], 2)
        self.assertIn("2000 ₽/ч", report.format_funnel(rows))
