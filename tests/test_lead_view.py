"""Ф08, единица leadview: тексты карточки лида, «Входящих», подборки, напоминания и описание кнопок.
Чистая логика и БД во временном файле, без сети и aiogram.

Запуск из корня проекта: python -m unittest tests.test_lead_view -v
"""
import unittest
from datetime import datetime, timezone

import database
from freelance import journal as jr, lead_view as lv, leads
from tests.test_pipeline_cycle import CycleTestBase

NOW = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)   # среда, 15:00 МСК


def row(oid=1, **kw):
    base = {"order_id": oid, "title": f"Заказ {oid}", "url": f"https://x/{oid}", "fit": 8, "lead": None,
            "lead_reason": None, "next_action": None, "remind_at": None, "deal_price": None,
            "card_deleted_at": None}
    return {**base, **kw}


def datas(rows):
    return [b.data for r in rows for b in r if b.data]


class WhenTest(unittest.TestCase):
    def test_msk_labels(self):
        self.assertEqual(lv.fmt_when("2026-11-18 14:00:00", NOW), "сегодня 17:00")
        self.assertEqual(lv.fmt_when("2026-11-19 07:00:00", NOW), "завтра 10:00")
        self.assertEqual(lv.fmt_when("2026-11-25 07:00:00", NOW), "25.11 10:00")
        self.assertEqual(lv.fmt_when("2026-11-18 09:00:00", NOW), "просрочено, было сегодня 12:00")
        self.assertEqual(lv.fmt_when(None, NOW), "")
        self.assertEqual(lv.fmt_when("мусор", NOW), "")


class LeadCardTest(unittest.TestCase):
    def test_active_card_has_stage_action_remind_history(self):
        lead = row(lead=jr.LEAD_APPLIED, next_action="проверить <ответ>", remind_at="2026-11-19 07:00:00")
        events = [{"kind": "stage", "to_lead": jr.LEAD_APPLIED, "at": "2026-11-18 09:00:00", "undone": 0}]
        text = lv.lead_card_text(lead, events, now=NOW)
        for part in ("Лид 1", "Откликнулся", "оценка 8/10", "проверить &lt;ответ&gt;", "⏰ завтра 10:00",
                     "История:", "18.11 12:00 → 📨 Откликнулся", "/next 1"):
            self.assertIn(part, text)

    def test_lost_shows_reason_and_no_action(self):
        text = lv.lead_card_text(row(lead=jr.LEAD_LOST, lead_reason="мало денег"), [], now=NOW)
        self.assertIn("Отказ — мало денег", text)
        self.assertNotIn("Дальше:", text)
        self.assertNotIn("История", text)

    def test_price_and_previous_price_hint(self):
        self.assertIn("💰 12 000 ₽", lv.lead_card_text(row(lead=jr.LEAD_AGREED, deal_price=12000.0), [], now=NOW))
        hint = lv.lead_card_text(row(lead=jr.LEAD_AGREED), [], prev_price=9000.0, now=NOW)
        self.assertIn("цена не указана (было 9 000 ₽)", hint)

    def test_only_last_three_events_and_undone_struck_through(self):
        events = [{"kind": "stage", "to_lead": jr.LEAD_APPLIED, "at": "2026-11-18 09:00:00", "undone": 1}] + [
            {"kind": "next", "reason": f"т{i}", "at": "2026-11-18 08:00:00", "undone": 0} for i in range(5)]
        text = lv.lead_card_text(row(lead=jr.LEAD_SAVED), events, now=NOW)
        self.assertEqual(text.count("\n• "), lv.EVENT_LIMIT)
        self.assertIn("<s>→ 📨 Откликнулся</s>", text)

    def test_deleted_message_note_and_title_escaped(self):
        text = lv.lead_card_text(row(title="A & <B>", card_deleted_at="2026-11-18 01:00:00", lead=jr.LEAD_SAVED), [])
        self.assertIn("A &amp; &lt;B&gt;", text)
        self.assertIn("удалено по сроку", text)


