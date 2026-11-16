"""Ёмкость очереди (симуляция) и защита триажа от устаревшей статистики. Без сети.

Запуск из корня проекта: python -m unittest tests.test_triage_capacity -v
"""
import unittest
from datetime import date
from unittest import mock

import database
from freelance import ai_scorer, capacity, pipeline
from tests.test_pipeline_cycle import CycleTestBase

MIX = {1: 0.25, 2: 0.35, 3: 0.40}
GOOD = {1: 0.4, 2: 0.2, 3: 0.08}


class SimulateQueueTest(unittest.TestCase):
    def test_capacity_above_demand_loses_almost_nothing(self):
        r = capacity.simulate_queue(90, 120, MIX, GOOD)
        self.assertEqual(r["expired"], 0)
        self.assertEqual(r["groups"][1]["expired"], 0)
        self.assertLessEqual(r["groups"][1]["wait_p90_h"], 3)

    def test_deficit_squeezes_out_lowest_priority_first(self):
        r = capacity.simulate_queue(127, 86, MIX, GOOD)
        g = r["groups"]
        self.assertEqual(g[1]["expired"], 0)                    # профильные не теряются
        self.assertGreater(g[3]["expired"], 0)                  # дефицит бьёт по группе 3
        self.assertGreater(g[3]["expired"], g[2]["expired"])
        self.assertGreater(r["good_lost"], 0)

    def test_total_quota_outage_loses_most(self):
        low = capacity.simulate_queue(127, 45, MIX, GOOD)
        base = capacity.simulate_queue(127, 86, MIX, GOOD)
        self.assertGreater(low["good_lost_share"], base["good_lost_share"])

    def test_skipping_part_of_flow_before_full_scoring_reduces_expiry(self):
        base = capacity.simulate_queue(127, 86, MIX, GOOD)
        skipped = capacity.simulate_queue(127, 86, MIX, GOOD, skip_share={3: 0.4})
        self.assertLess(skipped["expired"], base["expired"])

    def test_deterministic(self):
        self.assertEqual(capacity.simulate_queue(127, 86, MIX, GOOD), capacity.simulate_queue(127, 86, MIX, GOOD))


class OverheadTest(CycleTestBase):
    def test_none_without_journal(self):
        self.assertIsNone(capacity.measure_overhead())

    def test_failed_attempts_per_order(self):
        for oid, attempts in ((1, 0), (2, 2), (3, 0), (4, 0)):
            database.freelance_save(oid, "flru", f"Заказ {oid}", f"https://x/{oid}", "1000 ₽", "2026-11-16T10:00:00",
                                    f"k{oid}", None, "", "pending", attempts, None)
        self.assertAlmostEqual(capacity.measure_overhead(30), 0.5)

    def test_overhead_lowers_net_capacity(self):
        status = {"providers": {"cloudflare": {"has_key": True, "enabled": True, "dead": False, "limit": 50},
                                "openrouter": {"has_key": True, "enabled": True, "dead": False, "limit": 45}}}
        zero = capacity.forecast(status, 127, 0, date(2026, 11, 13), date(2026, 11, 20), overhead=0.0)
        some = capacity.forecast(status, 127, 0, date(2026, 11, 13), date(2026, 11, 20), overhead=0.25)
        self.assertEqual(zero["net_without_yandex"], 95)
        self.assertEqual(some["net_without_yandex"], 76)
        self.assertTrue(some["overhead_measured"])
        self.assertFalse(capacity.forecast(status, 127, 0, date(2026, 11, 13), date(2026, 11, 20))["overhead_measured"])


class CapacityToolTest(CycleTestBase):
    def test_report_prints_simulation_and_names_assumptions(self):
        import contextlib
        import io
        from tools import capacity_report
        buf = io.StringIO()
        status = {"providers": {"cloudflare": {"has_key": True, "enabled": True, "dead": False, "limit": 50},
                                "openrouter": {"has_key": True, "enabled": True, "dead": False, "limit": 45}}}
        with mock.patch.object(capacity, "demand_per_day", return_value=127.0), \
                mock.patch.object(ai_scorer, "status", return_value=status), \
                contextlib.redirect_stdout(buf):
            self.assertEqual(capacity_report.main(["--db", database.DB_PATH]), 0)
        text = buf.getvalue()
        self.assertIn("симуляция очереди", text)
        self.assertIn("ДОПУЩЕНИЕ", text)
        self.assertIn("потеряно подходящих", text)
        self.assertIn("НЕ учтены", text)


