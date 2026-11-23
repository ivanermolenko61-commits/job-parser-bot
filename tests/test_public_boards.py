import unittest
from datetime import datetime, timezone

from freelance import manual, public_boards as boards, sources
from freelance.filters import reject_reason


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
            <span data-status="{status}"></span>{stamp}</article>'''

        now = datetime(2026, 11, 23, 19, 0, tzinfo=timezone.utc)
        page = card(1, "22.11.2026") + card(2, "23.11.2026") + card(3, "23.11.2026 21:59")
        page += card(4, "23.11.2026", "closed")
        orders = boards.parse_weblancer_page(page, now)
        self.assertEqual([o.order_id - sources.WEBLANCER_ID_OFFSET for o in orders], [2, 3])
        self.assertIsNone(orders[0].published_at)
        self.assertEqual(orders[1].published_at, datetime(2026, 11, 23, 18, 59, tzinfo=timezone.utc))

    def test_challenge_is_not_empty_success(self):
        for parse in (boards.parse_weblancer_page, boards.parse_freelance_page):
            with self.assertRaises(ValueError):
                parse("<title>Just a moment...</title>")
