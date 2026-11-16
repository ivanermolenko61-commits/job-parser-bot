"""Суточная сводка фриланса и отключение Gemini. Без сети.

Запуск из корня проекта: python -m unittest tests.test_day_summary -v
"""
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import database
from freelance import ai_scorer
from freelance import journal as jr


class DaySummaryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = database.DB_PATH
        database.DB_PATH = os.path.join(self.tmp.name, "t.db")
        database.init_db()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(setattr, database, "DB_PATH", self.old_path)

    def save(self, oid, status, fit):
        database.freelance_save(oid, "kwork", f"заказ {oid}", f"https://x/{oid}", "1000 ₽",
                                "2026-10-24T00:00:00+00:00", f"k{oid}", fit, "{}", status)

    def test_counts_by_outcome(self):
        """Измерения не смешиваются: сводка «ещё N» - оценено хорошо и показано, а не отвергнуто AI."""
        self.save(1, "sent", 8)
        self.save(2, "taken", 9)
        self.save(3, "rejected", 3)
        self.save(4, "rejected", 4)
        database.freelance_save(5, "kwork", "дубль", "u", "", "2026-10-24T00:00:00+00:00", "k5", None, "",
                                "pending", tech="duplicate", dup_of=1)
        self.save(6, "pending", None)
        database.freelance_save(7, "kwork", "сводка", "u", "", "2026-10-24T00:00:00+00:00", "k7", 9, "{}",
                                "rejected", tech="assessed", verdict="ok", delivery="digest")
        self.save(8, "expired", None)    # устарел без отправки: не «отправлен»
        d = database.freelance_day_summary()
        self.assertEqual(d["total"], 8)
        self.assertEqual(d["sent"], 2)                      # карточки по времени отправки
        self.assertEqual(d["low_fit"], 2)                   # отклонены AI: fit 3 и 4
        self.assertEqual(d["good"], 3)                      # подходящие: 8, 9 и 9 из сводки
        self.assertEqual((d["card"], d["digest"]), (2, 1))
        self.assertEqual((d["duplicate"], d["queued"], d["stale"]), (1, 1, 1))
        self.assertEqual([m["fit"] for m in d["near_miss"]], [4, 3])

    def test_empty(self):
        d = database.freelance_day_summary()
        self.assertEqual((d["total"], d["near_miss"]), (0, []))


class GeminiDisabledTest(unittest.TestCase):
    def test_gemini_dropped_even_if_set_in_env(self):
        self.assertNotIn("gemini", ai_scorer._parse_chain("gemini,cloudflare,openrouter"))
        self.assertNotIn("gemini", ai_scorer._parse_chain(None))


# ---- БД журнала (миграция, слияние состояний, атомарность, чистка) ----


class DbCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = database.DB_PATH
        database.DB_PATH = os.path.join(self.tmp.name, "t.db")
        database.init_db()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(setattr, database, "DB_PATH", self.old_path)

    def save(self, oid, status="pending", fit=None, **kw):
        database.freelance_save(oid, "flru", f"заказ {oid}", f"https://x/{oid}", "1000 ₽",
                                datetime.now(timezone.utc).isoformat(), f"k{oid}", fit, "", status, **kw)
        return database.freelance_get(oid)


