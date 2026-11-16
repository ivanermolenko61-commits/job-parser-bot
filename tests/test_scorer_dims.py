"""Ф06: раздельная оценка (профиль, выполнимость, ясность, достаточность данных), опыт из каталога, промпт без бюджета.

Запуск из корня проекта: python -m unittest tests.test_scorer_dims -v
"""
import json
import os
import unittest
from unittest import mock

from freelance import ai_scorer, profile
from freelance.alot_client import FreelanceOrder
from datetime import datetime, timezone


def order(title="Таблица", body="", price="1000 ₽"):
    return FreelanceOrder(order_id=1, site="flru", title=title, body=body, price_text=price, price_value=0.0,
                          categories=[], published_at=datetime.now(timezone.utc), url="https://x/1")


GOOD = {"fit": 8, "category": "sheets", "clarity": "ясно", "feasibility": "высокая", "urgency": "срочно",
        "profile_reason": "импорт между таблицами", "unknowns": [], "components": [{"name": "импорт", "hours": 3}]}


class PromptTest(unittest.TestCase):
    def test_budget_does_not_drive_fit(self):
        text = ai_scorer.SYSTEM_PROMPT
        for phrase in ("бюджет адекватен", "бюджет скромный", "бюджет явно мал", "абсурдно мал", "меньше 500 ₽",
                       "client_budget_ok"):
            self.assertNotIn(phrase, text, phrase)
        self.assertIn("НЕ учитывай", text)

    def test_schema_asks_for_separate_dimensions(self):
        text = ai_scorer.SYSTEM_PROMPT
        for key in ('"feasibility"', '"urgency"', '"profile_reason"', '"clarity"', '"unknowns"', '"questions"'):
            self.assertIn(key, text, key)

    def test_budget_text_never_reaches_the_model(self):
        for price in ("1000 ₽", "50 000 ₽", "$500", ""):
            prompt = ai_scorer._user_prompt(order("Таблица", "Нужен импорт между таблицами", price))
            self.assertNotIn("Цена", prompt)
            self.assertNotIn(price or "§", prompt.split("Описание:")[0])
        a = ai_scorer._user_prompt(order("Т", "текст", "1000 ₽"))
        b = ai_scorer._user_prompt(order("Т", "текст", "90000 $"))
        self.assertEqual(a, b)

    def test_vagueness_is_not_a_reason_for_low_fit(self):
        text = ai_scorer.SYSTEM_PROMPT
        self.assertNotIn("размытые или подозрительные", text)
        self.assertNotIn("много неизвестных", text)
        self.assertIn("не причина низкого fit", text)

    def test_no_invented_experience_rule(self):
        self.assertIn("не придумывай клиентов", ai_scorer.SYSTEM_PROMPT)

    def test_stages_requested_as_components(self):
        self.assertIn("проверка на данных клиента", ai_scorer.SYSTEM_PROMPT)

    def test_prompt_is_signature_input(self):
        before = ai_scorer.triage_signature()
        with mock.patch.object(ai_scorer, "SYSTEM_PROMPT", ai_scorer.SYSTEM_PROMPT + " ."):
            self.assertNotEqual(before, ai_scorer.triage_signature())


class ParseDimensionsTest(unittest.TestCase):
    def parse(self, **over):
        return ai_scorer.parse_ai_json(json.dumps({**GOOD, **over}, ensure_ascii=False))

    def test_new_fields_are_normalized(self):
        r = self.parse(feasibility="Высокая", urgency="СРОЧНО")
        self.assertEqual((r["feasibility"], r["urgency"], r["profile_reason"]), ("высокая", "срочно", "импорт между таблицами"))

    def test_unknown_values_become_empty_not_errors(self):
        r = self.parse(feasibility="отлично", urgency=5, profile_reason=None)
        self.assertEqual((r["feasibility"], r["urgency"], r["profile_reason"]), ("", "", ""))

    def test_old_record_with_budget_flag_still_parses(self):
        r = self.parse(client_budget_ok=False)
        self.assertIs(r["client_budget_ok"], False)
        self.assertEqual(r["fit"], 8)

    def test_profile_reason_is_capped(self):
        self.assertLessEqual(len(self.parse(profile_reason="я" * 1000)["profile_reason"]), 200)


