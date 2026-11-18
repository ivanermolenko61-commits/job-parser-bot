"""Ф08, единица leaddb: этапы лида с историей, отмена, причина отказа, TTL, напоминания, подборка,
чистка, метки триажа, рестарт процессов. Без сети.

Запуск из корня проекта: python -m unittest tests.test_leads_db -v
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import database
from freelance import alot_client, journal as jr, leads, pipeline
from tests.test_pipeline_cycle import CycleTestBase

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NOW = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)   # среда, 15:00 МСК


def save(oid, status="sent", fit=8, **kw):
    database.freelance_save(oid, "flru", f"Заказ {oid}", f"https://x/{oid}", "5000 ₽", "2026-11-16T10:00:00",
                            f"k{oid}", fit, "{}", status, 0, kw.pop("message_id", oid), **kw)


def row(oid):
    return database.freelance_get(oid)


class LeadBase(CycleTestBase):
    def setUp(self):
        super().setUp()
        for oid in (1, 2, 3):
            save(oid)


class PureRulesTest(unittest.TestCase):
    def test_label_rules(self):
        self.assertIsNone(jr.lead_label(jr.LEAD_SAVED))
        self.assertIsNone(jr.lead_label(None))
        for stage in (jr.LEAD_APPLIED, jr.LEAD_DISCUSSING, jr.LEAD_AGREED, jr.LEAD_DONE):
            self.assertEqual(jr.lead_label(stage), jr.LABEL_GOOD, stage)
        self.assertEqual(jr.lead_label(jr.LEAD_LOST, jr.LOST_NOT_MY_PROFILE), jr.LABEL_BAD)
        self.assertEqual(jr.lead_label(jr.LEAD_LOST, jr.LOST_LEGACY), jr.LABEL_BAD)
        for reason in ("мало денег", "заказчик пропал", "выбрали другого", "сам отказался", "другое"):
            self.assertIsNone(jr.lead_label(jr.LEAD_LOST, reason), reason)

    def test_remind_choices(self):
        # завтра 10:00 МСК = 07:00 UTC следующих суток; через 3 дня - ровно +72 часа; без напоминания - None
        self.assertEqual(leads.remind_for_choice(leads.REMIND_TOMORROW, NOW), "2026-11-19 07:00:00")
        self.assertEqual(leads.remind_for_choice(leads.REMIND_3DAYS, NOW), "2026-11-21 12:00:00")
        self.assertIsNone(leads.remind_for_choice(leads.REMIND_NONE, NOW))
        with self.assertRaises(ValueError):
            leads.remind_for_choice("через год", NOW)
        late = datetime(2026, 11, 18, 22, 30, tzinfo=timezone.utc)   # 01:30 МСК 19-го: «завтра» = 20-е
        self.assertEqual(leads.remind_for_choice(leads.REMIND_TOMORROW, late), "2026-11-20 07:00:00")

    def test_default_next_per_stage(self):
        self.assertEqual(leads.default_next(jr.LEAD_SAVED, NOW), ("откликнуться", "2026-11-19 12:00:00"))
        self.assertEqual(leads.default_next(jr.LEAD_APPLIED, NOW), ("проверить ответ", "2026-11-20 12:00:00"))
        self.assertEqual(leads.default_next(jr.LEAD_DISCUSSING, NOW), ("ответить/уточнить ТЗ", "2026-11-19 12:00:00"))
        self.assertEqual(leads.default_next(jr.LEAD_AGREED, NOW), ("сдать работу", "2026-11-21 12:00:00"))
        self.assertEqual(leads.default_next(jr.LEAD_DONE, NOW), (None, None))
        self.assertEqual(leads.default_next(jr.LEAD_LOST, NOW), (None, None))


class TransitionsTest(LeadBase):
    def test_all_stages_set_event_and_default_action(self):
        for stage in jr.LEAD_STAGES:
            reason = "другое" if stage == jr.LEAD_LOST else None
            res = leads.set_stage(1, stage, reason, NOW)
            self.assertTrue(res["ok"] and res["changed"], stage)
            self.assertEqual(row(1)["lead"], stage)
        self.assertEqual(len(database.freelance_lead_events(1, 20)), len(jr.LEAD_STAGES))
        self.assertEqual(row(1)["status"], "dismissed")   # зеркало для старых запросов пересчитано

    def test_defaults_applied_and_cleared(self):
        leads.set_stage(1, jr.LEAD_APPLIED, now=NOW)
        r = row(1)
        self.assertEqual((r["next_action"], r["remind_at"]), ("проверить ответ", "2026-11-20 12:00:00"))
        leads.set_stage(1, jr.LEAD_DONE, now=NOW)
        r = row(1)
        self.assertEqual((r["next_action"], r["remind_at"]), (None, None))   # у done действие и срок сняты

    def test_unknown_stage_and_reason_leave_row_unchanged(self):
        leads.set_stage(1, jr.LEAD_SAVED, now=NOW)
        before = row(1)
        for args in (("в космос", None), (jr.LEAD_LOST, None), (jr.LEAD_LOST, "просто так")):
            with self.assertRaises(ValueError, msg=args):
                leads.set_stage(1, *args, now=NOW)
        self.assertEqual(row(1), before)
        self.assertEqual(len(database.freelance_lead_events(1, 20)), 1)

    def test_same_stage_twice_writes_nothing_new(self):
        leads.set_stage(1, jr.LEAD_APPLIED, now=NOW)
        res = leads.set_stage(1, jr.LEAD_APPLIED, now=NOW)
        self.assertTrue(res["ok"])
        self.assertFalse(res["changed"])
        self.assertEqual(len(database.freelance_lead_events(1, 20)), 1)

    def test_missing_order(self):
        self.assertEqual(leads.set_stage(999, jr.LEAD_SAVED, now=NOW)["reason"], "not_found")
        self.assertEqual(leads.undo(999, NOW)["reason"], "not_found")

    def test_next_text_and_remind_only_on_active_lead(self):
        self.assertEqual(leads.set_next(1, "позвонить", NOW)["reason"], "no_active_lead")
        leads.set_stage(1, jr.LEAD_DISCUSSING, now=NOW)
        self.assertTrue(leads.set_next(1, "  выслать \n смету  ", NOW)["changed"])
        self.assertEqual(row(1)["next_action"], "выслать смету")
        self.assertEqual(row(1)["remind_at"], "2026-11-19 12:00:00")           # срок не менялся
        self.assertTrue(leads.set_remind(1, leads.REMIND_NONE, NOW)["changed"])
        self.assertIsNone(row(1)["remind_at"])
        self.assertTrue(leads.set_next(1, "я" * 1000, NOW)["changed"])
        self.assertEqual(len(row(1)["next_action"]), jr.LEAD_TEXT_MAX)
        leads.set_stage(1, jr.LEAD_LOST, "мало денег", NOW)
        self.assertEqual(leads.set_remind(1, leads.REMIND_3DAYS, NOW)["reason"], "no_active_lead")

    def test_old_api_still_works_and_writes_no_events(self):
        self.assertTrue(database.freelance_set_status(1, "taken"))
        self.assertEqual(row(1)["lead"], jr.LEAD_AGREED)
        self.assertTrue(database.freelance_set_status(2, "dismissed"))
        self.assertEqual((row(2)["lead"], row(2)["lead_reason"]), (jr.LEAD_LOST, "не подходит"))
        self.assertEqual(database.freelance_lead_events(1), [])



class UndoTest(LeadBase):
    def test_undo_restores_stage_reason_action_and_remind(self):
        leads.set_stage(1, jr.LEAD_APPLIED, now=NOW)
        leads.set_next(1, "своё действие", NOW)
        leads.set_stage(1, jr.LEAD_LOST, "выбрали другого", NOW)
        self.assertTrue(leads.undo(1, NOW)["ok"])               # отмена отказа
        got = row(1)
        self.assertEqual((got["lead"], got["lead_reason"], got["next_action"], got["remind_at"]),
                         (jr.LEAD_APPLIED, None, "своё действие", "2026-11-20 12:00:00"))
        leads.undo(1, NOW)                                      # двойная отмена: на шаг дальше (своё действие)
        self.assertEqual(row(1)["next_action"], "проверить ответ")
        leads.undo(1, NOW)                                      # ещё дальше: лида не было
        self.assertIsNone(row(1)["lead"])
        self.assertIsNone(row(1)["next_action"])
        self.assertEqual(leads.undo(1, NOW), {"ok": False, "changed": False, "reason": "nothing"})

    def test_undo_without_events(self):
        self.assertEqual(leads.undo(1, NOW)["reason"], "nothing")
        self.assertIsNone(row(1)["lead"])

    def test_undo_of_lost_restores_previous_reason(self):
        leads.set_stage(1, jr.LEAD_LOST, "мало денег", NOW)
        leads.set_stage(1, jr.LEAD_LOST, "другое", NOW)
        leads.undo(1, NOW)
        self.assertEqual((row(1)["lead"], row(1)["lead_reason"]), (jr.LEAD_LOST, "мало денег"))

    def test_step_back_from_agreed_clears_deal_price_and_keeps_actuals(self):
        leads.set_stage(1, jr.LEAD_AGREED, now=NOW)
        database.freelance_set_deal_price(1, 12000.0)
        database.freelance_set_actuals(1, 5.0, 300)
        leads.set_stage(1, jr.LEAD_APPLIED, now=NOW)            # шаг назад обычным действием
        self.assertIsNone(row(1)["deal_price"])
        self.assertEqual(database.freelance_last_deal_price(1), 12000.0)   # подсказка «было N ₽»
        self.assertEqual(database.freelance_actuals_rows()[0]["hours_actual"], 5.0)   # факты работы целы
        leads.undo(1, NOW)                                      # отмена возвращает agreed и цену
        self.assertEqual((row(1)["lead"], row(1)["deal_price"]), (jr.LEAD_AGREED, 12000.0))

    def test_undo_of_agreed_step_has_no_price_and_reentry_asks_again(self):
        leads.set_stage(1, jr.LEAD_DISCUSSING, now=NOW)
        leads.set_stage(1, jr.LEAD_AGREED, now=NOW)
        database.freelance_set_deal_price(1, 9000.0)
        leads.undo(1, NOW)                                      # отмена перехода в agreed
        self.assertEqual(row(1)["lead"], jr.LEAD_DISCUSSING)
        self.assertIsNone(row(1)["deal_price"])
        leads.set_stage(1, jr.LEAD_AGREED, now=NOW)             # повторный agreed: цену бот спросит заново
        self.assertIsNone(row(1)["deal_price"])

    def test_done_keeps_price_and_lost_clears_it(self):
        leads.set_stage(1, jr.LEAD_AGREED, now=NOW)
        database.freelance_set_deal_price(1, 7000.0)
        leads.set_stage(1, jr.LEAD_DONE, now=NOW)
        self.assertEqual(row(1)["deal_price"], 7000.0)
        leads.set_stage(1, jr.LEAD_LOST, "другое", NOW)
        self.assertIsNone(row(1)["deal_price"])
        leads.undo(1, NOW)
        self.assertEqual((row(1)["lead"], row(1)["deal_price"]), (jr.LEAD_DONE, 7000.0))

    def test_undo_of_next_and_remind_events_keeps_stage(self):
        leads.set_stage(1, jr.LEAD_SAVED, now=NOW)
        leads.set_next(1, "иное", NOW)
        leads.set_remind(1, leads.REMIND_NONE, NOW)
        leads.undo(1, NOW)
        self.assertEqual(row(1)["remind_at"], "2026-11-19 12:00:00")
        leads.undo(1, NOW)
        self.assertEqual(row(1)["next_action"], "откликнуться")
        self.assertEqual(row(1)["lead"], jr.LEAD_SAVED)

    def test_failed_write_rolls_back_row_and_event(self):
        leads.set_stage(1, jr.LEAD_APPLIED, now=NOW)
        before = row(1)
        with database._connect() as conn:
            conn.execute("DROP TABLE freelance_lead_events")
        with self.assertRaises(Exception):
            database.freelance_lead_stage(1, jr.LEAD_DISCUSSING, now="2026-11-18 12:00:00")
        self.assertEqual(row(1), before)                        # строка не изменилась без события



class TtlAndCleanupTest(LeadBase):
    def age(self, hours):
        with database._connect() as conn:
            conn.execute("UPDATE freelance_orders SET sent_at = datetime('now', ?)", (f"-{hours} hours",))

    def test_ttl_skips_saved_lead_and_does_not_touch_lead_data(self):
        leads.set_stage(1, jr.LEAD_SAVED, now=NOW)
        leads.set_stage(2, jr.LEAD_LOST, "другое", NOW)
        self.age(30)
        self.assertEqual({r["order_id"] for r in database.freelance_expired_messages(24)}, {2, 3})
        events_before = database.freelance_lead_events(2, 20)
        for oid in (2, 3):
            database.freelance_mark_card_deleted(oid)
        self.assertEqual((row(2)["lead"], row(2)["lead_reason"]), (jr.LEAD_LOST, "другое"))
        self.assertEqual(database.freelance_lead_events(2, 20), events_before)
        self.assertEqual(database.freelance_expired_messages(24), [])

    def test_lead_card_data_comes_from_db_only(self):
        leads.set_stage(1, jr.LEAD_APPLIED, now=NOW)
        database.freelance_mark_card_deleted(1)
        lead = database.freelance_lead_get(1)
        self.assertEqual((lead["lead"], lead["next_action"], lead["title"]),
                         (jr.LEAD_APPLIED, "проверить ответ", "Заказ 1"))
        self.assertEqual(database.freelance_lead_events(1, 3)[0]["to_lead"], jr.LEAD_APPLIED)
        self.assertIsNone(database.freelance_lead_get(404))

    def test_cleanup_keeps_active_leads_with_events_and_drops_orphans(self):
        leads.set_stage(1, jr.LEAD_DISCUSSING, now=NOW)
        leads.set_stage(2, jr.LEAD_LOST, "другое", NOW)
        with database._connect() as conn:
            conn.execute("UPDATE freelance_orders SET seen_at = datetime('now', '-40 days')")
        database.cleanup_old()
        self.assertIsNotNone(row(1))
        self.assertEqual(len(database.freelance_lead_events(1, 20)), 1)   # события активного лида живы
        self.assertIsNone(row(2))                                         # закрытый - обычный срок журнала
        self.assertIsNone(row(3))
        with database._connect() as conn:
            orphans = conn.execute("SELECT COUNT(*) FROM freelance_lead_events WHERE order_id NOT IN "
                                   "(SELECT order_id FROM freelance_orders)").fetchone()[0]
        self.assertEqual(orphans, 0)


class RemindersTest(LeadBase):
    NOW_SQL = "2026-11-18 12:00:00"

    def test_due_marks_once_and_skips_closed(self):
        leads.set_stage(1, jr.LEAD_APPLIED, now=NOW - timedelta(days=5))   # срок давно прошёл
        leads.set_stage(2, jr.LEAD_DISCUSSING, now=NOW - timedelta(days=5))
        leads.set_stage(2, jr.LEAD_DONE, now=NOW)                          # закрыт: без напоминания
        leads.set_stage(3, jr.LEAD_SAVED, now=NOW + timedelta(days=5))     # срок в будущем
        due = database.freelance_reminders_due(now=self.NOW_SQL)
        self.assertEqual([r["order_id"] for r in due], [1])
        self.assertTrue(database.freelance_mark_reminded(1, due[0]["remind_at"], self.NOW_SQL))
        self.assertEqual(database.freelance_reminders_due(now="2026-11-18 12:05:00"), [])

    def test_removed_term_is_not_swallowed_and_new_term_fires_again(self):
        leads.set_stage(1, jr.LEAD_APPLIED, now=NOW - timedelta(days=5))
        old = row(1)["remind_at"]
        leads.set_remind(1, leads.REMIND_NONE, NOW)                        # срок успели снять
        self.assertFalse(database.freelance_mark_reminded(1, old, self.NOW_SQL))
        database.freelance_lead_remind(1, "2026-11-18 11:00:00", now=self.NOW_SQL)
        self.assertEqual(len(database.freelance_reminders_due(now=self.NOW_SQL)), 1)

    def test_failed_send_leaves_reminder_pending(self):
        leads.set_stage(1, jr.LEAD_APPLIED, now=NOW - timedelta(days=5))
        self.assertEqual(len(database.freelance_reminders_due(now=self.NOW_SQL)), 1)
        self.assertEqual(len(database.freelance_reminders_due(now="2026-11-18 12:05:00")), 1)   # mark не вызван



class OffersAndInboxTest(CycleTestBase):
    def seed(self, oid, *, fit=8, delivered="-1 hours", verdict="ok", delivery="card"):
        save(oid, "sent", fit)
        with database._connect() as conn:
            conn.execute("UPDATE freelance_orders SET delivered_at = datetime('now', ?), verdict = ?, delivery = ? "
                         "WHERE order_id = ?", (delivered, verdict, delivery, oid))

    def test_unprocessed_filters(self):
        self.seed(1)                                   # подходит: карточка
        self.seed(2, delivery="digest")                # подходит: сводка «ещё N»
        self.seed(3, delivered="-4 days")              # старше 3 дней
        self.seed(4, verdict="low_fit")                # verdict не ok
        self.seed(5)
        leads.set_stage(5, jr.LEAD_SAVED, now=NOW)     # решение уже есть
        self.assertEqual({r["order_id"] for r in database.freelance_unprocessed()}, {1, 2})

    def test_offer_once_then_never_again_even_after_restart(self):
        for oid in (1, 2, 3):
            self.seed(oid, fit=5 + oid)
        claimed, first = database.freelance_claim_offers(limit=2)
        self.assertEqual([r["order_id"] for r in first], [3, 2])           # лучшие по оценке
        self.assertEqual([r["order_id"] for r in database.freelance_claim_offers(limit=10)[1]], [1])
        self.assertEqual(database.freelance_claim_offers()[1], [])          # без повтора
        database.init_db()                                                  # «после рестарта»: состояние в БД
        self.assertEqual(database.freelance_claim_offers()[1], [])
        self.assertEqual(len(database.freelance_unprocessed()), 3)          # во «Входящих» заказы остаются
        self.assertEqual(database.freelance_release_offers(claimed, [3, 2]), 2)   # сбой отправки возвращает право
        self.assertEqual([r["order_id"] for r in database.freelance_claim_offers()[1]], [3, 2])

    def test_active_and_closed_lists(self):
        for oid in (1, 2, 3):
            self.seed(oid)
        leads.set_stage(1, jr.LEAD_AGREED, now=NOW)
        leads.set_stage(2, jr.LEAD_SAVED, now=NOW)
        leads.set_stage(3, jr.LEAD_DONE, now=NOW)
        self.assertEqual({r["order_id"] for r in database.freelance_leads_active()}, {1, 2})
        with database._connect() as conn:
            conn.execute("UPDATE freelance_orders SET lead_at = datetime('now', '-10 days') WHERE order_id = 3")
        self.assertEqual(database.freelance_leads_closed(7), [])
        self.assertEqual([r["order_id"] for r in database.freelance_leads_closed(30)], [3])



def ev_snapshot():
    return {k: v for k, v in pipeline.triage_evidence().items() if k.startswith("lab_")}


class TriageLabelTest(CycleTestBase):
    def seed_score(self, oid, triage):
        save(oid)
        pipeline._record_triage_evidence(miss=False, good=True, order_id=oid, triage=triage)

    def test_save_has_no_label_apply_good_undo_returns_counters(self):
        self.seed_score(1, 7)
        base = ev_snapshot()
        leads.set_stage(1, jr.LEAD_SAVED, now=NOW)
        self.assertEqual(ev_snapshot(), base)                              # «Сохранить» - не решение
        self.assertEqual(leads.set_stage(1, jr.LEAD_APPLIED, now=NOW)["label"], "good")
        self.assertEqual(ev_snapshot()["lab_good"], base["lab_good"] + 1)
        self.assertEqual(leads.undo(1, NOW)["label"], "removed")          # «↩️ Отменить»: lab_good вернулся
        self.assertEqual(ev_snapshot(), base)
        self.assertIsNone(leads.undo(1, NOW).get("label"))                # отмена «Сохранить»: метки не было

    def test_bad_then_return_restores_lab_bad(self):
        self.seed_score(1, 7)
        base = ev_snapshot()
        self.assertEqual(leads.set_stage(1, jr.LEAD_LOST, jr.LOST_LEGACY, NOW)["label"], "bad")
        self.assertEqual(ev_snapshot()["lab_bad"], base["lab_bad"] + 1)
        leads.undo(1, NOW)                                                 # «↩️ Вернуть»
        self.assertEqual(ev_snapshot(), base)

    def test_miss_is_counted_and_removed(self):
        self.seed_score(1, 1)                                              # быстрая оценка <= порога: пропуск
        self.assertEqual(leads.set_stage(1, jr.LEAD_DISCUSSING, now=NOW)["label"], "miss")
        ev = ev_snapshot()
        self.assertEqual((ev["lab_good"], ev["lab_miss"]), (1, 1))
        leads.undo(1, NOW)
        ev = ev_snapshot()
        self.assertEqual((ev["lab_good"], ev["lab_miss"], ev["lab_bad"]), (0, 0, 0))

    def test_other_lost_reason_preserves_label_and_not_my_profile_flips_it(self):
        self.seed_score(1, 7)
        base = ev_snapshot()
        leads.set_stage(1, jr.LEAD_APPLIED, now=NOW)                       # good
        good = ev_snapshot()
        self.assertIsNone(leads.set_stage(1, jr.LEAD_LOST, "мало денег", NOW)["label"])
        self.assertEqual(ev_snapshot(), good)
        self.assertEqual(leads.set_stage(1, jr.LEAD_LOST, jr.LOST_NOT_MY_PROFILE, NOW)["label"], "bad")
        self.assertEqual(ev_snapshot()["lab_bad"], base["lab_bad"] + 1)
        self.assertEqual(leads.set_stage(1, jr.LEAD_APPLIED, now=NOW)["label"], "good")   # bad -> good
        ev = ev_snapshot()
        self.assertEqual((ev["lab_good"], ev["lab_bad"]), (base["lab_good"] + 1, base["lab_bad"]))

    def test_economic_refusals_preserve_missed_good_and_undo_back_to_unlabeled(self):
        self.seed_score(1, 1)
        base = ev_snapshot()
        leads.set_stage(1, jr.LEAD_SAVED, now=NOW)
        leads.set_stage(1, jr.LEAD_APPLIED, now=NOW)
        good = ev_snapshot()
        self.assertEqual((good["lab_good"], good["lab_miss"]), (1, 1))
        for reason in jr.LOST_REASONS[1:]:
            with self.subTest(reason=reason):
                leads.set_stage(1, jr.LEAD_LOST, reason, NOW)
                self.assertEqual(ev_snapshot(), good)
                # Repeating the refusal and changing its reason cannot add another label.
                leads.set_stage(1, jr.LEAD_LOST, reason, NOW)
                self.assertEqual(ev_snapshot(), good)
        for _ in jr.LOST_REASONS[1:]:
            leads.undo(1, NOW)
            self.assertEqual(ev_snapshot(), good)
        leads.undo(1, NOW)  # applied -> saved
        self.assertEqual(ev_snapshot(), base)
        leads.undo(1, NOW)
        self.assertEqual(ev_snapshot(), base)

    def test_neutral_refusal_preserves_bad_and_undo_restores_previous_good(self):
        self.seed_score(1, 1)
        leads.set_stage(1, jr.LEAD_APPLIED, now=NOW)
        good = ev_snapshot()
        leads.set_stage(1, jr.LEAD_LOST, jr.LOST_REASONS[1], NOW)
        leads.set_stage(1, jr.LEAD_LOST, jr.LOST_NOT_MY_PROFILE, NOW)
        bad = ev_snapshot()
        self.assertEqual((bad["lab_good"], bad["lab_bad"], bad["lab_miss"]), (0, 1, 0))
        leads.set_stage(1, jr.LEAD_LOST, jr.LOST_REASONS[2], NOW)
        self.assertEqual(ev_snapshot(), bad)
        leads.undo(1, NOW)
        self.assertEqual(ev_snapshot(), bad)
        leads.undo(1, NOW)
        self.assertEqual(ev_snapshot(), good)

    def test_neutral_refusal_without_decision_stays_unlabeled(self):
        self.seed_score(1, 1)
        base = ev_snapshot()
        leads.set_stage(1, jr.LEAD_SAVED, now=NOW)
        leads.set_stage(1, jr.LEAD_LOST, jr.LOST_REASONS[1], NOW)
        self.assertEqual(ev_snapshot(), base)
        leads.set_stage(1, jr.LEAD_APPLIED, now=NOW)
        leads.undo(1, NOW)
        self.assertEqual(ev_snapshot(), base)

    def test_legacy_profile_decision_survives_neutral_refusal(self):
        self.seed_score(1, 1)
        database.freelance_set_lead(1, jr.LEAD_APPLIED)
        pipeline.record_triage_label(1, suitable=True)
        good = ev_snapshot()
        leads.set_stage(1, jr.LEAD_LOST, jr.LOST_REASONS[1], NOW)
        self.assertEqual(ev_snapshot(), good)
        leads.undo(1, NOW)
        self.assertEqual(ev_snapshot(), good)

    def test_guarded_price_write_rejects_early_stage(self):
        save(1)
        self.assertFalse(database.freelance_set_deal_price(1, 9000, require_price_stage=True))
        leads.set_stage(1, jr.LEAD_AGREED, now=NOW)
        self.assertTrue(database.freelance_set_deal_price(1, 9000, require_price_stage=True))
        leads.undo(1, NOW)
        self.assertFalse(database.freelance_set_deal_price(1, 9000, require_price_stage=True))
        self.assertIsNone(database.freelance_get(1)["deal_price"])

    def test_progress_between_good_stages_does_not_double_count(self):
        self.seed_score(1, 7)
        leads.set_stage(1, jr.LEAD_APPLIED, now=NOW)
        for stage in (jr.LEAD_DISCUSSING, jr.LEAD_AGREED, jr.LEAD_DONE):
            self.assertIsNone(leads.set_stage(1, stage, now=NOW).get("label"))
        self.assertEqual(ev_snapshot()["lab_good"], 1)

    def test_unscored_order_is_ignored_and_counters_stay_non_negative(self):
        save(2)                                                            # быстрой оценки нет
        base = ev_snapshot()
        leads.set_stage(2, jr.LEAD_APPLIED, now=NOW)
        leads.undo(2, NOW)
        self.assertEqual(ev_snapshot(), base)
        self.assertTrue(all(v >= 0 for v in ev_snapshot().values()))

    def test_changed_settings_remove_only_the_order_mark(self):
        self.seed_score(1, 7)
        leads.set_stage(1, jr.LEAD_APPLIED, now=NOW)
        with mock.patch.object(pipeline, "_evidence_signature", return_value="другие-настройки"):
            self.assertIsNone(pipeline.retarget_triage_label(1, jr.LABEL_GOOD, None))
        self.assertEqual(len(pipeline._load_scores()["1"]), 2)             # пометка заказа снята, счётчики не тронуты



class NoCustomerCallsTest(CycleTestBase):
    def test_lead_actions_never_touch_exchange_clients(self):
        save(1)
        boom = mock.Mock(side_effect=AssertionError("вызов площадки"))
        with mock.patch.object(pipeline, "_fetch_feed_orders", boom), \
                mock.patch.object(pipeline, "fetch_fl_orders", boom), mock.patch.object(pipeline, "fetch_kwork_orders", boom):
            leads.set_stage(1, jr.LEAD_APPLIED, now=NOW)
            leads.set_next(1, "текст", NOW)
            leads.set_remind(1, leads.REMIND_3DAYS, NOW)
            leads.undo(1, NOW)
            leads.set_stage(1, jr.LEAD_LOST, "другое", NOW)
            database.freelance_reminders_due()
            database.freelance_claim_offers()
        boom.assert_not_called()


CHILD = r'''
import json, os, sys
sys.path.insert(0, os.getcwd())
import database
from freelance import journal as jr, leads
step = sys.argv[1]
database.init_db()
if step == "create":
    database.freelance_save(7, "flru", "Бот", "https://x/7", "1000 ₽", "2026-11-16T10:00:00", "k7", 8, "{}", "sent", 0, 7)
    leads.set_stage(7, jr.LEAD_APPLIED)
    database.freelance_lead_remind(7, "2020-01-01 00:00:00")      # напоминание в прошлом
    print(json.dumps({"ok": True}))
else:
    due = database.freelance_reminders_due()
    fired = sum(1 for r in due if database.freelance_mark_reminded(r["order_id"], r["remind_at"]))
    row = database.freelance_get(7)
    print(json.dumps({"fired": fired, "lead": row["lead"], "next": row["next_action"],
                      "events": len(database.freelance_lead_events(7, 20))}))
'''


class RestartTest(unittest.TestCase):
    """Три отдельных процесса Python на одном файле БД: напоминание срабатывает один раз."""

    def run_child(self, db, step):
        env = dict(os.environ, DB_PATH=db, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
        r = subprocess.run([sys.executable, "-c", CHILD, step], cwd=ROOT, env=env, capture_output=True,
                           text=True, encoding="utf-8", timeout=180)
        self.assertEqual(r.returncode, 0, r.stderr[-600:])
        return json.loads(r.stdout.strip().splitlines()[-1])

    def test_reminder_fires_once_across_restarts(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "restart.db")
            self.run_child(db, "create")
            second = self.run_child(db, "check")
            third = self.run_child(db, "check")
        self.assertEqual((second["fired"], third["fired"]), (1, 0))
        self.assertEqual((second["lead"], third["lead"]), (jr.LEAD_APPLIED, jr.LEAD_APPLIED))
        self.assertEqual((second["next"], third["events"]), ("проверить ответ", 2))


if __name__ == "__main__":
    unittest.main()