class MigrationTest(unittest.TestCase):
    """Старая БД (до журнала): строки получают состояния без выдуманной истории."""

    def test_backfill_old_rows(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "old.db")
        conn = sqlite3.connect(path)
        conn.execute("""CREATE TABLE freelance_orders (order_id INTEGER PRIMARY KEY, site TEXT, title TEXT,
            url TEXT, price_text TEXT, fit INTEGER, ai_json TEXT, published_at TEXT, dedup_key TEXT,
            seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, sent_at TIMESTAMP, attempts INTEGER DEFAULT 0,
            message_id INTEGER, status TEXT, category TEXT, budget REAL, hours REAL, deal_price REAL)""")
        rows = [
            (1, "pending", None, None, 0), (2, "pending", 8, None, 0), (3, "sent", 8, "2026-11-01 10:00:00", 0),
            (4, "taken", 9, "2026-11-01 10:00:00", 0), (5, "dismissed", 7, "2026-11-01 10:00:00", 0),
            (6, "expired", 8, "2026-11-01 10:00:00", 0), (7, "expired", None, None, 0),
            (8, "rejected", 3, None, 0), (9, "rejected", 9, None, 0), (10, "rejected", None, None, 0),
            (11, "pending", None, None, 2),
            (10_000_000_005, "sent", 8, "2026-11-01 10:00:00", 0),
        ]
        for oid, status, fit, sent_at, attempts in rows:
            conn.execute("INSERT INTO freelance_orders (order_id, site, title, url, fit, status, sent_at, attempts, "
                         "published_at, dedup_key) VALUES (?,?,?,?,?,?,?,?,?,?)",
                         (oid, "flru", f"t{oid}", "u", fit, status, sent_at, attempts, "2026-11-01T09:00:00+00:00", "k"))
        conn.commit()
        conn.close()
        old = database.DB_PATH
        database.DB_PATH = path
        self.addCleanup(setattr, database, "DB_PATH", old)
        database.init_db()
        database.init_db()   # идемпотентно
        g = {oid: database.freelance_get(oid) for oid, *_ in rows}

        def states(oid):
            r = g[oid]
            return r["tech"], r["delivery"], r["verdict"], r["lead"]

        self.assertEqual(states(1)[:2], ("queued", "none"))
        self.assertEqual(g[1]["queue_reason"], "ai_wait")
        self.assertEqual(g[11]["queue_reason"], "ai_error")
        self.assertEqual(states(2)[:2], ("assessed", "pending"))
        self.assertEqual(states(3), ("assessed", "card", "ok", None))
        self.assertEqual(g[3]["delivered_at"], "2026-11-01 10:00:00")
        self.assertEqual(states(4), ("assessed", "card", "ok", "agreed"))
        self.assertEqual(states(5), ("assessed", "card", "ok", "lost"))
        # expired после доставки: карточку удалили по сроку, время неизвестно - не выдумываем
        self.assertEqual(states(6)[:2], ("assessed", "card"))
        self.assertEqual(g[6]["card_deleted_at"], "unknown")
        # expired без доставки: устарел, причина старой записи
        self.assertEqual(g[7]["tech"], "stale")
        self.assertEqual(g[7]["stale_reason"], "legacy_expired")
        self.assertEqual(states(8)[:3], ("assessed", "none", "low_fit"))
        # rejected с высоким fit: сводка или дубль - различить нельзя
        self.assertEqual(states(9)[:3], ("assessed", "unknown", "ok"))
        self.assertEqual((g[10]["tech"], g[10]["reject_reason"]), ("legacy", "legacy_unknown"))
        self.assertEqual(g[10_000_000_005]["origin"], "flru")
        self.assertIsNone(g[2]["assessed_at"])   # время оценки неизвестно: не выдумываем


