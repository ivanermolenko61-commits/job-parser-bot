"""RSS FL.ru: разбор ленты и работа цикла с двумя источниками. Без сети.

Запуск из корня проекта: python -m unittest tests.test_fl_client -v
"""
import unittest
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from unittest import mock

from freelance import alot_client, fl_client, pipeline, sources
from freelance.filters import is_candidate
from tests.test_pipeline_cycle import CycleTestBase, ai_result, make_order


def rss(*items: str) -> bytes:
    body = "".join(items)
    return (f'<?xml version="1.0" encoding="utf-8"?><rss version="2.0"><channel>'
            f"<title>FL</title>{body}</channel></rss>").encode("utf-8")


def item(oid=5526858, title="Нужен Telegram бот", desc="Сделать бота на Python",
         category="Программирование / Создание скриптов", when=None) -> str:
    when = when or datetime.now(timezone.utc)
    return (f"<item><title>{title}</title>"
            f"<link>https://www.fl.ru/projects/{oid}/x.html</link>"
            f"<description>{desc}</description><category>{category}</category>"
            f"<pubDate>{format_datetime(when)}</pubDate></item>")


class ParseRssTest(unittest.TestCase):
    def test_item_fields(self):
        when = datetime(2026, 10, 26, 18, 50, 35, tzinfo=timezone.utc)
        (o,) = fl_client.parse_rss(rss(item(when=when)))
        self.assertEqual(o.order_id, fl_client.FL_ID_OFFSET + 5526858)
        self.assertTrue(fl_client.is_fl_id(o.order_id))
        self.assertEqual(o.site, "flru")
        self.assertEqual(o.published_at, when)
        self.assertEqual(o.price_text, "")
        self.assertEqual(o.url, "https://www.fl.ru/projects/5526858/x.html")
        self.assertIn("PROG_OTHER", o.categories)

    def test_budget_moved_from_title_to_price(self):
        (o,) = fl_client.parse_rss(rss(item(title="Нужен бот (Бюджет: 3 000  &amp;#8381;)")))
        self.assertEqual(o.title, "Нужен бот")
        self.assertEqual(o.price_text, "3 000 ₽")
        self.assertEqual(o.price_value, 3000.0)

    def test_budget_negotiable_has_no_value(self):
        (o,) = fl_client.parse_rss(rss(item(title="Сайт (Бюджет: По договоренности)")))
        self.assertEqual((o.title, o.price_text, o.price_value), ("Сайт", "По договоренности", 0.0))

    def test_no_budget_in_title(self):
        (o,) = fl_client.parse_rss(rss(item(title="Скрипт &amp; бот")))
        self.assertEqual((o.title, o.price_text), ("Скрипт & бот", ""))

    def test_id_does_not_clash_with_alot(self):
        self.assertFalse(fl_client.is_fl_id(17360853))

    def test_category_codes(self):
        self.assertEqual(fl_client._dev_code("Программирование / Парсинг данных"), "PROG_OTHER")
        self.assertEqual(fl_client._dev_code("AI — искусственный интеллект / AI-агенты"), "PROG_OTHER")
        self.assertEqual(fl_client._dev_code("Сайты / WordPress"), "WEB")
        self.assertIsNone(fl_client._dev_code("Сайты / Дизайн сайтов"))
        self.assertIsNone(fl_client._dev_code("Сайты / 1С Битрикс"))
        self.assertIsNone(fl_client._dev_code("Дизайн / Figma"))

    def test_dev_category_passes_prefilter_without_keyword(self):
        (o,) = fl_client.parse_rss(rss(item(title="Задача по данным", desc="нужно собрать",
                                            category="Программирование / Парсинг данных")))
        self.assertTrue(is_candidate(o))
        (d,) = fl_client.parse_rss(rss(item(title="Задача по данным", desc="нужно собрать",
                                            category="Дизайн / Figma")))
        self.assertFalse(is_candidate(d))

    def test_bad_item_skipped_good_kept(self):
        bad = "<item><title>x</title><link>https://www.fl.ru/nope</link></item>"
        orders = fl_client.parse_rss(rss(bad, item(oid=5526859)))
        self.assertEqual([o.order_id for o in orders], [fl_client.FL_ID_OFFSET + 5526859])

    def test_empty_channel_is_ok_but_broken_xml_raises(self):
        self.assertEqual(fl_client.parse_rss(rss()), [])
        with self.assertRaises(RuntimeError):
            fl_client.parse_rss(b"<html>blocked</html>")
        with self.assertRaises(RuntimeError):
            fl_client.parse_rss(b"<rss><channel>")


