import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from unittest import mock

import requests

import database
from freelance import journal as jr, manual, pipeline, public_boards as boards, sources
from freelance.filters import reject_reason
from freelance.report import format_funnel, load_rows
from tests.test_pipeline_cycle import CycleTestBase, ai_result


def _card(number, title, body, stamp=''):
    return f'''<article class="task-card">
        <a class="task-card__title-link" href="/task/view/{number}">{title}</a>
        <div class="task-card__desc">{body}</div>
        <div class="task-card__foot-item" title="{stamp}"><i class="fa fa-clock-o"></i></div></article>'''


def _detail(body):
    return f'<div class="tv-hero"><h1>Задание</h1><p>{body}</p><div class="tv-hero__meta"></div></div>'


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

    def test_graphics_automation_after_character_600_survives_prefilter(self):
        title = "Инфографика для карточек"
        manual_graphics = "Цвета согласовать с заказчиком. " * 30
        automation = manual_graphics + "Нужен скрипт для автоматизации по API."
        order, hand_drawn = boards.parse_freelance_page(
            _card(1, title, automation) + _card(2, title, manual_graphics))
        self.assertGreater(order.body.index("скрипт"), 600)
        self.assertIsNone(reject_reason(order))
        self.assertEqual(reject_reason(hand_drawn), "marketplace_graphics")

    def test_challenge_is_not_empty_success(self):
        for parse in (boards.parse_weblancer_page, boards.parse_freelance_page):
            with self.assertRaises(ValueError):
                parse("<title>Just a moment...</title>")


