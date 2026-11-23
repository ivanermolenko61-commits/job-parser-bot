from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import database
from freelance import manual, pipeline, sources
from tests.test_pipeline_cycle import CycleTestBase, ai_result, make_order


class ManualTests(CycleTestBase):
    text = "Автоматизация выгрузки в Google Таблицы\nНужно каждый день обновлять данные из API и сохранять отчёт по продажам."
    url = "https://www.fl.ru/projects/1313/report.html?utm_source=test"

    def test_queue_after_restart_with_quota_and_all_sources_disabled(self):
        oid, short = manual.store(1, self.text)
        self.assertFalse(short)
        database.init_db()
        with patch.object(sources, "is_enabled", return_value=False), patch.object(pipeline, "_fetch_items", side_effect=AssertionError), \
             patch.object(pipeline.ai_scorer, "quota_exhausted", return_value=datetime.now(timezone.utc)+timedelta(days=1)):
            cycle = pipeline._prepare_cycle()
            self.assertEqual([sc.order.order_id for sc in cycle.unscored], [oid])
            self.assertIsNone(cycle.unscored[0].order.published_at)
            pipeline.finish_cycle(cycle, [], [], [])
        with patch.object(pipeline.ai_scorer, "score_order", return_value=ai_result(9)):
            cycle = pipeline._prepare_cycle(True)
            self.assertEqual([sc.order.order_id for sc in cycle.to_send], [oid])
            self.assertIn("дата", pipeline.format_order(cycle.to_send[0]))
            pipeline.save_sent(cycle.to_send[0], 77)
        row = database.freelance_get(oid)
        self.assertEqual((row["origin"],row["published_at"],row["delivery"]),("manual",None,"card"))

    def test_short_input_can_be_completed_after_restart(self):
        oid, short = manual.store(100, "Автоматизация таблиц")
        self.assertTrue(short)
        self.assertFalse(manual.pending())
        database.init_db()
        self.assertEqual(manual.store(101,self.text,supplement=oid),(oid,False))
        self.assertTrue(manual.pending())

    def test_canonical_id_both_orders(self):
        oid, _ = manual.store(200,self.text + "\n" + self.url)
        direct = make_order(sources.FL_ID_OFFSET + 1313)
        direct.site, direct.url = "flru","https://fl.ru/projects/1313/other-slug"
        self.assertEqual(pipeline._find_duplicate(direct),oid)
        direct.order_id = sources.FL_ID_OFFSET + 1314
        direct.url = "https://fl.ru/projects/1314/abc"
        pipeline._journal_new(direct,"queued",full=True)
        self.assertEqual(manual.store(201,"https://www.fl.ru/projects/1314/new-slug"),(direct.order_id,False))

    def test_unknown_host_preserves_job_identity_and_ignores_tracking(self):
        oid,_ = manual.store(205,self.text+" https://example.org/tasks/765/?utm_source=test")
        self.assertEqual(manual.store(206,"https://example.org/tasks/765"),(oid,False))
        self.assertIsNone(manual.listing("https://example.org:invalid/path"))

    def test_replayed_supplement_redirects_to_existing_order_and_keeps_notes(self):
        direct = make_order(sources.FL_ID_OFFSET + 1313)
        direct.site, direct.url = "flru", self.url
        pipeline._journal_new(direct,"queued",full=True)
        oid,short = manual.store(301,"Excel")
        self.assertTrue(short)
        self.assertEqual(manual.store(302,self.text+" "+self.url,supplement=oid),(direct.order_id,False))
        self.assertEqual(manual.store(302,self.text+" "+self.url,supplement=oid),(direct.order_id,False))
        self.assertIn("API",manual.latest_note(direct.order_id))
        with database._connect() as conn:
            records=conn.execute("SELECT message_id FROM freelance_manual_inputs WHERE order_id=? ORDER BY message_id",(direct.order_id,)).fetchall()
        self.assertEqual([r[0] for r in records],[301,302])

    def test_from_telegram_to_completed_deal(self):
        import bot
        from freelance import leads, journal as jr
        from tests.test_leads_bot import make_message, make_call, run, CHAT
        msg=make_message(self.text)
        msg.message_id=304
        msg.caption=None
        msg.entities=msg.caption_entities=[]
        msg.forward_origin=None
        run(bot.msg_manual_input(msg,"new"))
        oid=sources.MANUAL_ID_OFFSET+304
        with patch.object(pipeline.ai_scorer,"score_order",return_value=ai_result(9)):
            cycle=pipeline._prepare_cycle(True)
        pipeline.save_sent(cycle.to_send[0],23)
        call=make_call(f"fl_draft:{oid}")
        with patch.object(bot,"MY_CHAT_ID",CHAT):
            run(bot.cb_fl_draft(call))
        self.assertEqual(call.message.answer.await_count,2)
        leads.set_stage(oid,jr.LEAD_APPLIED)
        database.freelance_lead_remind(oid,"2026-11-01 00:00:00")
        database.init_db()
        self.assertIn(oid,[r["order_id"] for r in database.freelance_reminders_due("2026-11-23 12:00:00")])
        leads.set_stage(oid,jr.LEAD_AGREED)
        database.freelance_set_deal_price(oid,9000)
        leads.set_stage(oid,jr.LEAD_DONE)
        database.freelance_set_actuals(oid,3,1000)
        self.assertEqual(database.freelance_get(oid)["lead"],jr.LEAD_DONE)
