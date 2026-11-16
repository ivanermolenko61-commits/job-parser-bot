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

    def test_triage_miss_is_share_of_all_good_in_trimmed_groups(self):
        # ёмкости с запасом: потери только от триажа, и они равны заданной доле подходящих группы
        r = capacity.simulate_queue(100, 500, {1: 0.0, 2: 0.0, 3: 1.0}, {1: 0.0, 2: 0.0, 3: 0.2},
                                    skip_share={3: 0.4}, triage_miss=0.05)
        self.assertAlmostEqual(r["good_total"], 100 * r["days"] * 0.2, delta=0.1)
        self.assertAlmostEqual(r["triage_good_lost"], r["good_total"] * 0.05, delta=0.1)
        self.assertAlmostEqual(r["loss_total_share"], 0.05, delta=0.002)
        self.assertEqual(r["good_lost"], 0)

    def test_perfect_triage_keeps_all_good_orders_in_queue(self):
        # триаж без ошибок снимает только неподходящие: все подходящие остаются в очереди (доля выше исходной)
        r = capacity.simulate_queue(127, 86, MIX, GOOD, skip_share={3: 0.4, 2: 0.25}, triage_miss=0.0)
        self.assertEqual(r["triage_good_lost"], 0.0)
        self.assertAlmostEqual(r["good_arrived"], r["good_total"], delta=3)
        plain = capacity.simulate_queue(127, 86, MIX, GOOD, skip_share={3: 0.4, 2: 0.25}, triage_miss=0.05)
        self.assertLess(plain["good_arrived"], r["good_arrived"])

    def test_no_triage_means_no_triage_losses(self):
        none = capacity.simulate_queue(127, 86, MIX, GOOD, triage_miss=0.5)
        self.assertEqual(none["triage_good_lost"], 0.0)

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
        self.assertIn("ДОПУЩЕНИЯ", text)
        self.assertIn("потеряно подходящих", text)
        self.assertIn("СЦЕНАРИИ", text)
        self.assertIn("OpenRouter не отвечает", text)
        self.assertIn("БЕЗ триажа", text)
        self.assertIn("у триажа", text)


class TriageEvidenceStaleTest(CycleTestBase):
    def fill(self, labeled=True):
        for i in range(pipeline.TRIAGE_MIN_SAMPLE):
            pipeline._record_triage_evidence(miss=False, good=(i < 60), order_id=100 + i, triage=7)
        if labeled:
            for i in range(pipeline.TRIAGE_MIN_LABELED):
                pipeline.record_triage_label(100 + i, True)

    def test_clean_evidence_allows(self):
        self.fill()
        pipeline.approve_triage(pipeline.TRIAGE_AUDIT_MIN, 0)
        self.assertTrue(pipeline.triage_allowed()[0])

    def test_model_change_discards_statistics(self):
        self.fill()
        pipeline.approve_triage(pipeline.TRIAGE_AUDIT_MIN, 0)
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