class TriageEvidenceStaleTest(CycleTestBase):
    def fill(self, labeled=True):
        for i in range(pipeline.TRIAGE_MIN_SAMPLE):
            pipeline._record_triage_evidence(miss=False, good=(i < 30), order_id=100 + i, triage=7)
        if labeled:
            for i in range(pipeline.TRIAGE_MIN_LABELED):
                pipeline.record_triage_label(100 + i, True)

    def test_clean_evidence_allows(self):
        self.fill()
        self.assertTrue(pipeline.triage_allowed()[0])

    def test_model_change_discards_statistics(self):
        self.fill()
        with mock.patch.object(ai_scorer, "TRIAGE_MODEL", "другая-модель"):
            ok, why = pipeline.triage_allowed()
            self.assertFalse(ok)
            self.assertIn("другой модели", why)
            self.assertEqual(pipeline.triage_evidence()["n"], 0)

    def test_prompt_change_discards_statistics(self):
        self.fill()
        with mock.patch.object(ai_scorer, "TRIAGE_PROMPT", ai_scorer.TRIAGE_PROMPT + " ещё"):
            self.assertFalse(pipeline.triage_allowed()[0])

    def test_threshold_change_discards_statistics(self):
        self.fill()
        with mock.patch.object(pipeline, "TRIAGE_REJECT_MAX", pipeline.TRIAGE_REJECT_MAX + 1):
            self.assertFalse(pipeline.triage_allowed()[0])
        with mock.patch.object(pipeline, "FREELANCE_MIN_FIT", pipeline.FREELANCE_MIN_FIT + 1):
            self.assertFalse(pipeline.triage_allowed()[0])

    def test_full_scoring_prompt_change_discards_statistics(self):
        self.fill()
        with mock.patch.object(ai_scorer, "SYSTEM_PROMPT", ai_scorer.SYSTEM_PROMPT + " ещё"):
            self.assertFalse(pipeline.triage_allowed()[0])

    def test_new_records_after_change_start_from_zero_and_restore_after_return(self):
        self.fill()
        with mock.patch.object(ai_scorer, "TRIAGE_MODEL", "другая-модель"):
            pipeline._record_triage_evidence(miss=False, good=True, order_id=1, triage=5)
            self.assertEqual(pipeline.triage_evidence()["n"], 1)
        # вернули прежние настройки: старые счётчики перезаписаны, прежний набор не воскресает
        self.assertEqual(pipeline.triage_evidence()["n"], 0)
        self.assertFalse(pipeline.triage_allowed()[0])


class OwnerLabelTest(CycleTestBase):
    def test_label_miss_blocks_triage(self):
        for i in range(pipeline.TRIAGE_MIN_SAMPLE):
            pipeline._record_triage_evidence(miss=False, good=(i < 30), order_id=100 + i, triage=1 if i == 0 else 7)
        for i in range(pipeline.TRIAGE_MIN_LABELED):
            pipeline.record_triage_label(100 + i, True)   # у заказа 100 быстрая оценка 1: пропуск
        ok, why = pipeline.triage_allowed()
        self.assertFalse(ok)
        self.assertIn("разметка владельца", why)

    def test_label_counted_once_per_order(self):
        pipeline._record_triage_evidence(miss=False, good=True, order_id=5, triage=8)
        self.assertEqual(pipeline.record_triage_label(5, True), "good")
        self.assertIsNone(pipeline.record_triage_label(5, True))
        self.assertEqual(pipeline.triage_evidence()["lab_good"], 1)

    def test_label_without_triage_score_ignored(self):
        self.assertIsNone(pipeline.record_triage_label(404, True))
        self.assertEqual(pipeline.triage_evidence()["lab_good"], 0)

    def test_label_for_score_from_other_settings_ignored(self):
        pipeline._record_triage_evidence(miss=False, good=True, order_id=6, triage=8)
        with mock.patch.object(ai_scorer, "TRIAGE_MODEL", "другая-модель"):
            self.assertIsNone(pipeline.record_triage_label(6, True))

    def test_rejected_label_counted_separately(self):
        pipeline._record_triage_evidence(miss=False, good=False, order_id=7, triage=1)
        self.assertEqual(pipeline.record_triage_label(7, False), "bad")
        ev = pipeline.triage_evidence()
        self.assertEqual((ev["lab_bad"], ev["lab_good"], ev["lab_miss"]), (1, 0, 0))


if __name__ == "__main__":
    unittest.main()
