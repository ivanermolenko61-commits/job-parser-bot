"""Сценарии исходных дефектов из полного ревью Codex (2026-10-24): каждый тест
воспроизводит дефект и проверяет исправление. Без сети.

Запуск из корня проекта: python -m unittest tests.test_review_fixes -v
"""
import asyncio
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

os.environ.setdefault("BOT_TOKEN", "123456:TEST")

import bot  # noqa: E402
import database  # noqa: E402
from freelance import alot_client, pipeline, yandex_budget  # noqa: E402
from freelance.ai_scorer import _user_prompt, mask_contacts  # noqa: E402
from parsers import filters  # noqa: E402
from parsers.base import Vacancy  # noqa: E402


class DbTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = database.DB_PATH
        database.DB_PATH = os.path.join(self.tmp.name, "t.db")
        database.init_db()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(setattr, database, "DB_PATH", self.old_path)


class PauseTest(DbTestCase):
    def test_pause_survives_restart(self):
        self.addCleanup(setattr, bot, "subscribed", True)
        bot._set_subscribed(False)
        bot.subscribed = True            # «рестарт»: переменная в памяти сброшена
        bot._load_subscribed()
        self.assertFalse(bot.subscribed)
        bot._set_subscribed(True)
        bot.subscribed = False
        bot._load_subscribed()
        self.assertTrue(bot.subscribed)

    def test_default_is_subscribed(self):
        self.addCleanup(setattr, bot, "subscribed", True)
        bot.subscribed = False
        bot._load_subscribed()           # в БД ничего нет
        self.assertTrue(bot.subscribed)


class UnknownCompanyDedupTest(DbTestCase):
    def test_stub_company_gives_no_key(self):
        for stub in ("Не указана", "не указано", "—"):
            self.assertEqual(filters.dedup_key("Python разработчик", stub), "")
        self.assertNotEqual(filters.dedup_key("Python разработчик", "ООО Ромашка"), "")

    def test_two_vacancies_without_company_are_not_duplicates(self):
        database.mark_sent("habr", "1", "Python разработчик", "Не указана", "https://x/1", "")
        self.assertFalse(database.is_duplicate("Python разработчик", "Не указана"))


def vac(i):
    return Vacancy("habr", str(i), f"Python junior {i}", f"Компания {i}", f"https://x/{i}")


class PerVacancyMarkTest(DbTestCase):
    def run_check(self, send_effects):
        send = mock.AsyncMock(side_effect=send_effects)
        with mock.patch.object(bot, "fetch_new_vacancies", return_value=[vac(1), vac(2), vac(3)]), \
             mock.patch.object(bot.bot, "send_message", send), \
             mock.patch("asyncio.sleep", mock.AsyncMock()):
            return asyncio.run(bot.check_vacancies())

    def test_crash_mid_batch_keeps_already_sent(self):
        # третья отправка «роняет процесс» (BaseException не ловится except Exception)
        with self.assertRaises(SystemExit):
            self.run_check([None, None, SystemExit()])
        self.assertTrue(database.is_sent("habr", "1"))
        self.assertTrue(database.is_sent("habr", "2"))
        self.assertFalse(database.is_sent("habr", "3"))

    def test_failed_send_not_marked_and_others_are(self):
        self.run_check([None, RuntimeError("telegram"), None])
        self.assertTrue(database.is_sent("habr", "1"))
        self.assertFalse(database.is_sent("habr", "2"))
        self.assertTrue(database.is_sent("habr", "3"))

    def test_mark_error_does_not_stop_batch(self):
        calls = []

        def flaky(vacancies):
            calls.append(vacancies[0].vacancy_id)
            if vacancies[0].vacancy_id == "1":
                raise OSError("db locked")
            database.mark_sent(vacancies[0].source, vacancies[0].vacancy_id, vacancies[0].title,
                               vacancies[0].company, vacancies[0].url, "")

        with mock.patch.object(bot, "mark_vacancies_sent", side_effect=flaky), \
             self.assertLogs(level="ERROR"):
            self.run_check([None, None, None])
        self.assertEqual(calls, ["1", "1", "2", "3"])   # у первой две попытки
        self.assertTrue(database.is_sent("habr", "3"))