class SufficiencyTest(unittest.TestCase):
    def test_levels(self):
        f = ai_scorer.assess_sufficiency
        self.assertEqual(f("ясно", [], 500), ("достаточно", "высокая"))
        self.assertEqual(f("ясно", [], 100), ("частично", "средняя"))       # короткий текст не даёт «достаточно»
        self.assertEqual(f("частично", ["доступ"], 500), ("частично", "средняя"))
        self.assertEqual(f("размыто", [], 500), ("недостаточно", "низкая"))
        self.assertEqual(f("частично", [], 50), ("недостаточно", "низкая"))
        self.assertEqual(f("ясно", ["a", "b", "c"], 500)[1], "низкая")       # много неизвестных
        self.assertEqual(f("ясно", [], 0), ("недостаточно", "низкая"))       # текста нет вовсе
        self.assertEqual(f("", [], 500)[1], "низкая")                        # ясность не названа

    def test_score_order_uses_real_text_length(self):
        short = order(body="сделать")
        long_ = order(body="Нужно настроить импорт данных между таблицами. " * 20)
        with mock.patch.object(ai_scorer, "_chain_active", return_value=["groq"]), \
                mock.patch.object(ai_scorer, "_ask_provider", side_effect=lambda *a, **k: ai_scorer.parse_ai_json(
                    json.dumps(GOOD, ensure_ascii=False))), \
                mock.patch.dict(ai_scorer.PROVIDERS, {"groq": mock.Mock()}, clear=False):
            r_short = ai_scorer.score_order(short)
            r_long = ai_scorer.score_order(long_)
        self.assertNotEqual(r_short["sufficiency"], "достаточно")
        self.assertEqual((r_long["sufficiency"], r_long["confidence"]), ("достаточно", "высокая"))


class ExperienceInPromptTest(unittest.TestCase):
    def test_only_titles_of_matching_entries_go_to_ai(self):
        prompt = ai_scorer._user_prompt(order("Импорт и синхронизация таблиц",
                                              "Нужен импорт данных между таблицами, Apps Script, синхронизация."))
        self.assertIn("Подтверждённый опыт исполнителя (только это", prompt)
        self.assertIn("импорт и синхронизация данных между Google Таблицами", prompt)
        for private in ("skills/", "profile.md", "knowledge/", ".md", "projects/"):
            self.assertNotIn(private, prompt, private)
        self.assertNotIn("Telegram-боты на Python", prompt)         # неподходящая запись не передаётся

    def test_single_word_match_and_ids_are_in_block(self):
        prompt = ai_scorer._user_prompt(order("Нужен парсер", "Собрать данные."))
        self.assertIn("[tg_bots]", prompt)

    def test_no_matching_experience_is_explicit(self):
        prompt = ai_scorer._user_prompt(order("Дизайн логотипа", "Нужен логотип для кофейни."))
        self.assertIn("по теме заказа нет, не придумывай опыт", prompt)
        self.assertNotIn("- ", prompt.split("Описание:")[0].split("Подтверждённый")[1])

    def test_whole_knowledge_base_is_never_sent(self):
        everything = " ".join(item["title"] + item["source"] for item in profile.EXPERIENCE)
        prompt = ai_scorer._user_prompt(order("Бот", "Нужен telegram бот на aiogram, парсер данных."))
        self.assertLess(len(prompt), len(everything))
        self.assertLessEqual(prompt.count("\n- "), 3)

    def test_contacts_are_still_masked(self):
        prompt = ai_scorer._user_prompt(order("Таблица", "Пишите на ivan@example.com или +7 912 345-67-89"))
        self.assertNotIn("ivan@example.com", prompt)
        self.assertNotIn("345-67-89", prompt)


class CatalogTest(unittest.TestCase):
    def test_catalog_has_sources_and_no_client_claims(self):
        for item in profile.EXPERIENCE:
            self.assertTrue(item["source"], item["id"])
            self.assertTrue(item["title"], item["id"])
            for bad in ("клиент", "заказчик", "%", "млн", "выручк", "увеличил", "сэконом"):
                self.assertNotIn(bad, item["title"].lower(), (item["id"], bad))

    def test_client_visibility_needs_explicit_permission(self):
        with mock.patch.dict(os.environ, {"FREELANCE_CLIENT_EXPERIENCE_IDS": ""}):
            found = profile.relevant_experience("sheets", "импорт таблиц apps script")
            self.assertTrue(found)
            self.assertFalse(any(item["client_ok"] for item in found))
        with mock.patch.dict(os.environ, {"FREELANCE_CLIENT_EXPERIENCE_IDS": "sheets_import, nope"}):
            found = profile.relevant_experience("sheets", "импорт таблиц apps script")
            self.assertEqual({item["id"] for item in found if item["client_ok"]}, {"sheets_import"})

    def test_weak_match_returns_nothing(self):
        self.assertEqual(profile.relevant_experience(None, "нужен лендинг для кофейни"), [])
        self.assertEqual(profile.relevant_experience("landing", ""), [])


if __name__ == "__main__":
    unittest.main()
