"""Kwork: разбор страницы проектов и сбор нескольких страниц. Без сети.

Запуск из корня проекта: python -m unittest tests.test_kwork_client -v
"""
import json
import unittest
from datetime import datetime, timezone
from unittest import mock

import requests

from freelance import kwork_client
from freelance.filters import is_candidate
from freelance.sources import KWORK_ID_OFFSET


def want(pid=3269643, name="Нужен Telegram бот", desc="Сделать бота на Python",
         category="41", price="3000.00", created="2026-10-26 22:03:14", status="active"):
    return {"id": pid, "name": name, "description": desc, "category_id": category,
            "priceLimit": price, "date_create": created, "status": status}


def page(*wants, mark=True) -> str:
    state = {"wantsListData": {"wants": list(wants)}}
    body = ("window.stateData=" if mark else "window.other=") + json.dumps(state, ensure_ascii=False)
    return f"<html><script>{body};window.x=1;</script></html>"


class ParsePageTest(unittest.TestCase):
    def test_fields(self):
        (o,) = kwork_client.parse_page(page(want()))
        self.assertEqual(o.order_id, KWORK_ID_OFFSET + 3269643)
        self.assertTrue(kwork_client.is_kwork_id(o.order_id))
        self.assertEqual((o.site, o.title), ("kworkru", "Нужен Telegram бот"))
        self.assertEqual((o.price_text, o.price_value), ("до 3 000 ₽", 3000.0))
        self.assertEqual(o.url, "https://kwork.ru/projects/3269643/view")

    def test_moscow_time_converted_to_utc(self):
        (o,) = kwork_client.parse_page(page(want(created="2026-10-26 22:03:14")))
        self.assertEqual(o.published_at, datetime(2026, 10, 26, 19, 3, 14, tzinfo=timezone.utc))

    def test_entities_unescaped_in_title_and_body(self):
        (o,) = kwork_client.parse_page(page(want(name="Парсер &amp; бот", desc="Цель &mdash; сайт")))
        self.assertEqual(o.title, "Парсер & бот")
        self.assertIn("—", o.body)

    def test_inactive_and_broken_skipped(self):
        got = kwork_client.parse_page(page(want(pid=1, status="archive"), want(pid=2, created="вчера"),
                                           want(pid=3)))
        self.assertEqual([o.order_id for o in got], [KWORK_ID_OFFSET + 3])

    def test_no_price(self):
        (o,) = kwork_client.parse_page(page(want(price="0.00")))
        self.assertEqual((o.price_text, o.price_value), ("", 0.0))

    def test_category_codes(self):
        self.assertEqual(kwork_client._dev_codes("37"), ["WEB"])
        self.assertEqual(kwork_client._dev_codes("79"), ["WEB"])
        self.assertEqual(kwork_client._dev_codes("255"), ["PROG_OTHER"])
        self.assertEqual(kwork_client._dev_codes("41"), [])   # скрипты/боты: решают слова
        self.assertEqual(kwork_client._dev_codes("40"), [])

    def test_ai_media_in_scripts_category_not_auto_dev(self):
        (o,) = kwork_client.parse_page(page(want(name="ИИ-видео про исторические места",
                                                 desc="нужны ролики", category="41")))
        self.assertFalse(is_candidate(o))
        (s,) = kwork_client.parse_page(page(want(name="Доработка сайта", desc="нужна правка",
                                                 category="38")))
        self.assertTrue(is_candidate(s))

    def test_no_state_is_error(self):
        with self.assertRaises(RuntimeError):
            kwork_client.parse_page("<html>captcha</html>")

    def test_broken_state_is_error(self):
        with self.assertRaises(RuntimeError):
            kwork_client.parse_page("<script>window.stateData={broken</script>")
        with self.assertRaises(RuntimeError):
            kwork_client.parse_page("<script>window.stateData={\"a\":1};</script>")

    def test_empty_list_is_ok(self):
        self.assertEqual(kwork_client.parse_page(page()), [])


class FetchTest(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(kwork_client.time, "sleep")
        p.start()
        self.addCleanup(p.stop)

    def orders(self, *pids):
        found = kwork_client.parse_page(page(*[want(pid=i) for i in pids]))
        return found, len(pids)

    def test_collects_pages_and_dedups(self):
        pages = [self.orders(1, 2), self.orders(2, 3)]
        with mock.patch.object(kwork_client, "_fetch_page", side_effect=pages):
            got = kwork_client.fetch_kwork_orders(pages=2)
        self.assertEqual(sorted(o.order_id - KWORK_ID_OFFSET for o in got), [1, 2, 3])

    def test_first_page_failure_raises(self):
        with mock.patch.object(kwork_client, "_fetch_page", side_effect=requests.ConnectionError("x")):
            with self.assertRaises(requests.ConnectionError):
                kwork_client.fetch_kwork_orders(pages=3)

    def test_later_page_failure_keeps_collected(self):
        with mock.patch.object(kwork_client, "_fetch_page",
                               side_effect=[self.orders(1), RuntimeError("captcha")]):
            got = kwork_client.fetch_kwork_orders(pages=3)
        self.assertEqual([o.order_id - KWORK_ID_OFFSET for o in got], [1])

    def test_page_of_filtered_projects_does_not_stop_paging(self):
        # на странице 2 есть проекты, но все неактивные: обход идёт дальше, на странице 3 есть заказ
        archived = kwork_client.parse_page_counted(page(want(pid=7, status="archive"), want(pid=8, status="archive")))
        self.assertEqual((archived[0], archived[1]), ([], 2))
        with mock.patch.object(kwork_client, "_fetch_page",
                               side_effect=[self.orders(1), archived, self.orders(9)]) as f:
            got = kwork_client.fetch_kwork_orders(pages=3)
        self.assertEqual(f.call_count, 3)
        self.assertEqual(sorted(o.order_id - KWORK_ID_OFFSET for o in got), [1, 9])

    def test_empty_later_page_stops(self):
        with mock.patch.object(kwork_client, "_fetch_page", side_effect=[self.orders(1), ([], 0)]) as f:
            kwork_client.fetch_kwork_orders(pages=4)
        self.assertEqual(f.call_count, 2)


if __name__ == "__main__":
    unittest.main()
