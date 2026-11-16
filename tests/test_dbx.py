"""БД этапа 2: фактические часы, версии оценки, очередь повторной оценки (новые таблицы).

Запуск из корня проекта: python -m unittest tests.test_dbx -v
"""
import unittest

import database
from freelance import journal as jr, pricing
from tests.test_pipeline_cycle import CycleTestBase


class ActualsTest(CycleTestBase):
    def test_actuals_validation_and_join(self):
        database.freelance_save(501, "flru", "Бот", "https://x/501", "", "2026-11-16T10:00:00", "k501", 8, "{}",
                                "pending", 0, None, category="script", hours=4.0)
        self.assertTrue(database.freelance_set_actuals(501, 6.0, 800))
        self.assertTrue(database.freelance_set_actuals(501, expenses=900))          # часы не затираются
        rows = database.freelance_actuals_rows()
        self.assertEqual((rows[0]["hours_est"], rows[0]["hours_actual"], rows[0]["expenses"]), (4.0, 6.0, 900.0))
        for bad in ((501, -1, None), (501, float("nan"), None), (501, "abc", None), (501, None, None), (999, 2, None)):
            self.assertFalse(database.freelance_set_actuals(*bad), bad)
        self.assertEqual(database.freelance_actuals_rows()[0]["hours_actual"], 6.0)

    def test_calibration_from_rows(self):
        for oid in range(510, 515):
            database.freelance_save(oid, "flru", "Бот", f"https://x/{oid}", "", "2026-11-16T10:00:00", f"k{oid}", 8,
                                    "{}", "pending", 0, None, category="script", hours=4.0)
            database.freelance_set_actuals(oid, 6.0)
        calibration = pricing.hours_calibration(database.freelance_actuals_rows())
        self.assertEqual(calibration["script"], {"n": 5, "ratio": 1.5})


class AssessmentHistoryTest(CycleTestBase):
    def test_version_is_recorded_once(self):
        self.assertTrue(database.freelance_record_assessment(601, "h1", 8, "m", "{}"))
        self.assertFalse(database.freelance_record_assessment(601, "h1", 3, "m2", "{}"))
        self.assertFalse(database.freelance_record_assessment(601, "", 8, "m", "{}"))
        self.assertEqual(database.freelance_assessment_get(601, "h1")["fit"], 8)

    def test_replace_requires_same_text_version(self):
        database.freelance_save(602, "flru", "Бот", "https://x/602", "", "2026-11-16T10:00:00", "k", 8, "{}",
                                "pending", 0, None, content_hash="v1", tech=jr.ASSESSED)
        self.assertFalse(database.freelance_replace_assessment(602, "other", 3, "{}", None, None))
        self.assertEqual(database.freelance_get(602)["fit"], 8)
        self.assertTrue(database.freelance_replace_assessment(602, "v1", 3, "{}", "script", 2.0))
        self.assertEqual(database.freelance_get(602)["fit"], 3)

    def test_reassess_queue_same_hash_is_idempotent(self):
        self.assertTrue(database.freelance_reassess_queue(603, "h", 0.5, True, 8, 4000))
        self.assertFalse(database.freelance_reassess_queue(603, "h", 0.5, True, 5, 1))
        self.assertEqual(database.freelance_reassess_state(603)["old_fit"], 8)


if __name__ == "__main__":
    unittest.main()
