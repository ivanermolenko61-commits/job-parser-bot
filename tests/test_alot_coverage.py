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
        scan = ScanResult(orders=[order(1, "youdoru", 20), order(2, "youdoru", 3)], max_id=2, raw_count=2)
        with mock.patch.object(alot_coverage, "find_upper_bound", return_value=100), \
                mock.patch.object(alot_coverage, "fetch_new_orders", return_value=scan) as fetch, \
                mock.patch.object(alot_coverage.time, "sleep"):
            with mock.patch.object(alot_coverage, "datetime") as dt:
                dt.now.return_value = NOW
                orders, top, covered, partial = alot_coverage.collect(12)
        self.assertEqual([o.order_id for o in orders], [2])
        self.assertEqual(top, 100)
        self.assertTrue(covered)
        self.assertFalse(partial)
        self.assertEqual(fetch.call_count, 1)


if __name__ == "__main__":
    unittest.main()