class LeadButtonsTest(unittest.TestCase):
    def test_forward_stages_per_state(self):
        expect = {None: ["fl_st:1:saved", "fl_st:1:applied", "fl_lost:1"],
                  jr.LEAD_SAVED: ["fl_st:1:applied", "fl_st:1:discussing", "fl_lost:1", "fl_rm:1"],
                  jr.LEAD_AGREED: ["fl_st:1:done", "fl_lost:1", "fl_rm:1"],
                  jr.LEAD_DONE: [], jr.LEAD_LOST: []}
        for stage, want in expect.items():
            self.assertEqual(datas(lv.lead_card_buttons(row(lead=stage), [])), want, stage)

    def test_undo_button_only_with_undoable_event(self):
        ev = {"kind": "stage", "undone": 0}
        self.assertIn("fl_undo:1", datas(lv.lead_card_buttons(row(lead=jr.LEAD_SAVED), [ev])))
        for evs in ([], [{"kind": "undo", "undone": 0}], [{"kind": "stage", "undone": 1}]):
            self.assertNotIn("fl_undo:1", datas(lv.lead_card_buttons(row(lead=jr.LEAD_SAVED), evs)))

    def test_open_button_is_url_and_all_data_fit_telegram_limit(self):
        big = 999999999999
        rows = lv.lead_card_buttons(row(oid=big, lead=jr.LEAD_SAVED), [{"kind": "stage", "undone": 0}])
        self.assertTrue(any(b.url for r in rows for b in r))
        every = datas(rows) + datas(lv.lost_reason_buttons(big)) + datas(lv.remind_buttons(big)) \
            + datas(lv.order_card_buttons(big, "u")) + datas(lv.collapsed_buttons(big))
        self.assertTrue(all(len(d.encode()) <= 64 for d in every))

    def test_lost_reasons_cover_the_whole_list_and_remind_choices(self):
        got = [d for d in datas(lv.lost_reason_buttons(5)) if d.startswith("fl_lr:")]
        self.assertEqual(got, [f"fl_lr:5:{i}" for i in range(len(jr.LOST_REASONS))])
        self.assertEqual(datas(lv.remind_buttons(5)), ["fl_rc:5:tomorrow", "fl_rc:5:3days", "fl_rc:5:none", "fl_card:5"])


class OrderButtonsTest(unittest.TestCase):
    def test_new_card_has_save_apply_not_fit(self):
        rows = lv.order_card_buttons(7, "https://x/7")
        self.assertEqual(datas(rows), ["fl_more:7", "fl_save:7", "fl_apply:7", "fl_np:7"])
        self.assertEqual(rows[0][0].url, "https://x/7")
        self.assertIn("fl_less:7", datas(lv.order_card_buttons(7, None, detailed=True)))

    def test_decided_card_points_to_lead_and_closed_card_has_no_actions(self):
        saved = datas(lv.order_card_buttons(7, None, lead=jr.LEAD_SAVED))
        self.assertEqual(saved[-2:], ["fl_apply:7", "fl_lead:7"])
        self.assertEqual(datas(lv.order_card_buttons(7, None, lead=jr.LEAD_DISCUSSING))[-1], "fl_lead:7")
        self.assertEqual(datas(lv.order_card_buttons(7, None, lead=jr.LEAD_LOST)), ["fl_more:7"])
        self.assertEqual(datas(lv.order_card_buttons(7, None, actions=False)), ["fl_more:7"])

    def test_collapsed_line(self):
        self.assertEqual(lv.collapsed_text(row(title="Парсер <1>")), "❌ Не подходит: Парсер &lt;1&gt;")
        self.assertEqual(datas(lv.collapsed_buttons(7)), ["fl_back:7"])



