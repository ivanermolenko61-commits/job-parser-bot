"""Тесты диагностики tools/alot_coverage: окно по времени и строка о свежести выдачи.

Запуск из корня проекта: python -m unittest tests.test_alot_coverage -v
"""
import io
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from unittest import mock

from freelance.alot_client import FreelanceOrder, ScanResult
from tools import alot_coverage

NOW = datetime(2026, 10, 26, 9, 0, tzinfo=timezone.utc)


def order(oid, site, hours_ago):
    return FreelanceOrder(oid, site, f"Заказ {oid}", "", "", 0.0, [],
                          NOW - timedelta(hours=hours_ago), f"https://example.com/{oid}")


class FreshnessTest(unittest.TestCase):
    def run_fresh(self, orders):
        buf = io.StringIO()
        with redirect_stdout(buf):
            alot_coverage.print_freshness(orders, now=NOW)
        return buf.getvalue()

    def test_reports_lag_of_newest_order_and_each_site(self):
        out = self.run_fresh([order(1, "youdoru", 7), order(2, "kworkru", 5), order(3, "youdoru", 9)])
        self.assertIn("старше на 5.0 ч", out)
        self.assertIn("YouDo", out)
        self.assertIn("7.0", out)

    def test_empty_window(self):
        self.assertIn("нет ни одного заказа", self.run_fresh([]))


class CollectTest(unittest.TestCase):
    def test_keeps_only_orders_inside_window_and_stops_when_covered(self):
        old = [order(i, "youdoru", 20) for i in range(1, 26)]
        scan = ScanResult(orders=old + [order(30, "youdoru", 3)], max_id=30, raw_count=26)
        with mock.patch.object(alot_coverage, "find_upper_bound", return_value=100), \
                mock.patch.object(alot_coverage, "fetch_new_orders", return_value=scan) as fetch, \
                mock.patch.object(alot_coverage.time, "sleep"):
            with mock.patch.object(alot_coverage, "datetime") as dt:
                dt.now.return_value = NOW
                orders, top, covered, partial = alot_coverage.collect(12)
        self.assertEqual([o.order_id for o in orders], [30])
        self.assertEqual(top, 100)
        self.assertTrue(covered)
        self.assertFalse(partial)
        self.assertEqual(fetch.call_count, 1)


    def test_accumulates_passes_and_keeps_earlier_orders_on_partial(self):
        first = ScanResult(orders=[order(10, "youdoru", 2), order(11, "youdoru", 1)], max_id=11, raw_count=2)
        second = ScanResult(orders=[order(5, "kworkru", 3)], max_id=5, raw_count=1, partial=True)
        with mock.patch.object(alot_coverage, "find_upper_bound", return_value=100),                 mock.patch.object(alot_coverage, "fetch_new_orders", side_effect=[first, second]),                 mock.patch.object(alot_coverage.time, "sleep"):
            with mock.patch.object(alot_coverage, "datetime") as dt:
                dt.now.return_value = NOW
                orders, _, covered, partial = alot_coverage.collect(12)
        self.assertEqual([o.order_id for o in orders], [5, 10, 11])
        self.assertTrue(partial)
        self.assertFalse(covered)


class WindowCoveredTest(unittest.TestCase):
    def test_one_old_order_in_the_middle_does_not_prove_coverage(self):
        threshold = NOW - timedelta(hours=12)
        # старый заказ с поздним импортом (id 50) не должен закрывать окно: край (id 1..) свежий
        orders = [order(1, "youdoru", 3), order(2, "youdoru", 2), order(50, "youdoru", 20)]
        self.assertFalse(alot_coverage.window_covered(orders, threshold))

    def test_covered_when_lowest_ids_are_older_than_threshold(self):
        threshold = NOW - timedelta(hours=12)
        orders = [order(i, "youdoru", 40 - i) for i in range(1, 25)]  # id 1..24: 39..16 ч назад
        orders += [order(100, "youdoru", 1)]
        self.assertTrue(alot_coverage.window_covered(orders, threshold))

    def test_single_old_order_is_not_enough(self):
        threshold = NOW - timedelta(hours=12)
        self.assertFalse(alot_coverage.window_covered([order(1, "youdoru", 40)], threshold))

    def test_fresh_orders_behind_old_edge_do_not_change_the_edge_rule(self):
        threshold = NOW - timedelta(hours=12)
        old = [order(i, "youdoru", 40) for i in range(1, 21)]
        fresh = [order(i, "youdoru", 1) for i in range(100, 110)]
        self.assertTrue(alot_coverage.window_covered(old + fresh, threshold))
        # но свежий заказ среди наименьших id снимает покрытие
        self.assertFalse(alot_coverage.window_covered(old[:19] + [order(0, "youdoru", 1)] + fresh, threshold))

    def test_empty_is_not_covered(self):
        self.assertFalse(alot_coverage.window_covered([], NOW))


if __name__ == "__main__":
    unittest.main()
