"""Цикл фриланса: первый запуск берёт мало id, оценка сохраняется сразу. Без сети.

Запуск из корня проекта: python -m unittest tests.test_pipeline_cycle -v
"""
import os
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

import database
from freelance import alot_client, pipeline


def make_order(oid, title="Создать телеграм бота"):
    return alot_client.FreelanceOrder(oid, "youdoru", title, "нужен бот", "5000 ₽", 5000.0, [],
                                      datetime.now(timezone.utc), f"https://x/{oid}")


def ai_result(fit):
    return {"fit": fit, "difficulty": "легко", "hours": 4.0, "category": "tg_bot",
            "components": [{"name": "бот", "hours": 4}], "unknowns": [], "clarity": "ясно",
            "client_budget_ok": True, "summary": "s", "risks": "", "questions": [],
            "model": "test:m"}


class CycleTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = database.DB_PATH
        database.DB_PATH = os.path.join(self.tmp.name, "t.db")
        database.init_db()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(setattr, database, "DB_PATH", self.old_path)
        for p in (mock.patch.object(pipeline.monitor, "record"),
                  mock.patch.object(pipeline, "_report_ai_health"),
                  mock.patch.object(pipeline.ai_scorer, "is_enabled", return_value=True),
                  mock.patch.object(pipeline.ai_scorer, "daily_capacity", return_value=None)):
            p.start()
            self.addCleanup(p.stop)

    def scan(self, orders):
        return alot_client.ScanResult(orders=orders, max_id=max([o.order_id for o in orders] or [0]),
                                      raw_count=len(orders))


class FirstRunTest(CycleTestBase):
    def test_first_run_takes_last_50_ids(self):
        with mock.patch.object(pipeline, "find_upper_bound", return_value=5000), \
                mock.patch.object(pipeline, "fetch_new_orders", return_value=self.scan([])) as fetch, \
                mock.patch.object(pipeline, "_load_pending", return_value={}):
            pipeline._prepare_cycle()
        self.assertEqual(pipeline.FIRST_RUN_LOOKBACK, 50)
        fetch.assert_called_once_with(4950, max_ahead=350)


class SaveRightAfterScoringTest(CycleTestBase):
    def test_scored_order_saved_pending_and_not_rescored(self):
        order = make_order(101)
        with mock.patch.object(pipeline, "fetch_new_orders", return_value=self.scan([order])), \
                mock.patch.object(pipeline.ai_scorer, "score_order", return_value=ai_result(8)) as score:
            database.kv_set(pipeline.LAST_ID_KEY, 100)
            res = pipeline._prepare_cycle()
        self.assertEqual(score.call_count, 1)
        row = database.freelance_get(101)       # в БД ещё до finish_cycle
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["fit"], 8)
        self.assertIn("price_mid", row["ai_json"])
        self.assertEqual([s.order.order_id for s in res.to_send], [101])

        # «рестарт» до finish_cycle: следующий цикл берёт оценку из БД и не оценивает заново
        with mock.patch.object(pipeline, "fetch_new_orders", return_value=self.scan([])), \
                mock.patch.object(pipeline, "_fetch_items", return_value=[{"stub": 1}]), \
                mock.patch.object(pipeline, "_parse_item", return_value=order), \
                mock.patch.object(pipeline.ai_scorer, "score_order") as score2:
            res2 = pipeline._prepare_cycle()
        score2.assert_not_called()
        self.assertEqual(res2.pending_loaded, 1)
        self.assertEqual([s.order.order_id for s in res2.to_send], [101])   # не отброшен как дубль

    def test_finish_cycle_overwrites_status(self):
        order = make_order(102)
        database.kv_set(pipeline.LAST_ID_KEY, 100)
        with mock.patch.object(pipeline, "fetch_new_orders", return_value=self.scan([order])), \
                mock.patch.object(pipeline.ai_scorer, "score_order", return_value=ai_result(2)):
            res = pipeline._prepare_cycle()
        self.assertEqual(database.freelance_get(102)["status"], "pending")
        self.assertEqual(len(res.rejected), 1)
        pipeline.finish_cycle(res, [], [], [])
        self.assertEqual(database.freelance_get(102)["status"], "rejected")

    def test_early_save_keeps_taken_status(self):
        order = make_order(103)
        database.kv_set(pipeline.LAST_ID_KEY, 100)
        sc = pipeline.ScoredOrder(order, None, "k103")
        pipeline.save_order(sc, "pending")

        def score(o):
            database.freelance_set_status(103, "taken")   # пока оценивали, нажали «Взял»
            return ai_result(8)

        with mock.patch.object(pipeline, "fetch_new_orders", return_value=self.scan([])), \
                mock.patch.object(pipeline, "_load_pending", return_value={103: sc}), \
                mock.patch.object(pipeline.ai_scorer, "score_order", side_effect=score):
            pipeline._prepare_cycle()
        row = database.freelance_get(103)
        self.assertEqual(row["status"], "taken")
        self.assertEqual(row["fit"], 8)

    def test_early_save_failure_does_not_block_sending(self):
        order = make_order(104)
        database.kv_set(pipeline.LAST_ID_KEY, 100)
        with mock.patch.object(pipeline, "fetch_new_orders", return_value=self.scan([order])), \
                mock.patch.object(pipeline.ai_scorer, "score_order", return_value=ai_result(8)), \
                mock.patch.object(pipeline, "save_order", side_effect=OSError("db")), \
                mock.patch("time.sleep"):
            res = pipeline._prepare_cycle()
        self.assertEqual([s.order.order_id for s in res.to_send], [104])