class TwoSourcesCycleTest(CycleTestBase):
    def fl_order(self, oid, title="Парсер сайта на Python", minutes_ago=10):
        when = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
        return fl_client.parse_rss(rss(item(oid=oid, title=title, when=when)))[0]

    def run_cycle(self, alot_orders, fl_orders, alot_error=None, stored="1000"):
        database_kv = mock.patch.object(pipeline, "kv_get", side_effect=lambda k, d=None: {
            pipeline.LAST_ID_KEY: stored}.get(k, d))
        fetch = (mock.patch.object(pipeline, "fetch_new_orders", side_effect=alot_error)
                 if alot_error else
                 mock.patch.object(pipeline, "fetch_new_orders", return_value=self.scan(alot_orders)))
        with database_kv, fetch, mock.patch.object(pipeline, "kv_set"), \
                mock.patch.object(pipeline, "_fetch_feed_orders", return_value=fl_orders), \
                mock.patch.object(pipeline, "_load_pending", return_value={}) as pend, \
                mock.patch.object(pipeline.ai_scorer, "score_order", side_effect=lambda o, **k: ai_result(8),
                                  create=True):
            res = pipeline._prepare_cycle()
        return res, pend

    def test_fl_orders_join_candidates_and_do_not_move_cursor(self):
        fl = self.fl_order(5000001)
        res, pend = self.run_cycle([make_order(1100)], [fl])
        ids = {sc.order.order_id for sc in res.to_send + res.unscored + res.rejected}
        self.assertIn(fl.order_id, ids)
        self.assertEqual(res.new_last_id, 1100)  # курсор alot: только id alot
        self.assertEqual(pend.call_args.args[2], [fl])

    def test_alot_down_fl_still_works(self):
        fl = self.fl_order(5000002)
        res, _ = self.run_cycle([], [fl], alot_error=RuntimeError("alot down"))
        self.assertEqual(res.error, "")
        self.assertEqual(res.new_last_id, 0)  # курсор alot не трогаем
        ids = {sc.order.order_id for sc in res.to_send + res.unscored + res.rejected}
        self.assertIn(fl.order_id, ids)

    def test_both_down_is_error(self):
        # alot лежит, а прямых источников включённых нет - брать заказы неоткуда
        sources.set_enabled(sources.FLRU, False)
        sources.set_enabled(sources.KWORK, False)
        res, _ = self.run_cycle([], [], alot_error=RuntimeError("alot down"))
        self.assertIn("alot down", res.error)

    def test_fl_down_alot_still_works(self):
        with mock.patch.object(pipeline, "fetch_fl_orders", side_effect=RuntimeError("fl down")):
            self.assertEqual(pipeline._fetch_fl_orders(), [])
        pipeline.monitor.record.assert_called_with(
            "flru", 0, mock.ANY, label="Источник фриланс-заказов FL.ru")
        res, _ = self.run_cycle([make_order(1100)], [])
        self.assertEqual(res.error, "")
        self.assertEqual(res.new_last_id, 1100)

    def test_fl_disabled_does_not_call_network(self):
        sources.set_enabled(sources.FLRU, False)
        with mock.patch.object(pipeline, "fetch_fl_orders") as net:
            self.assertEqual(pipeline._fetch_fl_orders(), [])
        net.assert_not_called()