class OwnerApprovalTest(CycleTestBase):
    """Числа в настройках сервера не включают отсечение без подтверждения владельца по выборке отсекаемых."""

    def fill_all(self):
        for i in range(pipeline.TRIAGE_MIN_SAMPLE):
            pipeline._record_triage_evidence(miss=False, good=(i < pipeline.TRIAGE_MIN_GOOD), order_id=100 + i, triage=7)
        for i in range(pipeline.TRIAGE_MIN_LABELED):
            pipeline.record_triage_label(100 + i, True)

    def test_thresholds_without_approval_stay_shadow(self):
        self.fill_all()
        with mock.patch.object(pipeline, "TRIAGE_MODE", "on"):
            self.assertEqual(pipeline.effective_triage_mode(), "shadow")
        self.assertFalse(pipeline.triage_approval()["valid"])

    def test_too_small_or_positive_sample_does_not_approve(self):
        self.fill_all()
        pipeline.approve_triage(pipeline.TRIAGE_AUDIT_MIN - 1, 0)
        self.assertFalse(pipeline.triage_allowed()[0])
        pipeline.approve_triage(pipeline.TRIAGE_AUDIT_MIN, 1)
        self.assertIn("подходящих среди отсекаемых", pipeline.triage_allowed()[1])
        self.assertIn("некорректны", pipeline.approve_triage(5, 9))

    def test_approval_is_dropped_when_settings_change(self):
        self.fill_all()
        pipeline.approve_triage(pipeline.TRIAGE_AUDIT_MIN, 0)
        self.assertTrue(pipeline.triage_allowed()[0])
        with mock.patch.object(pipeline, "TRIAGE_REJECT_MAX", pipeline.TRIAGE_REJECT_MAX + 1):
            self.assertFalse(pipeline.triage_approval()["valid"])

    def test_audit_sample_contains_only_would_cut_orders(self):
        for oid, tr in ((1, 1), (2, 7), (3, 2)):
            database.freelance_save(oid, "flru", f"Заказ {oid}", f"https://x/{oid}", "", "2026-11-16T10:00:00",
                                    f"k{oid}", 5, "", "pending", 0, None)
            pipeline._record_triage_evidence(miss=False, good=False, order_id=oid, triage=tr)
        ids = {r["order_id"] for r in pipeline.triage_audit_sample(10)}
        self.assertEqual(ids, {1, 3})

    def test_report_names_requested_effective_reason_and_counters(self):
        self.fill_all()
        with mock.patch.object(pipeline, "TRIAGE_MODE", "on"):
            text = pipeline.triage_report()
        self.assertIn("запрошен on, применяется shadow", text)
        self.assertIn("заблокировано", text)
        self.assertIn(f"сравнений с полной оценкой {pipeline.TRIAGE_MIN_SAMPLE}/", text)
        self.assertIn("отметки владельца", text)

    def test_report_marks_stale_statistics(self):
        self.fill_all()
        with mock.patch.object(ai_scorer, "TRIAGE_MODEL", "другая-модель"):
            self.assertIn("устарела", pipeline.triage_report())

    def test_control_slice_still_scored_in_on_mode(self):
        self.fill_all()
        pipeline.approve_triage(pipeline.TRIAGE_AUDIT_MIN, 0)
        from tests.test_journal import order
        from tests.test_pipeline_cycle import ai_result
        o_cut = order(1101, site="youdoru")      # не контрольный: отсекается
        o_ctl = order(1100, "Парсер цен конкурентов в таблицу", "Нужен скрипт на Python, который собирает цены с сайтов конкурентов раз в сутки и пишет их в Google Таблицу с историей изменений.", site="youdoru")      # id кратен TRIAGE_CONTROL_EVERY: оценивается полной моделью
        database.kv_set(pipeline.LAST_ID_KEY, 100)
        scored = []

        def full(o):
            scored.append(o.order_id)
            return ai_result(9)
        with mock.patch.object(pipeline, "TRIAGE_MODE", "on"),                 mock.patch.object(pipeline, "_triage", return_value=1),                 mock.patch.object(pipeline, "fetch_new_orders", return_value=self.scan([o_cut, o_ctl])),                 mock.patch.object(pipeline.ai_scorer, "score_order", side_effect=full):
            pipeline._prepare_cycle()
        self.assertEqual(scored, [1100])
        # контрольный заказ дал пропуск (быстрая 1, полная 9): отсечение снова заблокировано
        self.assertEqual(pipeline.effective_triage_mode(), "shadow")


class OwnerLabelTest(CycleTestBase):
    def test_label_miss_blocks_triage(self):
        for i in range(pipeline.TRIAGE_MIN_SAMPLE):
            pipeline._record_triage_evidence(miss=False, good=(i < 60), order_id=100 + i, triage=1 if i == 0 else 7)
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
