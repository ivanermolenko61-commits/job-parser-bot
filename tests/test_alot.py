"""Скан alot.pro: сбой пачки не выбрасывает уже собранные заказы. Без сети."""
import unittest
from unittest import mock

from freelance import alot_client


def item(order_id: int) -> dict:
    return {"internalId": order_id, "date": 1791500000000, "site": "youdoru",
            "title": f"заказ {order_id}", "body": "", "url": f"https://x/{order_id}"}


def fake_fetch(fail_from: int | None = None, top: int = 10_000_000):
    """Пачка: существующие id <= top; с fail_from - responseCode=8."""
    def fetch(ids):
        if fail_from is not None and ids[0] >= fail_from:
            raise RuntimeError("alot.pro responseCode=8")
        return [item(i) for i in ids if i <= top and i % 2 == 0]
    return fetch


class FetchNewOrdersTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(alot_client.time, "sleep")
        patcher.start()
        self.addCleanup(patcher.stop)

    def scan(self, fetch, last_id=1000):
        with mock.patch.object(alot_client, "_fetch_items", side_effect=fetch):
            return alot_client.fetch_new_orders(last_id, max_ahead=300, look_back=100)

    def test_full_scan_stops_at_boundary(self):
        r = self.scan(fake_fetch(top=1150))
        self.assertFalse(r.partial)
        self.assertEqual(r.max_id, 1150)

    def test_failure_keeps_collected(self):
        r = self.scan(fake_fetch(fail_from=1100))
        self.assertTrue(r.partial)
        self.assertEqual(r.max_id, 1098)          # 900..999 и 1000..1099, без пачки со сбоем
        self.assertEqual(len(r.orders), 100)

    def test_failure_before_anything_raises(self):
        with self.assertRaises(RuntimeError):
            self.scan(fake_fetch(fail_from=0))


class PartialStreakTest(unittest.TestCase):
    """Скан, раз за разом оборванный без продвижения курсора, даёт ошибку в health."""

    def setUp(self):
        import os
        import tempfile

        import database
        from freelance import pipeline
        self.pipeline, self.database = pipeline, database
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = database.DB_PATH
        database.DB_PATH = os.path.join(self.tmp.name, "t.db")
        database.init_db()
        database.kv_set(pipeline.LAST_ID_KEY, 1000)

    def tearDown(self):
        self.database.DB_PATH = self.old_path
        self.tmp.cleanup()

    def cycle(self, scan):
        p = self.pipeline
        with mock.patch.object(p, "fetch_new_orders", return_value=scan), \
                mock.patch.object(p, "_load_pending", return_value={}), \
                mock.patch.object(p.monitor, "record") as rec:
            p._prepare_cycle()
        return [c for c in rec.call_args_list if c.args and c.args[0] == "alot"][-1]

    def stuck_scan(self):
        return alot_client.ScanResult(orders=[], max_id=990, raw_count=5, partial=True)

    def test_alert_after_repeated_partial(self):
        for _ in range(self.pipeline.PARTIAL_ALERT_CYCLES - 1):
            args = self.cycle(self.stuck_scan())
            self.assertEqual(len(args.args), 2)            # без ошибки
        args = self.cycle(self.stuck_scan())
        self.assertIsInstance(args.args[2], RuntimeError)  # тревога
        self.assertEqual(self.database.kv_get("freelance_stale"), "1")

    def test_progress_resets_streak(self):
        self.cycle(self.stuck_scan())
        self.cycle(alot_client.ScanResult(orders=[], max_id=1050, raw_count=5, partial=True))
        self.assertEqual(self.database.kv_get("freelance_partial"), "0")


class PriceVersionTest(unittest.TestCase):
    def test_old_price_recalculated(self):
        from freelance import pipeline, pricing
        from freelance.pipeline import ScoredOrder
        order = alot_client.FreelanceOrder(1, "youdoru", "t", "", "", 3000.0, [], None, "u")
        ai = {"hours": 10, "category": "other", "clarity": "ясно", "unknowns": [],
              "price_mid": 3300, "price_low": 2800,        # посчитано старой формулой
              "market_low": 1000, "market_high": 2000}
        sc = ScoredOrder(order, ai)
        with mock.patch.object(pricing, "price_order", wraps=lambda a, b: pricing.calc_price(a, b, rate=1000)):
            pipeline._ensure_price(sc)
        self.assertEqual(sc.ai["price_v"], pricing.PRICE_VERSION)
        self.assertGreaterEqual(sc.ai["price_low"], 7750)
        self.assertNotIn("market_low", sc.ai)             # устаревший рынок удалён
        self.assertEqual(sc.ai["category"], "other")      # оценка модели не тронута


if __name__ == "__main__":
    unittest.main()