class SaveSemanticsTest(DbCase):
    def test_new_row_states_and_timestamps(self):
        r = self.save(1, "pending")
        self.assertEqual((r["tech"], r["queue_reason"], r["status"]), ("queued", "ai_wait", "pending"))
        r = self.save(1, "pending", fit=8)
        self.assertEqual((r["tech"], r["delivery"]), ("assessed", "pending"))
        self.assertTrue(r["assessed_at"])
        assessed_at = r["assessed_at"]
        r = self.save(1, "sent", fit=8, message_id=5)
        self.assertEqual((r["delivery"], r["status"]), ("card", "sent"))
        self.assertTrue(r["delivered_at"] and r["sent_at"])
        self.assertEqual(r["assessed_at"], assessed_at)

    def test_assessed_does_not_return_to_queue_and_card_not_rolled_back(self):
        self.save(2, "sent", fit=8, message_id=7)
        r = self.save(2, "pending")
        self.assertEqual((r["tech"], r["delivery"], r["message_id"]), ("assessed", "card", 7))

    def test_digest_is_not_rejected(self):
        r = self.save(3, "rejected", fit=9, delivery=jr.D_DIGEST, tech=jr.ASSESSED, verdict=jr.V_OK)
        self.assertEqual((r["tech"], r["delivery"], r["verdict"]), ("assessed", "digest", "ok"))
        self.assertTrue(r["delivered_at"])
        self.assertIsNone(r["sent_at"])

    def test_card_deletion_keeps_lead_and_states(self):
        self.save(4, "sent", fit=8, message_id=9)
        database.freelance_set_lead(4, jr.LEAD_SAVED)
        database.freelance_mark_card_deleted(4)
        r = database.freelance_get(4)
        self.assertEqual((r["tech"], r["delivery"], r["lead"]), ("assessed", "card", "saved"))
        self.assertTrue(r["card_deleted_at"])
        self.assertEqual(r["status"], "taken" if r["lead"] in ("applied", "agreed") else "expired")

    def test_lead_survives_later_saves(self):
        self.save(5, "sent", fit=8, message_id=1)
        database.freelance_set_status(5, "taken")
        r = self.save(5, "pending", fit=8)
        self.assertEqual(r["lead"], "agreed")

    def test_unknown_lead_rejected(self):
        self.save(6)
        with self.assertRaises(ValueError):
            database.freelance_set_lead(6, "почти")

    def test_mark_stale_keeps_assessment_and_does_not_touch_delivered(self):
        self.save(7, "pending", fit=8)
        database.freelance_mark_stale(7, jr.S_DELIVERY_EXPIRED)
        r = database.freelance_get(7)
        self.assertEqual((r["tech"], r["delivery"], r["fit"], r["stale_reason"]), ("stale", "none", 8, "delivery_expired"))
        self.save(8, "sent", fit=8, message_id=1)
        database.freelance_mark_stale(8, jr.S_QUEUE_EXPIRED)
        self.assertEqual(database.freelance_get(8)["tech"], "assessed")

    def test_stale_is_terminal_late_assessment_does_not_revive_delivery(self):
        self.save(40, "pending")
        database.freelance_mark_stale(40, jr.S_QUEUE_EXPIRED)
        r = self.save(40, "pending", fit=8)       # запоздавшая оценка
        self.assertEqual((r["tech"], r["delivery"], r["fit"]), ("stale", "none", 8))
        self.assertEqual(database.freelance_pending(), [])

    def test_stale_does_not_overwrite_concurrent_delivery(self):
        self.save(41, "pending", fit=8)
        real = database._update_states

        def deliver_then_update(order_id, _guard=None, **fields):
            self.save(41, "sent", fit=8, message_id=3)     # доставка между проверкой и записью
            return real(order_id, _guard, **fields)

        with mock.patch.object(database, "_update_states", deliver_then_update):
            database.freelance_mark_stale(41, jr.S_DELIVERY_EXPIRED)
        r = database.freelance_get(41)
        self.assertEqual((r["tech"], r["delivery"]), ("assessed", "card"))

    def test_mark_filtered_and_requeue_are_guarded(self):
        self.save(42, "sent", fit=8, message_id=1)
        database.freelance_mark_filtered(42, "no_include")
        self.assertEqual(database.freelance_get(42)["tech"], "assessed")
        self.save(43, "pending")
        database.freelance_requeue(43, "h", "s")                # не отсеян: очередь не трогаем
        self.assertEqual(database.freelance_get(43)["desc_changed"], 0)

    def test_expired_cards_query_skips_active_leads_and_marks_not_repeat(self):
        for oid in (10, 11):
            self.save(oid, "sent", fit=8, message_id=oid)
        database.freelance_set_lead(11, jr.LEAD_APPLIED)
        with database._connect() as conn:
            conn.execute("UPDATE freelance_orders SET sent_at = datetime('now', '-30 hours')")
        self.assertEqual([r["order_id"] for r in database.freelance_expired_messages(24)], [10])
        database.freelance_mark_card_deleted(10)
        self.assertEqual(database.freelance_expired_messages(24), [])

    def test_cleanup_keeps_active_leads_and_deletes_old_journal(self):
        for oid in (20, 21, 22):
            self.save(oid, "sent", fit=8, message_id=oid)
        database.freelance_set_lead(21, jr.LEAD_DISCUSSING)
        database.freelance_set_lead(22, jr.LEAD_LOST, "дорого")
        with database._connect() as conn:
            conn.execute("UPDATE freelance_orders SET seen_at = datetime('now', '-40 days')")
        database.cleanup_old()
        self.assertIsNone(database.freelance_get(20))
        self.assertIsNotNone(database.freelance_get(21))     # активный лид не теряется при очистке
        self.assertIsNone(database.freelance_get(22))        # закрытый отказом - обычный срок журнала

    def test_pending_sorted_by_publication_not_by_order_id(self):
        now = datetime.now(timezone.utc)
        for oid, hours in ((30, 5), (10_000_000_031, 1), (32, 3)):
            database.freelance_save(oid, "flru", "t", "u", "", (now - timedelta(hours=hours)).isoformat(),
                                    "k", None, "", "pending")
        self.assertEqual([r["order_id"] for r in database.freelance_pending()], [10_000_000_031, 32, 30])


if __name__ == "__main__":
    unittest.main()