class PendingFlTest(CycleTestBase):
    def test_fl_pending_taken_from_rss_not_from_alot(self):
        oid = fl_client.FL_ID_OFFSET + 777
        now = datetime.now(timezone.utc)
        row = {"order_id": oid, "published_at": now.isoformat(), "ai_json": None,
               "dedup_key": "k", "attempts": 1}
        order = fl_client.parse_rss(rss(item(oid=777, when=now)))[0]
        with mock.patch.object(pipeline, "freelance_pending", return_value=[row]), \
                mock.patch.object(pipeline, "_fetch_items", return_value=[]) as alot:
            res = pipeline._load_pending(now - timedelta(hours=6), set(), [order])
        alot.assert_not_called()
        self.assertEqual(list(res), [oid])
        self.assertEqual(res[oid].attempts, 1)

    def pending_order(self, pid, desc="полное описание задачи", hours_ago=1, attempts=2):
        """FL-заказ, сохранённый как pending обычным путём (save_order): в БД и в хранилище описаний."""
        when = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
        order = fl_client.parse_rss(rss(item(oid=pid, desc=desc, when=when)))[0]
        pipeline.save_order(pipeline.ScoredOrder(order, None, "dk", attempts), "pending")
        return order

    def floor(self):
        return datetime.now(timezone.utc) - timedelta(hours=6)

    def test_fell_out_of_feed_restored_with_description_after_restart(self):
        # описание сохранено при записи pending; памяти процесса нет (перезапуск), в ленте заказа нет
        order = self.pending_order(778)
        res = pipeline._load_pending(self.floor(), set(), [])
        self.assertEqual(list(res), [order.order_id])
        sc = res[order.order_id]
        self.assertEqual(sc.order.body, "полное описание задачи")     # исходное описание, не вслепую
        self.assertEqual((sc.order.title, sc.order.site, sc.order.url),
                         (order.title, "flru", order.url))
        self.assertEqual((sc.attempts, sc.dedup), (2, "dk"))
        self.assertIsNone(sc.ai)

    def test_restored_when_feed_failed_this_cycle(self):
        order = self.pending_order(779)
        res = pipeline._load_pending(self.floor(), set(), None)
        self.assertEqual(list(res), [order.order_id])

    def test_current_feed_wins_over_stored(self):
        order = self.pending_order(782, desc="старое описание")
        fresh = fl_client.parse_rss(rss(item(oid=782, desc="новое описание", when=order.published_at)))[0]
        res = pipeline._load_pending(self.floor(), set(), [fresh])
        self.assertEqual(res[order.order_id].order.body, "новое описание")

    def test_pending_without_stored_description_is_not_scored_blind(self):
        # запись в БД есть, описания нет нигде: заказ ждёт, а не оценивается по названию
        import database
        oid = fl_client.FL_ID_OFFSET + 781
        database.freelance_save(oid, "flru", "Парсер", "https://x", "", datetime.now(timezone.utc).isoformat(),
                                "dk", None, "", "pending")
        self.assertEqual(pipeline._load_pending(self.floor(), set(), []), {})
        self.assertEqual(database.freelance_get(oid)["status"], "pending")

    def test_expired_is_marked_expired(self):
        import database
        order = self.pending_order(780, hours_ago=7)
        self.assertEqual(pipeline._load_pending(self.floor(), set(), []), {})
        self.assertEqual(database.freelance_get(order.order_id)["status"], "expired")

    def test_store_forgets_order_when_status_changes_and_prunes_old(self):
        import database
        keep = self.pending_order(1)
        gone = self.pending_order(2)
        pipeline.save_order(pipeline.ScoredOrder(gone, None, "dk"), "sent")
        self.assertEqual(list(pipeline._feed_store_read()), [str(keep.order_id)])
        old = self.pending_order(3, hours_ago=9)      # старше срока: не сохраняется в хранилище
        self.assertNotIn(str(old.order_id), pipeline._feed_store_read())

    def test_store_ignores_alot_orders(self):
        pipeline.save_order(pipeline.ScoredOrder(make_order(1234), None, "dk"), "pending")
        self.assertEqual(pipeline._feed_store_read(), {})

    def test_store_survives_garbage_and_save_failure(self):
        import database
        database.kv_set(pipeline.FEED_STORE_KEY, "не json")
        self.assertEqual(pipeline._feed_store_read(), {})
        database.kv_set(pipeline.FEED_STORE_KEY, "[1, 2]")
        self.assertEqual(pipeline._feed_store_read(), {})
        order = self.pending_order(5)          # перезаписывает мусор нормальным значением
        self.assertIn(str(order.order_id), pipeline._feed_store_read())
        with mock.patch.object(pipeline, "kv_set", side_effect=OSError("db")):
            pipeline.save_order(pipeline.ScoredOrder(order, None, "dk"), "pending")   # не падает

    def test_store_keeps_exactly_what_ai_sees_and_no_contacts(self):
        from freelance import ai_scorer
        desc = ("Звоните +7 (999) 123-45-67, пишите ivan@mail.ru. Нужен парсер сайта. " * 80
                + "ГЛАВНОЕ ТРЕБОВАНИЕ В КОНЦЕ")
        order = self.pending_order(6, desc=desc)
        stored = pipeline._feed_store_read()[str(order.order_id)]["body"]
        seen_by_ai = ai_scorer.mask_contacts(order.body)[:ai_scorer.BODY_LIMIT]
        self.assertEqual(stored, seen_by_ai)
        self.assertNotIn("123-45-67", stored)
        self.assertNotIn("ivan@mail.ru", stored)
        # после восстановления AI получает тот же текст, что и без перезапуска
        restored = pipeline._load_pending(self.floor(), set(), [])[order.order_id].order
        self.assertEqual(ai_scorer.mask_contacts(restored.body)[:ai_scorer.BODY_LIMIT], seen_by_ai)

    def test_store_follows_configured_body_limit(self):
        from freelance import ai_scorer
        with mock.patch.object(ai_scorer, "BODY_LIMIT", 3000):
            order = self.pending_order(7, desc="я" * 5000)
        stored = pipeline._feed_store_read()[str(order.order_id)]["body"]
        self.assertEqual(len(stored), 3000)


if __name__ == "__main__":
    unittest.main()
