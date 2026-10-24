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
        self.save(1, "sent", 8)
        self.save(2, "taken", 9)
        self.save(3, "rejected", 3)
        self.save(4, "rejected", 4)
        self.save(5, "rejected", None)   # дубль: оценки нет
        self.save(6, "pending", None)
        self.save(7, "rejected", 9)      # показан сводкой по лимиту: не «отсеян AI»
        self.save(8, "expired", None)    # просрочен без отправки: не «отправлен»
        d = database.freelance_day_summary()
        self.assertEqual(d["total"], 8)
        self.assertEqual(d["sent"], 2)
        self.assertEqual(d["rejected_ai"], 2)
        self.assertEqual(d["rejected_other"], 3)
        self.assertEqual(d["pending"], 1)
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
