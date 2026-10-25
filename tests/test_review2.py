"""Исправления по повторному полному ревью (октябрь 2026): потери вакансий и заказов, квоты. Без сети.

Запуск из корня проекта: python -m unittest tests.test_review2 -v
"""
import os
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

from bs4 import BeautifulSoup

import database
from freelance import ai_scorer, alot_client, pipeline
from freelance.alot_client import FreelanceOrder
from freelance.filters import is_candidate
from parsers.filters import LEVEL_PATTERN, is_allowed_level
from parsers.habr_parser import HabrParser


def order(title, oid=1, categories=("WEB",)):
    return FreelanceOrder(oid, "youdoru", title, "", "5000 ₽", 5000.0, list(categories),
                          datetime.now(timezone.utc), f"https://x/{oid}")


class LevelWordTest(unittest.TestCase):
    def test_level_matches_whole_words_only(self):
        for text in ("Python Developer - Internal Tools", "Middleware developer", "Team Leader",
                     "Seniority matrix", "Internet shop"):
            with self.subTest(text=text):
                self.assertIsNone(LEVEL_PATTERN.search(text))
                self.assertTrue(is_allowed_level(text))

    def test_real_levels_still_found(self):
        for text, level in (("Middle Python", "Middle"), ("Senior/Lead", "Senior"),
                            ("Intern", "Intern"), ("Junior-разработчик", "Junior"),
                            ("Стажёр Python", "Стажёр")):
            with self.subTest(text=text):
                self.assertEqual(LEVEL_PATTERN.search(text).group(1), level)
        self.assertFalse(is_allowed_level("Middle Python"))
        self.assertTrue(is_allowed_level("Middle / Junior"))


class HabrMetaTest(unittest.TestCase):
    def test_middle_junior_meta_keeps_both_levels(self):
        card = BeautifulSoup('<div class="vacancy-card__meta">Middle / Junior Москва Можно удалённо</div>',
                             "html.parser")
        _loc, level, remote = HabrParser()._parse_meta(card)
        self.assertIn("Junior", level)
        self.assertIn("Middle", level)
        self.assertTrue(is_allowed_level(level))
        self.assertTrue(remote)

    def test_single_level_and_none(self):
        card = BeautifulSoup('<div class="vacancy-card__meta">Senior Москва</div>', "html.parser")
        self.assertEqual(HabrParser()._parse_meta(card)[1], "Senior")
        card = BeautifulSoup('<div class="vacancy-card__meta">Москва</div>', "html.parser")
        self.assertEqual(HabrParser()._parse_meta(card)[1], "")


class DesignWordTest(unittest.TestCase):
    def test_given_design_does_not_exclude(self):
        for title in ("Вёрстка сайта по готовому дизайну", "Сверстать сайт, дизайн готов",
                      "Сделать сайт по макету, дизайн есть", "Нужен лендинг по утверждённому дизайну"):
            with self.subTest(title=title):
                self.assertTrue(is_candidate(order(title)))

    def test_ordering_design_still_excluded(self):
        for title in ("Нужен дизайн сайта", "Дизайн логотипа и сайт", "Разработать дизайн лендинга"):
            with self.subTest(title=title):
                self.assertFalse(is_candidate(order(title)))


class FitRangeTest(unittest.TestCase):
    def test_bool_and_out_of_range_rejected(self):
        for text in ('{"fit": true}', '{"fit": false}', '{"fit": 85}', '{"fit": -100}', '{"fit": 11}',
                     '{"fit": NaN}'):
            with self.subTest(text=text):
                self.assertIsNone(ai_scorer.parse_ai_json(text))

    def test_valid_boundaries(self):
        for text, fit in (('{"fit": 0}', 0), ('{"fit": 10}', 10), ('{"fit": 7.6}', 8), ('{"fit": "6"}', 6)):
            with self.subTest(text=text):
                self.assertEqual(ai_scorer.parse_ai_json(text)["fit"], fit)


class CountLoadTest(unittest.TestCase):
    def provider(self):
        return ai_scorer._build_providers({"OPENROUTER_API_KEY": "k"})["openrouter"]

    def test_failed_read_is_retried_and_does_not_overwrite_stored_count(self):
        p = self.provider()
        today = ai_scorer._today(p)
        with mock.patch("database.kv_get", side_effect=[OSError("db"), f"{today}:44"]), \
                mock.patch("database.kv_set") as kv_set:
            ai_scorer._load_count(p)
            self.assertFalse(p.count_loaded)
            p.day, p.count = today, 1
            ai_scorer._save_count(p)
            kv_set.assert_not_called()                  # 44 в БД не затёрто единицей
            ai_scorer._load_count(p)
        self.assertTrue(p.count_loaded)
        self.assertEqual(p.count, 44)


