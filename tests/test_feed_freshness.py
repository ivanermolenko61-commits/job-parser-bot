"""Тесты оповещения «выдача alot.pro отстаёт»: самый новый заказ в выдаче старше порога.

Запуск из корня проекта: python -m unittest tests.test_feed_freshness -v
"""
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from freelance import alot_client, pipeline
from health import HealthMonitor
from tests.test_pipeline_cycle import CycleTestBase

NOW = datetime(2026, 10, 26, 9, 0, tzinfo=timezone.utc)   # 12:00 МСК


def order(oid, hours_ago, site="youdoru"):
    return alot_client.FreelanceOrder(oid, site, f"Заказ {oid}", "", "", 0.0, [],
                                      NOW - timedelta(hours=hours_ago), f"https://x/{oid}")


def scan(*orders):
    return alot_client.ScanResult(orders=list(orders), max_id=max([o.order_id for o in orders] or [0]),
                                  raw_count=len(orders))


class FeedLagTest(unittest.TestCase):
    def test_empty_window_has_no_verdict(self):
        self.assertIsNone(pipeline.feed_lag([], NOW))

    def test_newest_overall_and_newest_youdo(self):
        newest, hours, youdo = pipeline.feed_lag([order(1, 7), order(2, 5, "kworkru"), order(3, 9)], NOW)
        self.assertEqual(newest, NOW - timedelta(hours=5))
        self.assertAlmostEqual(hours, 5.0)
        self.assertEqual(youdo, NOW - timedelta(hours=7))

    def test_no_youdo_in_window(self):
        self.assertIsNone(pipeline.feed_lag([order(1, 1, "kworkru")], NOW)[2])


class ReportFreshnessTest(unittest.TestCase):
    def setUp(self):
        self.mon = HealthMonitor()
        p = mock.patch.object(pipeline, "monitor", self.mon)
        p.start()
        self.addCleanup(p.stop)

    def cycles(self, n, sc, threshold=3.0):
        for _ in range(n):
            pipeline._report_feed_freshness(sc, now=NOW, threshold=threshold)
        return self.mon.pop_alerts()

    def test_fresh_feed_never_alerts(self):
        self.assertEqual(self.cycles(6, scan(order(1, 0.5))), [])

    def test_stale_feed_alerts_after_three_cycles_once(self):
        stale = scan(order(1, 7), order(2, 5, "kworkru"))
        self.assertEqual(self.cycles(2, stale), [])           # одиночный сбой не шумит
        alerts = self.cycles(1, stale)                        # третья проверка подряд
        self.assertEqual(len(alerts), 1)
        text = alerts[0]
        self.assertIn("Свежесть выдачи alot.pro: выдача отстаёт", text)
        self.assertIn("07:00 МСК", text)                      # 09:00 UTC - 5 ч = 04:00 UTC = 07:00 МСК
        self.assertIn("5.0 ч назад", text)
        self.assertIn("порог 3 ч", text)
        self.assertIn("заказ Youdo в выдаче: 26.10 05:00 МСК", text)
        self.assertIn("проверьте Youdo вручную", text)
        self.assertEqual(self.cycles(5, stale), [])           # дальше повторов нет, пока не восстановится

    def test_recovery_message_when_feed_catches_up(self):
        stale, fresh = scan(order(1, 7)), scan(order(2, 0.2))
        self.cycles(3, stale)
        recovered = self.cycles(1, fresh)
        self.assertEqual(len(recovered), 1)
        self.assertIn("снова свежая", recovered[0])
        self.assertEqual(self.cycles(3, fresh), [])

    def test_threshold_is_strict_and_configurable(self):
        self.assertEqual(self.cycles(4, scan(order(1, 3.0)), threshold=3.0), [])     # ровно порог - не отставание
        self.assertEqual(len(self.cycles(3, scan(order(2, 3.1)), threshold=3.0)), 1)
        self.setUp()
        self.assertEqual(len(self.cycles(3, scan(order(3, 2.0)), threshold=1.0)), 1)

    def test_no_youdo_in_window_is_said_explicitly(self):
        alerts = self.cycles(3, scan(order(1, 6, "kworkru")))
        self.assertIn("заказов Youdo в просмотренном окне нет", alerts[0])

    def test_empty_window_does_not_touch_the_series(self):
        stale = scan(order(1, 7))
        self.cycles(2, stale)
        self.assertEqual(self.cycles(5, scan()), [])                 # пустое окно - не доказательство
        self.assertEqual(len(self.cycles(1, stale)), 1)              # серия сохранилась: третья плохая проверка


class CycleIntegrationTest(CycleTestBase):
    """Оповещение встроено в цикл: в health.monitor идёт источник alot_feed, а сбой проверки цикл не ломает."""

    def feed_calls(self):
        return [c for c in pipeline.monitor.record.call_args_list if c.args and c.args[0] == "alot_feed"]

    def test_cycle_reports_stale_feed(self):
        old = alot_client.FreelanceOrder(5, "kworkru", "Сайт", "", "", 0.0, [],
                                         datetime.now(timezone.utc) - timedelta(hours=8), "https://x/5")
        with mock.patch.object(pipeline, "fetch_new_orders", return_value=self.scan([old])), \
                mock.patch.object(pipeline, "_load_pending", return_value={}):
            pipeline.kv_set(pipeline.LAST_ID_KEY, 4)
            pipeline._prepare_cycle()
        calls = self.feed_calls()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].args[1], 0)                          # плохая проверка
        self.assertIn("выдача отстаёт", calls[0].kwargs["bad_text"])

    def test_cycle_reports_fresh_feed_as_good(self):
        fresh = alot_client.FreelanceOrder(6, "kworkru", "Сайт", "", "", 0.0, [],
                                           datetime.now(timezone.utc), "https://x/6")
        with mock.patch.object(pipeline, "fetch_new_orders", return_value=self.scan([fresh])), \
                mock.patch.object(pipeline, "_load_pending", return_value={}):
            pipeline.kv_set(pipeline.LAST_ID_KEY, 5)
            pipeline._prepare_cycle()
        self.assertEqual([c.args[1] for c in self.feed_calls()], [1])

    def test_freshness_failure_does_not_break_the_cycle(self):
        with mock.patch.object(pipeline, "fetch_new_orders", return_value=self.scan([])), \
                mock.patch.object(pipeline, "_load_pending", return_value={}), \
                mock.patch.object(pipeline, "_report_feed_freshness", side_effect=RuntimeError("boom")):
            pipeline.kv_set(pipeline.LAST_ID_KEY, 5)
            res = pipeline._prepare_cycle()
        self.assertFalse(res.error)


if __name__ == "__main__":
    unittest.main()
