import unittest
from datetime import datetime, timezone
from unittest import mock

import database
from freelance import manual, pipeline, public_boards as boards, sources
from freelance.filters import reject_reason
from tests.test_pipeline_cycle import CycleTestBase


class BoardsTest(unittest.TestCase):
    def test_freelance_exact_time_uses_clock_not_customer_activity(self):
        page = '''
        <article class="task-card">
          <a class="task-card__title-link" href="/task/view/55?from=feed">Чат-бот Telegram</a>
          <div class="task-card__desc">Интеграция с API и Google Таблицами</div>
          <div class="task-card__budget"><span class="bold">5000 ₽</span></div>
          <div class="task-card__foot-item" title="23.11.2026 21:40"><i class="fa fa-clock-o"></i></div>
          <time datetime="2026-11-23T20:00:00Z"></time>
        </article>'''
        order, = boards.parse_freelance_page(page)
        self.assertEqual(order.order_id, sources.FREELANCE_RU_ID_OFFSET + 55)
        self.assertEqual(order.published_at, datetime(2026, 11, 23, 18, 40, tzinfo=timezone.utc))
        self.assertEqual(order.price_text, "5000 ₽")
        self.assertEqual(manual.listing(order.url)[3], order.order_id)
        self.assertIsNone(reject_reason(order))

    def test_freelance_reads_description_up_to_meta(self):
        html = ('<div class="tv-hero"><h1>Задание</h1>Начало<p>Сделать отчёт</p>'
                '<div class="tv-hero__meta">Был сегодня</div><p>Профиль</p></div>')
        self.assertEqual(boards._freelance_description(html), "Начало Сделать отчёт")

    def test_weblancer_skips_old_days_and_closed_keeps_today_time_unknown(self):
        def card(oid, stamp, status="open"):
            return f'''<article><a href="/freelance/programming/bot-{oid}/">Парсер на Python {oid}</a>
            <p>Интеграция с API и выгрузка результатов в Google Таблицы</p>
            <span data-status="{status}"></span><span itemprop="datePublished">{stamp}</span></article>'''

        now = datetime(2026, 11, 23, 19, 0, tzinfo=timezone.utc)
        page = card(1, "22.11.2026") + card(2, "23.11.2026") + card(3, "23.11.2026 21:59")
        page += card(4, "23.11.2026", "closed")
        orders = boards.parse_weblancer_page(page, now)
        self.assertEqual([o.order_id - sources.WEBLANCER_ID_OFFSET for o in orders], [2, 3])
        self.assertIsNone(orders[0].published_at)
        self.assertEqual(orders[1].published_at, datetime(2026, 11, 23, 18, 59, tzinfo=timezone.utc))

    def test_weblancer_ignores_dates_and_time_tags_from_the_description(self):
        page = '''<article><a href="/freelance/programming/bot-20/">Интеграция API</a>
        <p>Сделать выгрузку за 20.11.2026 10:00 до <time datetime="2026-12-10T18:00:00Z">
        10.12.2026 21:00</time></p></article>'''
        order, = boards.parse_weblancer_page(page, datetime(2026, 11, 23, tzinfo=timezone.utc))
        self.assertIsNone(order.published_at)

    def test_excess_freelance_previews_still_reach_intake(self):
        page = "".join(f'''<article class="task-card">
            <a class="task-card__title-link" href="/task/view/{i}">Чат-бот Telegram {i}</a>
            <div class="task-card__desc">Интеграция с API и Google Таблицами...</div></article>'''
                       for i in range(1, 7))
        with mock.patch.object(boards, "_get", side_effect=[page] + [""] * boards.MAX_DETAIL) as fetch:
            with mock.patch.object(boards, "freelance_known", return_value=set()):
                self.assertEqual(len(boards.fetch_freelance_ru()), 6)
        self.assertEqual(fetch.call_count, 1 + boards.MAX_DETAIL)

    def test_challenge_is_not_empty_success(self):
        for parse in (boards.parse_weblancer_page, boards.parse_freelance_page):
            with self.assertRaises(ValueError):
                parse("<title>Just a moment...</title>")


class BoardsIntakeTest(CycleTestBase):
    def test_direct_collectors_persist_both_boards(self):
        freelance = '''<article class="task-card">
            <a class="task-card__title-link" href="/task/view/47">Телеграм-бот для доставки</a>
            <div class="task-card__desc">Интеграция с API и Google Таблицами</div></article>'''
        weblancer = '''<article><a href="/freelance/programming/parser-27/">Парсинг данных на Python</a>
            <p>Выгружать цены поставщиков через API в локальный каталог</p></article>'''
        cases = [(sources.FREELANCE_RU, boards.FREELANCE_URL, freelance, sources.FREELANCE_RU_ID_OFFSET + 47),
                 (sources.WEBLANCER, boards.WEBLANCER_URL, weblancer, sources.WEBLANCER_ID_OFFSET + 27)]
        for site, url, html, oid in cases:
            with self.subTest(site=site), mock.patch.object(sources, "is_enabled", return_value=True):
                with mock.patch.object(boards, "_get", return_value=html) as fetch:
                    result = pipeline.collect_live(site)
                fetch.assert_called_once_with(url)
                self.assertEqual(result.error, "")
                self.assertEqual(result.intake["new"], 1)
                self.assertEqual(database.freelance_get(oid)["origin"], site)