class BoardsIntakeTest(CycleTestBase):
    def test_six_previews_retry_after_restart_and_do_not_block_ready_delivery(self):
        ids = [sources.FREELANCE_RU_ID_OFFSET + i for i in range(1, 7)]
        page = _card(1, "Инфографика для карточек", "Цвета согласовать…")
        stamp = (datetime.now(boards.MSK) - timedelta(minutes=2)).strftime("%d.%m.%Y %H:%M")
        page += "".join(_card(i, f"Телеграм-бот {i}", "Интеграция с API...", stamp) for i in range(2, 7))
        full = "Цвета согласовать с заказчиком. " * 30 + "Нужен скрипт для автоматизации по API."
        entered, release, ready_to_score = threading.Event(), threading.Event(), threading.Event()
        attempt = 1
        intake = pipeline._intake

        def notify_intake(*args, **kwargs):
            result = intake(*args, **kwargs)
            if result[2]:
                ready_to_score.set()
            return result

        def fetch(url):
            if url == boards.FREELANCE_URL:
                return page
            number = int(url.rsplit("/", 1)[-1])
            if number == 1:
                if attempt == 1:
                    entered.set()
                    if not release.wait(5):
                        raise RuntimeError("время ожидания теста")
                    raise requests.Timeout()
                if attempt == 2:
                    return '<title>Без описания</title>'
                return _detail(full)
            return _detail(f"Телеграм-бот с настройкой API. Полное описание {number}")

        with mock.patch.object(sources, "is_enabled", return_value=True), \
                mock.patch.object(pipeline, "_intake", side_effect=notify_intake), \
                mock.patch.object(boards, "_get", side_effect=fetch) as http:
            with ThreadPoolExecutor(max_workers=1) as pool:
                scan = pool.submit(pipeline.collect_live, sources.FREELANCE_RU)
                try:
                    self.assertTrue(entered.wait(5))
                    self.assertIsNone(database.freelance_get(ids[0])["fit"])
                    self.assertTrue(ready_to_score.wait(4))
                    rows = pipeline.live_rows("score", 6)
                    self.assertNotIn(ids[0], {row["order_id"] for row in rows})
                    with mock.patch.object(pipeline.ai_scorer, "score_order", return_value=ai_result(8)):
                        self.assertEqual(pipeline.score_live(rows[0]).ai_ok, 1)
                    ready = database.freelance_claim_delivery(rows[0]["order_id"])
                    self.assertTrue(database.freelance_finish_delivery(ready["order_id"], ready["delivery_token"], 101))
                    self.assertFalse(scan.done())
                finally:
                    release.set()
                self.assertEqual(scan.result(timeout=5).intake["new"], 3)
            self.assertEqual(http.call_count, 1 + boards.MAX_DETAIL)
            self.assertEqual(database.freelance_get(ids[0])["attempts"], 1)
            self.assertEqual(database.freelance_day_summary()["details_pending"], 3)
            self.assertIn("ждут полного описания 3", format_funnel(load_rows(1)))

            with database._connect() as conn:
                conn.execute("UPDATE freelance_orders SET seen_at=datetime('now','-5 minutes')")
            seen = {oid: database.freelance_get(oid)["seen_at"] for oid in ids}
            attempt = 2
            http.reset_mock()
            self.assertEqual(pipeline.collect_live(sources.FREELANCE_RU).intake["new"], 2)
            self.assertEqual(http.call_count, 4)
            self.assertEqual(database.freelance_get(ids[0])["tech"], jr.DETAILS_PENDING)
            self.assertEqual(database.freelance_get(ids[0])["attempts"], 2)

            database.init_db()
            attempt = 3
            # Проверяем восстановление из SQLite, даже если заказ ушёл из выдачи.
            page = "Заданий не найдено"
            http.reset_mock()
            self.assertEqual(pipeline.collect_live(sources.FREELANCE_RU).intake["new"], 1)
            self.assertEqual(http.call_count, 2)
            for oid, original_seen in seen.items():
                row = database.freelance_get(oid)
                self.assertIn(row["tech"], (jr.QUEUED, jr.ASSESSED))
                self.assertEqual(row["seen_at"], original_seen)
                self.assertNotIn("...", row["snippet"])
            self.assertIn("автоматизации по API", database.freelance_get(ids[0])["snippet"])

    def test_unexpected_detail_error_does_not_back_off_the_source(self):
        page = _card(1, "Телеграм-бот", "Нужен бот…")
        page += _card(2, "Интеграция с API", "Собрать выгрузку…")

        def fetch(url):
            if url == boards.FREELANCE_URL:
                return page
            if url.endswith("/1"):
                raise TypeError("неожиданный сбой парсера")
            return _detail("Нужен скрипт для интеграции API и выгрузки в Google Таблицы")

        with mock.patch.dict(pipeline._source_retry_at, clear=True), \
                mock.patch.object(sources, "is_enabled", return_value=True), \
                mock.patch.object(boards, "_get", side_effect=fetch), \
                self.assertLogs(level="ERROR"):
            result = pipeline.collect_live(sources.FREELANCE_RU)
            self.assertNotIn(sources.FREELANCE_RU, pipeline._source_retry_at)

        failed, ready = (database.freelance_get(sources.FREELANCE_RU_ID_OFFSET + i) for i in (1, 2))
        self.assertFalse(result.error)
        self.assertEqual(result.intake["new"], 1)
        self.assertEqual((failed["tech"], failed["attempts"]), (jr.DETAILS_PENDING, 1))
        self.assertEqual(ready["tech"], jr.QUEUED)

    def test_detail_retry_uses_publication_or_original_seen_at_for_expiry(self):
        stamp = (datetime.now(boards.MSK) - timedelta(minutes=61)).strftime("%d.%m.%Y %H:%M")
        page = _card(1, "Телеграм-бот", "Нужно сделать интеграцию...", stamp)
        page += _card(2, "Телеграм-бот для учёта", "Нужен бот…")
        with mock.patch.object(sources, "is_enabled", return_value=True), \
                mock.patch.object(boards, "_get", side_effect=[page, "<title>Без описания</title>", page]) as http:
            pipeline.collect_live(sources.FREELANCE_RU)
            old, unknown = (sources.FREELANCE_RU_ID_OFFSET + i for i in (1, 2))
            self.assertEqual(database.freelance_get(old)["stale_reason"], jr.S_DETAILS_TOO_OLD)
            self.assertEqual(database.freelance_day_summary()["details_expired"], 1)
            with database._connect() as conn:
                conn.execute("UPDATE freelance_orders SET seen_at=datetime('now','-61 minutes') WHERE order_id=?",
                             (unknown,))
            pipeline.collect_live(sources.FREELANCE_RU)
            self.assertEqual(http.call_count, 3)
        self.assertEqual(database.freelance_expire_live(), 1)
        self.assertEqual(database.freelance_get(unknown)["stale_reason"], jr.S_DETAILS_EXPIRED)
        self.assertEqual(database.freelance_day_summary()["details_expired"], 2)

    def test_full_description_arriving_after_freshness_deadline_is_not_scored(self):
        oid = sources.FREELANCE_RU_ID_OFFSET + 1
        page = _card(1, "Телеграм-бот для учёта", "Интеграция с API...")

        def fetch(url):
            if url == boards.FREELANCE_URL:
                return page
            with database._connect() as conn:
                conn.execute("UPDATE freelance_orders SET seen_at=datetime('now','-61 minutes') WHERE order_id=?",
                             (oid,))
            return _detail("Разработать бота с интеграцией по API")

        with mock.patch.object(sources, "is_enabled", return_value=True), \
                mock.patch.object(boards, "_get", side_effect=fetch):
            result = pipeline.collect_live(sources.FREELANCE_RU)
            self.assertEqual(pipeline.live_rows("score", 10), [])
        self.assertEqual(result.intake["stale"], 1)
        self.assertEqual(database.freelance_get(oid)["stale_reason"], jr.S_DETAILS_EXPIRED)

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