class DateTest(DbTestCase):
    def test_explicit_year_is_respected(self):
        self.assertTrue(database.normalize_date("23 октября 2025").startswith("2025-10-23"))
        self.assertFalse(database.is_fresh("23 октября 2025", 5))

    def test_year_less_date_unchanged(self):
        now = datetime.now()
        text = f"{now.day} {list(database.MONTHS_RU)[0]}"   # просто формат «день месяц»
        self.assertIsInstance(database.normalize_date(text), str)

    def test_unparsable_date_is_cleaned_by_found_at(self):
        old = (datetime.now(timezone.utc) - timedelta(days=10)).strftime("%Y-%m-%d %H:%M:%S")
        with database._connect() as conn:
            conn.execute("INSERT INTO vacancies (source, vacancy_id, title, company, url, found_at, published_at) "
                         "VALUES ('geekjob','1','t','c','u1',?, 'вчера')", (old,))
            conn.execute("INSERT INTO vacancies (source, vacancy_id, title, company, url, published_at) "
                         "VALUES ('geekjob','2','t','c','u2','вчера')")
        deleted = database.cleanup_old(days=5)
        self.assertEqual(deleted, 1)          # старая по found_at удалена, свежая осталась


class InternWordTest(unittest.TestCase):
    def test_internal_is_not_a_junior_marker(self):
        self.assertFalse(filters.is_allowed_level("Senior Python Developer (Internal Tools)"))

    def test_real_intern_markers_still_work(self):
        for title in ("Python Intern", "Python internship", "Senior Python mentor for interns"):
            self.assertTrue(filters.is_allowed_level(title), title)