class TriageSharedQuotaTest(unittest.TestCase):
    def setUp(self):
        providers = ai_scorer._build_providers({"CLOUDFLARE_API_KEY": "k", "CLOUDFLARE_ACCOUNT_ID": "abc123"})
        self.cf = providers["cloudflare"]
        for p in (mock.patch.object(ai_scorer, "PROVIDERS", providers),
                  mock.patch.object(ai_scorer, "CHAIN", ["cloudflare"]),
                  mock.patch.object(ai_scorer, "_triage_day", ""),
                  mock.patch.object(ai_scorer, "_triage_count", 0),
                  mock.patch.object(ai_scorer, "_triage_paused_until", 0.0),
                  mock.patch.object(ai_scorer, "_triage_disabled", False),
                  mock.patch.object(ai_scorer, "_triage_loaded", True),
                  mock.patch.object(ai_scorer, "_triage_save_count")):
            p.start()
            self.addCleanup(p.stop)

    def test_triage_skipped_when_full_scoring_hit_daily_limit(self):
        import time
        for m in self.cf.models:
            self.cf.paused[m] = time.monotonic() + 3 * 3600
        with mock.patch("requests.post") as post:
            self.assertIsNone(ai_scorer.triage_order(alot_client.FreelanceOrder(
                1, "s", "Бот", "", "", 0.0, [], datetime.now(timezone.utc), "u")))
        post.assert_not_called()

    def test_daily_429_on_triage_pauses_full_scoring_too(self):
        r = mock.Mock(status_code=429, headers={"retry-after": "7200"}, text="daily limit")
        with mock.patch("requests.post", return_value=r):
            self.assertIsNone(ai_scorer.triage_order(alot_client.FreelanceOrder(
                1, "s", "Бот", "", "", 0.0, [], datetime.now(timezone.utc), "u")))
        self.assertTrue(all(t > 0 for t in self.cf.paused.values()))
        self.assertEqual(set(self.cf.paused), set(self.cf.models))


class PipelineBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = database.DB_PATH
        database.DB_PATH = os.path.join(self.tmp.name, "t.db")
        database.init_db()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(setattr, database, "DB_PATH", self.old_path)
        pipeline._triage_cache.clear()
        for p in (mock.patch.object(pipeline.monitor, "record"),
                  mock.patch.object(pipeline, "_report_ai_health"),
                  mock.patch.object(pipeline.ai_scorer, "is_enabled", return_value=True),
                  mock.patch.object(pipeline.ai_scorer, "daily_capacity", return_value=None),
                  mock.patch.object(pipeline.ai_scorer, "quota_exhausted", return_value=None)):
            p.start()
            self.addCleanup(p.stop)
        database.kv_set(pipeline.LAST_ID_KEY, 100)

    def run_cycle(self, o, triage=None, mode="on", full=None, min_fit=None):
        scan = alot_client.ScanResult(orders=[o], max_id=o.order_id, raw_count=1)
        patches = [mock.patch.object(pipeline, "TRIAGE_MODE", mode),
                   mock.patch.object(pipeline, "fetch_new_orders", return_value=scan),
                   mock.patch.object(pipeline.ai_scorer, "triage_order", return_value=triage),
                   mock.patch.object(pipeline.ai_scorer, "score_order", return_value=full)]
        if min_fit is not None:
            patches.append(mock.patch.object(pipeline, "FREELANCE_MIN_FIT", min_fit))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return pipeline._prepare_cycle()


FULL = {"fit": 8, "difficulty": "легко", "hours": 4.0, "category": "tg_bot",
        "components": [{"name": "бот", "hours": 4}], "unknowns": [], "clarity": "ясно",
        "client_budget_ok": True, "summary": "s", "risks": "", "questions": [], "model": "t:m"}


class TriageMinFitTest(PipelineBase):
    def test_triage_reject_is_final_even_if_min_fit_is_low(self):
        # FREELANCE_MIN_FIT=2 <= оценка триажа 2: заказ не должен уйти в чат без полной оценки
        res = self.run_cycle(order("Бот", 301), triage=2, mode="on", full=dict(FULL), min_fit=2)
        self.assertEqual([s.order.order_id for s in res.rejected], [301])
        self.assertEqual(res.to_send, [])


class SaveRightAfterScoreTest(PipelineBase):
    def test_score_saved_before_cycle_ends(self):
        seen = {}
        real = pipeline._save_scored_now

        def spy(sc, result):
            real(sc, result)
            seen["row"] = database.freelance_get(sc.order.order_id)

        with mock.patch.object(pipeline, "_save_scored_now", side_effect=spy):
            self.run_cycle(order("Бот", 302), triage=None, mode="off", full=dict(FULL))
        self.assertEqual(seen["row"]["status"], "pending")      # в БД уже в момент оценки
        self.assertEqual(seen["row"]["fit"], 8)
        self.assertIn("price_mid", seen["row"]["ai_json"])

    def test_save_failure_does_not_break_scoring(self):
        with mock.patch.object(pipeline, "_save_retry", side_effect=[RuntimeError("db")] + [True] * 5):
            res = self.run_cycle(order("Бот", 303), triage=None, mode="off", full=dict(FULL))
        self.assertEqual([s.order.order_id for s in res.to_send], [303])


if __name__ == "__main__":
    unittest.main()
