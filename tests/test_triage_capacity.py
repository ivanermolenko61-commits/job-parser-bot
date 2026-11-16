"""Ёмкость очереди (симуляция) и защита триажа от устаревшей статистики. Без сети.

Запуск из корня проекта: python -m unittest tests.test_triage_capacity -v
"""
import unittest
from datetime import date
from unittest import mock

import database
from freelance import ai_scorer, capacity, pipeline
from tests.test_journal import approve_audit, seed_cut_orders
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
        approve_audit()
        self.assertTrue(pipeline.triage_allowed()[0])

    def test_model_change_discards_statistics(self):
        self.fill()
        approve_audit()
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
    """Числа в настройках сервера не включают отсечение без подтверждения владельца по выданной выборке отсекаемых."""

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

    def test_too_small_sample_does_not_approve(self):
        self.fill_all()
        approve_audit(pipeline.TRIAGE_AUDIT_MIN - 10)
        self.assertEqual(pipeline.triage_approval()["checked"], pipeline.TRIAGE_AUDIT_MIN - 10)
        self.assertFalse(pipeline.triage_allowed()[0])

    def test_suitable_order_in_sample_blocks(self):
        self.fill_all()
        seed_cut_orders(pipeline.TRIAGE_AUDIT_MIN + 10)
        found = False
        while pipeline.triage_approval()["checked"] < pipeline.TRIAGE_AUDIT_MIN:
            b = pipeline.triage_audit_sample(20)
            pipeline.approve_triage(b["code"], [] if found else [b["rows"][0]["order_id"]])
            found = True
        self.assertEqual(pipeline.triage_approval()["suitable"], 1)
        self.assertIn("подходящих среди отсекаемых", pipeline.triage_allowed()[1])

    def test_arbitrary_numbers_and_foreign_ids_do_not_approve(self):
        self.fill_all()
        self.assertIn("Нет выданной партии", pipeline.approve_triage("abcd", []))
        seed_cut_orders(30)
        b = pipeline.triage_audit_sample(20)
        self.assertIn("Код партии не совпадает", pipeline.approve_triage("0000", []))
        self.assertIn("не из выданной партии", pipeline.approve_triage(b["code"], [999999]))
        self.assertEqual(pipeline.triage_approval()["checked"], 0)
        self.assertIn("Партия принята", pipeline.approve_triage(b["code"].upper(), []))
        self.assertEqual(pipeline.triage_approval()["checked"], 20)
        # та же партия второй раз не засчитывается
        self.assertIn("Нет выданной партии", pipeline.approve_triage(b["code"], []))
        self.assertEqual(pipeline.triage_approval()["checked"], 20)

    def test_repeated_audit_does_not_add_unique_observations(self):
        seed_cut_orders(60)
        b1 = pipeline.triage_audit_sample(20)
        b2 = pipeline.triage_audit_sample(20)
        self.assertEqual([r["order_id"] for r in b1["rows"]], [r["order_id"] for r in b2["rows"]])
        self.assertEqual(b1["code"], b2["code"])
        ap = pipeline.triage_approval()
        self.assertEqual((ap["issued"], ap["checked"], ap["pending"]), (20, 0, 20))
        pipeline.approve_triage(b1["code"], [])
        b3 = pipeline.triage_audit_sample(20)
        self.assertFalse({r["order_id"] for r in b1["rows"]} & {r["order_id"] for r in b3["rows"]})
        self.assertEqual(pipeline.triage_approval()["issued"], 40)

    def test_sample_is_unique_subset_of_would_cut_orders_only(self):
        for oid, tr in ((1, 1), (2, 7), (3, 2)):
            database.freelance_save(oid, "flru", f"Заказ {oid}", f"https://x/{oid}", "", "2026-11-16T10:00:00",
                                    f"k{oid}", 5, "", "pending", 0, None)
            pipeline._record_triage_evidence(miss=False, good=False, order_id=oid, triage=tr)
        b = pipeline.triage_audit_sample(10)
        self.assertEqual({r["order_id"] for r in b["rows"]}, {1, 3})
        self.assertTrue(all("fit" not in r for r in b["rows"]))   # полная оценка модели владельцу не подсказывается
        pipeline.approve_triage(b["code"], [])
        seed_cut_orders(40)
        ids = []
        while True:
            nb = pipeline.triage_audit_sample(20)
            if not nb["rows"]:
                break
            ids += [r["order_id"] for r in nb["rows"]]
            pipeline.approve_triage(nb["code"], [])
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(set(ids), {70000 + i for i in range(40)})

    def test_approval_is_dropped_when_settings_change(self):
        self.fill_all()
        approve_audit()
        self.assertTrue(pipeline.triage_allowed()[0])
        with mock.patch.object(pipeline, "TRIAGE_REJECT_MAX", pipeline.TRIAGE_REJECT_MAX + 1):
            self.assertFalse(pipeline.triage_approval()["valid"])
            self.assertEqual(pipeline.triage_approval()["issued"], 0)

    def test_pending_batch_is_dropped_when_settings_change(self):
        seed_cut_orders(30)
        b = pipeline.triage_audit_sample(20)
        with mock.patch.object(ai_scorer, "TRIAGE_MODEL", "другая-модель"):
            self.assertIn("Нет выданной партии", pipeline.approve_triage(b["code"], []))

    def test_report_names_requested_effective_reason_and_counters(self):
        self.fill_all()
        with mock.patch.object(pipeline, "TRIAGE_MODE", "on"):
            text = pipeline.triage_report()
        self.assertIn("запрошен on, применяется shadow", text)
        self.assertIn("заблокировано", text)
        self.assertIn(f"сравнений с полной оценкой {pipeline.TRIAGE_MIN_SAMPLE}/", text)
        self.assertIn("отметки владельца", text)
        self.assertIn("лишних оценок", text)
        self.assertIn("контрольный срез", text)

    def test_report_marks_stale_statistics(self):
        self.fill_all()
        with mock.patch.object(ai_scorer, "TRIAGE_MODEL", "другая-модель"):
            self.assertIn("устарела", pipeline.triage_report())

    def test_three_measures_are_counted_separately(self):
        rec = pipeline._record_triage_evidence
        rec(miss=False, good=False, order_id=1, triage=1)            # отсечён, действительно не подходит
        rec(miss=True, good=True, order_id=2, triage=2)              # отсечён, но подходит: пропуск
        rec(miss=False, good=True, order_id=3, triage=8)             # пропущен триажем, подходит: верно
        rec(miss=False, good=False, order_id=4, triage=9)            # пропущен триажем, полная отклонила: лишний расход
        ev = pipeline.triage_evidence()
        self.assertEqual((ev["n"], ev["cut"], ev["miss"], ev["good"], ev["extra"]), (4, 2, 1, 2, 1))
        self.assertEqual((ev["ctl_cut"], ev["ctl_miss"]), (0, 0))
        rec(miss=True, good=True, order_id=5, triage=1, control=True)
        rec(miss=False, good=False, order_id=6, triage=8, control=True)   # контроль, но не отсекаемый: в срез отсечений не входит
        ev = pipeline.triage_evidence()
        self.assertEqual((ev["ctl_cut"], ev["ctl_miss"]), (1, 1))

    def test_control_slice_is_about_a_tenth_stable_and_tied_to_settings(self):
        ids = range(5000, 7000)
        first = [i for i in ids if pipeline._is_control(i)]
        self.assertEqual(first, [i for i in ids if pipeline._is_control(i)])   # без состояния: тот же ответ после рестарта
        self.assertTrue(160 <= len(first) <= 240, len(first))
        with mock.patch.object(ai_scorer, "TRIAGE_MODEL", "другая-модель"):
            self.assertNotEqual(first, [i for i in ids if pipeline._is_control(i)])

    def _pick(self, want_control, start):
        i = start
        while pipeline._is_control(i) != want_control:
            i += 1
        return i

    def _cycle(self, orders, triage_value=1, full_fit=9):
        from tests.test_pipeline_cycle import ai_result
        database.kv_set(pipeline.LAST_ID_KEY, 100)
        scored = []

        def full(o):
            scored.append(o.order_id)
            return ai_result(full_fit)
        with mock.patch.object(pipeline, "TRIAGE_MODE", "on"), \
                mock.patch.object(pipeline, "_triage", return_value=triage_value), \
                mock.patch.object(pipeline, "fetch_new_orders", return_value=self.scan(orders)), \
                mock.patch.object(pipeline.ai_scorer, "score_order", side_effect=full):
            pipeline._prepare_cycle()
        return scored

    def test_control_slice_scored_only_among_would_cut_and_miss_keeps_block(self):
        from tests.test_journal import order
        self.fill_all()
        approve_audit()
        cut_id, ctl_id = self._pick(False, 1101), self._pick(True, 1101)
        o_cut = order(cut_id, f"Заказ {cut_id}: разбор выписок банка",
                      f"Нужно разобрать банковские выписки {cut_id} и свести их в одну таблицу с категориями расходов.",
                      site="youdoru")
        o_ctl = order(ctl_id, "Парсер цен конкурентов в таблицу",
                      "Нужен скрипт на Python, который собирает цены с сайтов конкурентов раз в сутки и пишет их в "
                      "Google Таблицу с историей изменений.", site="youdoru")
        scored = self._cycle([o_cut, o_ctl])
        self.assertEqual(scored, [ctl_id])                 # отсекаемый неконтрольный не оценивался, контрольный - да
        ev = pipeline.triage_evidence()
        self.assertEqual((ev["ctl_cut"], ev["ctl_miss"]), (1, 1))
        with mock.patch.object(pipeline, "TRIAGE_MODE", "on"):
            self.assertEqual(pipeline.effective_triage_mode(), "shadow")   # пропуск вернул shadow

    def test_miss_block_survives_restart(self):
        self.fill_all()
        approve_audit()
        pipeline._record_triage_evidence(miss=True, good=True, order_id=77, triage=1, control=True)
        pipeline._triage_warned["on"] = False        # «рестарт»: память процесса пуста, состояние - только в БД
        pipeline._triage_cache.clear()
        with mock.patch.object(pipeline, "TRIAGE_MODE", "on"):
            self.assertEqual(pipeline.effective_triage_mode(), "shadow")
            self.assertIn("пропуски", pipeline.triage_allowed()[1])
        # продолжение работы пропуск не обнуляет
        pipeline._record_triage_evidence(miss=False, good=True, order_id=78, triage=8)
        self.assertEqual(pipeline.triage_evidence()["miss"], 1)


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