class SummaryTest(DbTestCase):
    def save(self, oid, status, fit=None):
        database.freelance_save(oid, "kwork", f"заказ {oid}", f"https://x/{oid}", "1000 ₽",
                                "2026-10-24T00:00:00+00:00", f"k{oid}", fit, "{}", status)

    def test_sent_counted_by_sent_at(self):
        self.save(1, "sent", 8)
        with database._connect() as conn:   # увидели 2 суток назад, отправили только что
            conn.execute("UPDATE freelance_orders SET seen_at = datetime('now','-2 days')")
        d = database.freelance_day_summary()
        self.assertEqual(d["sent"], 1)
        self.assertEqual(d["total"], 0)

    def make_dt(self, hour):
        fixed = datetime(2026, 10, 24, hour, 5, tzinfo=bot.MSK)

        class FakeDT(datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed.astimezone(tz) if tz else fixed.replace(tzinfo=None)

        return FakeDT

    def run_summary(self, coro_fn, hour):
        send = mock.AsyncMock()
        with mock.patch.object(bot.bot, "send_message", send), \
             mock.patch.object(bot, "datetime", self.make_dt(hour)):
            asyncio.run(coro_fn())
        return send

    def test_not_sent_twice_per_day(self):
        self.assertEqual(self.run_summary(bot.send_freelance_daily_summary, 21).await_count, 1)
        self.assertEqual(self.run_summary(bot.send_freelance_daily_summary, 21).await_count, 0)

    def test_catch_up_after_restart(self):
        self.assertEqual(self.run_summary(bot.catch_up_freelance_summary, 22).await_count, 1)
        # cron в 21:00 в тот же день уже ничего не пришлёт
        self.assertEqual(self.run_summary(bot.send_freelance_daily_summary, 21).await_count, 0)

    def test_no_catch_up_before_21(self):
        self.assertEqual(self.run_summary(bot.catch_up_freelance_summary, 15).await_count, 0)

    def test_failed_send_is_retried_later(self):
        with mock.patch.object(bot.bot, "send_message", mock.AsyncMock(side_effect=RuntimeError("tg"))), \
             mock.patch.object(bot, "datetime", self.make_dt(21)), self.assertLogs(level="ERROR"):
            asyncio.run(bot.send_freelance_daily_summary())
        self.assertEqual(self.run_summary(bot.catch_up_freelance_summary, 22).await_count, 1)

    def test_paused_freelance_sends_nothing(self):
        database.kv_set("freelance_subscribed", "0")
        self.assertEqual(self.run_summary(bot.send_freelance_daily_summary, 21).await_count, 0)


class FreshnessBeforeSendTest(unittest.TestCase):
    def sc(self, oid, age_hours):
        order = alot_client.FreelanceOrder(
            oid, "kwork", "Заказ", "текст", "1000 ₽", 1000.0, [],
            datetime.now(timezone.utc) - timedelta(hours=age_hours), f"https://x/{oid}")
        return pipeline.ScoredOrder(order, {"fit": 8})

    def test_stale_order_not_sent(self):
        fresh, stale = self.sc(1, 1), self.sc(2, 7)
        self.assertFalse(bot._order_is_stale(fresh))
        self.assertTrue(bot._order_is_stale(stale))
        send = mock.AsyncMock(return_value=10)
        progress = {}
        res = pipeline.CycleResult(to_send=[fresh, stale])
        with mock.patch.object(bot, "_send_order", send), \
             mock.patch.object(bot, "save_sent", return_value=True), \
             mock.patch("asyncio.sleep", mock.AsyncMock()):
            asyncio.run(bot._deliver_freelance(res, progress))
        sent_ids = [c.args[0].order.order_id for c in send.await_args_list]
        self.assertEqual(sent_ids, [1])
        self.assertEqual([sc.order.order_id for sc in progress["retry"]], [2])
    def test_order_that_goes_stale_during_batch_is_not_sent(self):
        a, b = self.sc(1, 1), self.sc(2, 1)
        send = mock.AsyncMock(return_value=10)
        progress = {}
        # две проверки в начале (оба свежие), затем в цикле: первый свежий, второй состарился
        with mock.patch.object(bot, "_order_is_stale", side_effect=[False, False, False, True]),              mock.patch.object(bot, "_send_order", send),              mock.patch.object(bot, "save_sent", return_value=True),              mock.patch("asyncio.sleep", mock.AsyncMock()):
            asyncio.run(bot._deliver_freelance(pipeline.CycleResult(to_send=[a, b]), progress))
        self.assertEqual([c.args[0].order.order_id for c in send.await_args_list], [1])
        self.assertEqual([sc.order.order_id for sc in progress["retry"]], [2])


class YandexUsageTest(unittest.TestCase):
    M = "aliceai-llm-flash"

    def test_bad_usage_is_unreadable(self):
        bad = [
            {"prompt_tokens": 1000, "completion_tokens": -10000},
            {"prompt_tokens": float("nan"), "completion_tokens": 10},
            {"prompt_tokens": float("inf"), "completion_tokens": 1},
            {"prompt_tokens": 100, "completion_tokens": 10, "prompt_tokens_details": {"cached_tokens": -5}},
            {"prompt_tokens": "abc"},
        ]
        for usage in bad:
            self.assertIsNone(yandex_budget.usage_cost(self.M, usage), usage)

    def test_good_usage_still_priced(self):
        cost = yandex_budget.usage_cost(self.M, {"prompt_tokens": 1000, "completion_tokens": 500})
        self.assertGreater(cost, 0)

    def test_settle_charges_reserve_for_bad_usage(self):
        with mock.patch.object(yandex_budget, "_save"), self.assertLogs(level="WARNING"):
            spent = yandex_budget.settle(self.M, 1.5, {"prompt_tokens": 1000, "completion_tokens": -10000})
        self.assertEqual(spent, 1.5)


class MaskContactsTest(unittest.TestCase):
    def test_masks_contacts(self):
        text = mask_contacts("Звоните +7 (903) 123-45-67 или 89031234567, пишите ivan@mail.ru, "
                             "ключ " + "sk" + "-abcdefghijklmnopqrstuvwxyz123456")
        for secret in ("903", "89031234567", "ivan@mail.ru", "abcdefghijkl"):
            self.assertNotIn(secret, text)
        self.assertIn("[телефон]", text)
        self.assertIn("[email]", text)

    def test_keeps_task_meaning(self):
        text = "Бюджет 15 000 000 ₽, 50000-100000 руб, бот @shop_bot, сайт https://example.com/a?id=1"
        self.assertEqual(mask_contacts(text), text)

    def test_prompt_is_masked(self):
        order = alot_client.FreelanceOrder(1, "kwork", "Бот, тел 89031234567", "почта a@b.ru", "1000 ₽",
                                           1000.0, [], datetime.now(timezone.utc), "https://x/1")
        prompt = _user_prompt(order)
        self.assertNotIn("89031234567", prompt)
        self.assertNotIn("a@b.ru", prompt)

    def test_international_phones(self):
        for phone in ("+375 29 123-45-67", "+44 20 7946 0958", "+1 (415) 555-2671", "+7 903 123 45 67"):
            self.assertEqual(mask_contacts(f"тел {phone} звонить"), "тел [телефон] звонить", phone)

    def test_contact_on_truncation_boundary_is_masked(self):
        from freelance import ai_scorer
        body = "x " * 1490 + "пишите ivan@private-company.ru"   # email пересекает BODY_LIMIT
        self.assertGreater(len(body), ai_scorer.BODY_LIMIT)
        order = alot_client.FreelanceOrder(1, "kwork", "Бот", body, "1000 ₽", 1000.0, [],
                                           datetime.now(timezone.utc), "https://x/1")
        prompt = _user_prompt(order)
        self.assertNotIn("ivan@", prompt)
        self.assertNotIn("private", prompt)

    def test_alot_parse_error_does_not_log_content(self):
        item = {"title": "тел 89031234567 a@b.ru"}      # нет обязательных полей
        with self.assertLogs(level="WARNING") as cm:
            self.assertIsNone(alot_client._parse_item(item))
        joined = "\n".join(cm.output)
        self.assertNotIn("89031234567", joined)
        self.assertNotIn("a@b.ru", joined)


if __name__ == "__main__":
    unittest.main()