class QuotaReserveTest(CycleTestBase):
    def orders(self):
        database.kv_set(pipeline.LAST_ID_KEY, 100)
        o = []
        for oid, title, cats in ((201, "Создать телеграм бота", ["PROG_OTHER"]),   # группа 1
                                 (202, "Создать телеграм бота", []),               # группа 2
                                 (203, "Специалист TapLink", [])):                 # группа 3 (бот в тексте)
            order = make_order(oid, title)
            order.categories = cats
            order.body = f"нужен бот {oid}"   # разные ключи дублей
            o.append(order)
        return o

    def run_cycle(self, capacity):
        calls = []

        def score(order):
            calls.append(order.order_id)
            return ai_result(8)

        with mock.patch.object(pipeline, "fetch_new_orders", return_value=self.scan(self.orders())), \
                mock.patch.object(pipeline, "AI_WORKERS", 1), \
                mock.patch.object(pipeline.ai_scorer, "daily_capacity", return_value=capacity), \
                mock.patch.object(pipeline.ai_scorer, "score_order", side_effect=score):
            res = pipeline._prepare_cycle()
        return res, calls

    def test_priority_order_when_quota_ample(self):
        res, calls = self.run_cycle((90, 100))
        self.assertEqual(calls, [201, 202, 203])     # группа 1 первой, хотя id новее у 203
        self.assertEqual(res.reserved, 0)

    def test_low_quota_skips_group_3(self):
        res, calls = self.run_cycle((30, 100))
        self.assertEqual(calls, [201, 202])
        self.assertEqual(res.reserved, 1)
        self.assertEqual([s.order.order_id for s in res.unscored], [203])
        self.assertEqual(res.ai_attempted, 2)       # отложенный - не сбой AI

    def test_very_low_quota_only_group_1(self):
        res, calls = self.run_cycle((20, 100))
        self.assertEqual(calls, [201])
        self.assertEqual(res.reserved, 2)

    def test_border_share_is_reserved(self):
        res, calls = self.run_cycle((50, 100))       # ровно 50% - группа 3 уже ждёт
        self.assertEqual(calls, [201, 202])

    def test_no_capped_providers_no_reserve(self):
        res, calls = self.run_cycle(None)
        self.assertEqual(sorted(calls), [201, 202, 203])


class YandexGrantAlertTest(CycleTestBase):
    def status(self, **budget):
        info = {"until": "2026-11-20", "days_left": 30, "expired": False, "budget": 3300.0,
                "total": 100.0, "today": 5.0, "allowance": 80.0, "exhausted": False, "unknown": False}
        info.update(budget)
        return {"providers": {"yandex": {"budget": info}}}

    def test_soon_alert_sent_once(self):
        with mock.patch.object(pipeline.monitor, "record") as record:
            pipeline._report_yandex_grant(self.status(days_left=3))
            pipeline._report_yandex_grant(self.status(days_left=2))
        self.assertEqual(record.call_count, 1)
        self.assertEqual(record.call_args.args[0], "yandex_grant_soon")
        self.assertIn("20.11.2026", record.call_args.kwargs["reason"])

    def test_expired_and_spent_alerts(self):
        with mock.patch.object(pipeline.monitor, "record") as record:
            pipeline._report_yandex_grant(self.status(expired=True, days_left=0))
            pipeline._report_yandex_grant(self.status(exhausted=True))
        self.assertEqual([c.args[0] for c in record.call_args_list], ["yandex_grant_off", "yandex_grant_spent"])

    def test_quiet_when_far_or_unknown(self):
        with mock.patch.object(pipeline.monitor, "record") as record:
            pipeline._report_yandex_grant(self.status())
            pipeline._report_yandex_grant(self.status(days_left=1, unknown=True))
            pipeline._report_yandex_grant({"providers": {}})
        record.assert_not_called()


if __name__ == "__main__":
    unittest.main()
