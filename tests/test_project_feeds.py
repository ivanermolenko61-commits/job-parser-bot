import json
import unittest
from datetime import timedelta

from freelance import manual, project_feeds as feeds, sources
from freelance.filters import reject_reason
from freelance.language import UKRAINIAN_REASON


class ProjectFeedsTests(unittest.TestCase):
    def test_kadrof_canonical_id_language_and_timezone(self):
        item = """<div class="post project-list"><div class="date">24.11.2026 в 15:32</div>
            <div class="project-title"><h6><a href="/work/123">Потрібно створити бота</a></h6>
            <div class="budget">4000 ₽</div></div><div class="clear"></div>
            Потрібно налаштувати облік замовлень та зберігати відповіді покупців.
            <div class="project-info">Просмотры: 8</div></div>"""
        rows = feeds.parse_kadrof('<div class="post-list">' + item * 2 +
                                  item.replace('/work/123', 'https://example.org/work/123') + '</div>')
        order, = rows
        self.assertEqual(order.url, "https://www.kadrof.ru/work/123")
        self.assertEqual(sources.source_of(order.order_id), sources.KADROF)
        self.assertEqual(order.site, "kadrofru")
        self.assertEqual(manual.listing(order.url)[3], order.order_id)
        self.assertEqual(order.price_text, "4000 ₽")
        self.assertEqual(order.published_at.hour, 12)
        self.assertNotIn("Просмотры", order.body)
        self.assertEqual(reject_reason(order), UKRAINIAN_REASON)
        order.title = "Разработать Telegram-бота на Python"
        order.body = "Настроить выгрузку заказов в Google Таблицы"
        self.assertIsNone(reject_reason(order))
        order.body = "Вакансия в штат на полный рабочий день"
        self.assertEqual(reject_reason(order), "excluded_site")

    def test_contractis_uses_open_linked_orders_with_publication_dates(self):
        values = []
        links = []
        for ident, status, stamp in [(5, "Open", "2026-11-23 23:35:36"), (6, "Closed", None),
                                     (7, "Open", None), (8, "Open", "2020-01-01 01:01:01")]:
            fields = dict(id=ident, status=status, publishedDate=stamp, title="Парсер на Python",
                          description="Автоматизация отчётов", slug=f"test-{ident}", startPrice=2000,
                          endPrice=3000, moderStatus="Approved", paymentType="fixed")
            row = {}
            for key, value in fields.items():
                row[key] = len(values)
                values.append(value)
            values.append(row)
            if ident != 8:
                links.append(f'<a href="/order/test-{ident}"></a>')
        page = "".join(links) + '<script id="__NUXT_DATA__">' + json.dumps(values) + '</script>'
        order, = feeds.parse_contractis(page)
        self.assertEqual(order.order_id, sources.CONTRACTIS_ID_OFFSET + 5)
        self.assertEqual(sources.source_of(order.order_id), sources.CONTRACTIS)
        self.assertEqual(order.price_text, "2000 – 3000 ₽")
        self.assertEqual(order.published_at.hour, 23)
        self.assertEqual(order.published_at.utcoffset(), timedelta(0))

    def test_changed_format_reports_failure(self):
        for parse in (feeds.parse_contractis, feeds.parse_kadrof):
            with self.subTest(parse=parse.__name__), self.assertRaises(ValueError):
                parse("<html></html>")