class InboxTest(unittest.TestCase):
    def test_empty_inbox_text(self):
        self.assertEqual(lv.inbox_text([], [], [], NOW),
                         "Входящих нет: сохранённых лидов и необработанных интересных заказов нет")

    def test_groups_by_stage_with_action_and_remind(self):
        active = [row(1, lead=jr.LEAD_APPLIED, next_action="проверить ответ", remind_at="2026-11-19 07:00:00"),
                  row(2, lead=jr.LEAD_SAVED, next_action="откликнуться", remind_at="2026-11-18 09:00:00"),
                  row(3, lead=jr.LEAD_AGREED)]
        text = lv.inbox_text(active, [], [], NOW)
        order = [text.index(lv.STAGE_LABEL[s]) for s in (jr.LEAD_SAVED, jr.LEAD_APPLIED, jr.LEAD_AGREED)]
        self.assertEqual(order, sorted(order))                    # saved -> applied -> agreed
        for part in ("проверить ответ · ⏰ завтра 10:00", "откликнуться · ⏰ просрочено", "/lead_1", "/lead_3"):
            self.assertIn(part, text)
        self.assertNotIn("Необработанные", text)
        self.assertNotIn("Закрытые", text)

    def test_unprocessed_and_closed_blocks(self):
        text = lv.inbox_text([], [row(4, fit=9)],
                             [row(5, lead=jr.LEAD_LOST, lead_reason="мало денег"), row(6, lead=jr.LEAD_DONE)], NOW)
        for part in ("Активных лидов нет", "Необработанные интересные</b> (1)", "9/10", "/lead_4",
                     "Закрытые за 7 дней</b> (2)", "/lead_5", "Отказ: мало денег", "/lead_6", "Выполнен"):
            self.assertIn(part, text)

    def test_offers_and_reminder_texts(self):
        text = lv.offers_text([row(1), row(2, fit=None)])
        self.assertIn("(2)", text)
        self.assertIn("/lead_2", text)
        rem = lv.reminder_text(row(3, lead=jr.LEAD_APPLIED, next_action="проверить ответ"))
        self.assertIn("проверить ответ", rem)
        self.assertIn("/lead_3", rem)
        self.assertEqual(datas(lv.reminder_buttons(3)), ["fl_card:3", "fl_rm:3"])
        self.assertIn("проверить лид", lv.reminder_text(row(4, lead=jr.LEAD_SAVED)))


class CardFromDbTest(CycleTestBase):
    """Карточка и «Входящие» строятся только из БД: сообщения Telegram и память процесса не нужны."""

    def test_card_after_ttl_deletion(self):
        database.freelance_save(1, "flru", "Бот для заявок", "https://x/1", "5000 ₽", "2026-11-16T10:00:00",
                                "k1", 8, "{}", "sent", 0, 11)
        leads.set_stage(1, jr.LEAD_APPLIED, now=NOW)
        leads.set_stage(1, jr.LEAD_AGREED, now=NOW)
        database.freelance_set_deal_price(1, 15000.0)
        database.freelance_mark_card_deleted(1)
        lead = database.freelance_lead_get(1)
        events = database.freelance_lead_events(1, 3)
        text = lv.lead_card_text(lead, events, database.freelance_last_deal_price(1), NOW)
        for part in ("Бот для заявок", "Договорились", "сдать работу", "15 000 ₽", "удалено по сроку", "История"):
            self.assertIn(part, text)
        self.assertIn("fl_undo:1", datas(lv.lead_card_buttons(lead, events)))

    def test_inbox_from_db(self):
        for oid in (1, 2, 3):
            database.freelance_save(oid, "flru", f"Заказ {oid}", f"https://x/{oid}", "", "2026-11-16T10:00:00",
                                    f"k{oid}", 8, "{}", "sent", 0, oid)
        with database._connect() as conn:
            conn.execute("UPDATE freelance_orders SET verdict = 'ok', delivery = 'card', "
                         "delivered_at = datetime('now', '-1 hours')")
        leads.set_stage(1, jr.LEAD_SAVED, now=NOW)
        leads.set_stage(2, jr.LEAD_LOST, "другое", NOW)
        text = lv.inbox_text(database.freelance_leads_active(), database.freelance_unprocessed(),
                             database.freelance_leads_closed(7), NOW)
        for part in ("Сохранён", "/lead_1", "Необработанные интересные</b> (1)", "/lead_3", "Закрытые за 7 дней",
                     "/lead_2", "Отказ: другое"):
            self.assertIn(part, text)


if __name__ == "__main__":
    unittest.main()

