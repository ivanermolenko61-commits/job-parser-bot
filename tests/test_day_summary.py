"""Суточная сводка фриланса и отключение Gemini. Без сети.

Запуск из корня проекта: python -m unittest tests.test_day_summary -v
"""
import os
import tempfile
import unittest

import database
from freelance import ai_scorer


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


if __name__ == "__main__":
    unittest.main()
