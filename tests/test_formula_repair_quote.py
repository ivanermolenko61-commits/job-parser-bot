import json
import os
import tempfile
import unittest
from unittest.mock import patch

import database
from freelance import draft, pipeline, pricing, sources


TITLE = "Автоматизация расчетов в Exel"
BODY = ("Нужно автоматизировать расчеты в Exel формате. Проект строительство металлоконструкций. "
        "Есть действующая автоматизация с формулами, нужно ее откорректировать.")


def cached_row(**fields):
    old = {"fit": 7, "category": "sheets", "hours": 6, "components": [{"name": "Правка", "hours": 6}],
           "price_v": 4, "price_low": 7400, "price_mid": 10500, "price_high": 13500,
           "price_min": 5600, "price_vague": True, "hours_low": 7.2, "hours_high": 10.4,
           "budget_status": "below_min", "questions": ["Какие именно расчёты нужно скорректировать?"]}
    return {"order_id": sources.MANUAL_ID_OFFSET + 1, "origin": "manual", "site": "youdo",
            "title": TITLE, "snippet": BODY, "price_text": "до 1 000 ₽", "ai_json": json.dumps(old), **fields}


class FormulaRepairQuoteTest(unittest.TestCase):
    def setUp(self):
        for name, value in (("FREELANCE_RATE_PER_HOUR", 1000), ("FREELANCE_MIN_RATE_PER_HOUR", 775),
                            ("FREELANCE_FX_RATES", "")):
            setting = patch.object(pricing, name, value)
            setting.start()
            self.addCleanup(setting.stop)

    def test_live_quote_is_separate_from_full_job_history(self):
        row = cached_row()
        raw = json.loads(row.pop("ai_json"))
        with (patch.object(pipeline, "live_fresh", return_value=True),
              patch.object(pipeline.ai_scorer, "quota_exhausted", return_value=None),
              patch.object(pipeline.ai_scorer, "is_enabled", return_value=True),
              patch.object(pipeline.ai_scorer, "score_order", return_value=raw),
              patch.object(pipeline, "freelance_live_score_result", return_value=True) as save):
            self.assertEqual(pipeline.score_live(row).ai_ok, 1)
        self.assertIsNone(save.call_args.kwargs["hours"])
        assessed = json.loads(save.call_args.kwargs["ai_json"])
        self.assertEqual(assessed["price_scope_bid"], 800)
        for old_field in ("hours", "components", "hours_low", "price_mid", "price_full", "price_min", "budget_status"):
            self.assertNotIn(old_field, assessed)

    def test_saved_card_and_draft_use_source_without_changing_saved_offer(self):
        row = cached_row(lead="agreed", deal_price=950)
        saved = row.copy()
        sc = pipeline.scored_from_row(row)
        public, private = draft.draft_messages(row)
        for message in (pipeline.format_order(sc), pipeline.format_order(sc, detailed=True), private):
            self.assertIn("800 ₽", message)
            self.assertIn("30–60 мин", message)
            self.assertIn("одна локальная правка", message)
            self.assertNotIn("ниже минимума", message)
            self.assertNotIn("7 400", message)
            self.assertNotIn("6 ч", message)
        self.assertIn(pricing.FORMULA_QUESTION, public)
        self.assertNotIn("800", public)
        self.assertEqual(row, saved)

    def test_scope_requires_source_evidence(self):
        for title, body in (("Исправить 1 формулу в Excel", "1 файл"),
                            ("Google Sheets", "В готовой таблице нужно поправить формулу в ячейке итога.")):
            with self.subTest(title=title):
                self.assertTrue(pricing.apply_formula_quote({"fit": 7}, title, body, None))
        for body in ("Создать калькулятор в Excel с формулами и проверкой ошибок.",
                     "Собрать таблицу с формулами с нуля, исправить ошибку при вводе.",
                     "Исправить формулы, добавить интеграцию с CRM.",
                     "Исправить формулу и добавить сводный отчёт.",
                     "Исправить 20 формул и переработать несколько листов.",
                     "Исправить формулы. Нужны макросы VBA.",
                     "Есть готовый файл. Нужна автоматизация."):
            with self.subTest(body=body):
                data = json.loads(cached_row()["ai_json"])
                original = data.copy()
                self.assertFalse(pricing.apply_formula_quote(data, TITLE, body, "до 500 ₽"))
                self.assertEqual(data, original)

    def test_bid_respects_hourly_settings_and_compares_only_total_ceilings(self):
        with patch.object(pricing, "FREELANCE_MIN_RATE_PER_HOUR", 830):
            for budget, above in (("до 500 ₽", True), ("до 100 000 ₽", False),
                                  ("от 500 ₽", False), ("до 500 ₽/ч", False),
                                  ("500", False), ("до 5 USD", False)):
                with self.subTest(budget=budget):
                    ai = {"fit": 7}
                    pricing.apply_formula_quote(ai, TITLE, BODY, budget)
                    self.assertEqual(ai["price_scope_bid"], 900)
                    self.assertEqual(bool(ai.get("price_scope_over_budget")), above)

    def test_changed_description_waits_for_reassessment(self):
        row = cached_row(desc_changed=1)
        with patch.object(pipeline, "freelance_reassess_state", return_value={"significant": 1, "status": "pending"}):
            sc = pipeline.scored_from_row(row)
        self.assertNotIn("price_mid", sc.ai)
        self.assertNotIn("hours", sc.ai)
        self.assertIn(pipeline.STALE_PENDING, pipeline.format_order(sc))
        self.assertNotIn("800 ₽", draft.draft_messages(row)[1])
        row["desc_changed"] = 0
        self.assertEqual(pipeline.scored_from_row(row).ai["price_scope_bid"], 800)

    def test_reused_old_assessment_is_repriced_for_matching_version(self):
        row = cached_row(content_hash="restored", tech=pipeline.jr.ASSESSED, desc_changed=1)
        with (patch.object(pipeline, "freelance_get", return_value=row),
              patch.object(pipeline, "freelance_assessment_get", return_value={"ai_json": row["ai_json"]}),
              patch.object(pipeline, "freelance_replace_assessment", return_value=True) as save,
              patch.object(pipeline, "freelance_reassess_finish"),
              patch.object(pipeline.ai_scorer, "score_order") as score):
            pipeline._reassess_one(pipeline.CycleResult(), row)
        score.assert_not_called()
        self.assertEqual(json.loads(save.call_args.args[3])["price_scope_bid"], 800)
        self.assertIsNone(save.call_args.args[5])

    def test_reopening_preserves_scope_even_if_the_decisive_detail_was_truncated(self):
        limit = pipeline.ai_scorer.BODY_LIMIT
        for full_body, expected in ((BODY + " " * limit + " Требуется интеграция с CRM.", "full"),
                                    ("Подробности. " * limit + BODY, "single_formula")):
            with self.subTest(expected=expected):
                row = cached_row(snippet=full_body[:limit])
                order = pipeline.scored_from_row(row).order
                order.body = full_body
                fresh = pipeline.ScoredOrder(order, json.loads(row["ai_json"]))
                pipeline._ensure_price(fresh)
                self.assertEqual(fresh.ai["price_scope"], expected)
                row["ai_json"] = json.dumps(fresh.ai)
                for prefix in (row["snippet"], ""):
                    row["snippet"] = prefix
                    reopened = pipeline.scored_from_row(row)
                    self.assertEqual(reopened.ai["price_scope"], expected)
                    self.assertEqual("800 ₽" in draft.draft_messages(row)[1], expected == "single_formula")

    def test_truncated_snippet_never_converts_an_unclassified_old_or_new_quote(self):
        limit = pipeline.ai_scorer.BODY_LIMIT
        row = cached_row(snippet=BODY.ljust(limit, "."))
        cached = pipeline.scored_from_row(row)
        self.assertNotIn("price_scope_bid", cached.ai)
        self.assertNotIn("800 ₽", draft.draft_messages(row)[1])
        cached.order.body = row["snippet"]
        pipeline._ensure_price(cached)
        self.assertEqual(cached.ai["price_scope"], "full")
        self.assertEqual(cached.ai["hours"], 6)

    def test_pending_refresh_clears_full_hours_from_sqlite_calibration(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(database, "DB_PATH", os.path.join(temp, "test.db")):
            database.init_db()
            row = cached_row()
            oid = row["order_id"]
            database.freelance_save(oid, row["site"], TITLE, "", row["price_text"], None,
                                    "repair", 7, row["ai_json"], "pending", category="sheets", hours=6)
            database.freelance_set_deal_price(oid, 800)
            database.freelance_set_actuals(oid, hours=.5)
            self.assertEqual(len(database.freelance_deals("sheets")), 1)
            sc = pipeline.scored_from_row(row)
            sc.order.body = BODY
            pipeline.save_order(sc, "pending")
            self.assertEqual(database.freelance_get(oid)["deal_price"], 800)
            self.assertEqual(database.freelance_deals("sheets"), [])
            self.assertEqual(pricing.hours_calibration(database.freelance_actuals_rows(), min_points=1), {})
