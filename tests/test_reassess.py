"""Ф06/Ф07: повторная оценка при существенной правке описания, версии оценки, повторные уведомления, карточка
(цена отдельно от бюджета, ориентир, пометка устаревания), бюджет в БД, фактические часы.

Запуск из корня проекта: python -m unittest tests.test_reassess -v
"""
import json
import unittest
from datetime import datetime, timezone
from unittest import mock

import database
from freelance import journal as jr, pipeline, pricing
from freelance.alot_client import FreelanceOrder
from tests.test_journal import LONG_BODY, order
from tests.test_pipeline_cycle import CycleTestBase, ai_result

NEW_BODY = ("Требуется телеграм бот для записи клиентов в салон красоты с оплатой через ЮKassa, "
            "админкой для мастера и напоминаниями за сутки до визита. Сроки обсудим.")


def result(fit, hours=4.0, **extra):
    return {**ai_result(fit), "hours": hours, "components": [{"name": "бот", "hours": hours}], **extra}


class ReassessBase(CycleTestBase):
    def setUp(self):
        super().setUp()
        p = mock.patch.object(pipeline, "TRIAGE_MODE", "off")
        p.start()
        self.addCleanup(p.stop)

    def cycle(self, alot=(), score=None, last_id=100):
        database.kv_set(pipeline.LAST_ID_KEY, last_id)
        score = score if score is not None else mock.Mock(return_value=result(8))
        with mock.patch.object(pipeline, "fetch_new_orders", return_value=self.scan(list(alot))), \
                mock.patch.object(pipeline, "_load_pending", return_value={}), \
                mock.patch.object(pipeline.ai_scorer, "score_order", score):
            res = pipeline._prepare_cycle()
        return res, score

    def deliver(self, oid):
        database._update_states(oid, delivery=jr.D_CARD, delivered_at="2026-11-16 10:00:00")

    def hist(self, oid):
        return database.freelance_assessment_history(oid)


class TextChangeTest(unittest.TestCase):
    def test_small_addition_is_not_significant(self):
        sim, significant = pipeline._text_change(LONG_BODY, LONG_BODY + " Срок - до пятницы.")
        self.assertFalse(significant)
        self.assertGreater(sim, 0.8)

    def test_rewrite_is_significant(self):
        sim, significant = pipeline._text_change(LONG_BODY, NEW_BODY)
        self.assertTrue(significant)
        self.assertLess(sim, 0.8)

    def test_big_length_change_is_significant_even_with_same_words(self):
        self.assertTrue(pipeline._text_change(LONG_BODY, LONG_BODY + " " + LONG_BODY.split(",")[0] * 6)[1])

    def test_empty_old_text_is_significant(self):
        self.assertEqual(pipeline._text_change("", "Новое описание")[1], True)

    def test_same_text_is_not_significant(self):
        self.assertEqual(pipeline._text_change(LONG_BODY, LONG_BODY), (1.0, False))


class MinorEditTest(ReassessBase):
    def test_minor_edit_is_marked_but_not_reassessed(self):
        self.cycle(alot=[order(201)])
        res, score = self.cycle(alot=[order(201, body=LONG_BODY + " Срок - до пятницы.")])
        score.assert_not_called()
        row = database.freelance_get(201)
        self.assertEqual((row["desc_changed"], row["fit"]), (1, 8))
        self.assertEqual(database.freelance_reassess_state(201)["status"], "skipped")
        sc = pipeline.scored_from_row(row)
        self.assertEqual(sc.stale_note, pipeline.STALE_MINOR)
        self.assertEqual(res.reassessed, 0)


class SignificantEditTest(ReassessBase):
    def test_significant_edit_is_reassessed_once_and_history_kept(self):
        self.cycle(alot=[order(202)])
        self.assertEqual(len(self.hist(202)), 1)
        res, score = self.cycle(alot=[order(202, body=NEW_BODY)], score=mock.Mock(return_value=result(6, 6.0)))
        self.assertEqual(score.call_count, 1)
        row = database.freelance_get(202)
        self.assertEqual((row["fit"], row["desc_changed"], row["tech"]), (6, 0, "assessed"))
        ai = json.loads(row["ai_json"])
        self.assertTrue(ai["reassessed"])
        self.assertEqual(ai["hours"], 6.0)
        self.assertEqual(database.freelance_reassess_state(202)["status"], "done")
        self.assertEqual(len(self.hist(202)), 2)
        self.assertEqual(res.reassessed, 1)
        # следующие циклы с тем же текстом ничего не тратят
        _, score2 = self.cycle(alot=[order(202, body=NEW_BODY)])
        score2.assert_not_called()
        self.assertEqual(pipeline.scored_from_row(database.freelance_get(202)).stale_note, pipeline.STALE_DONE)

    def test_staleness_is_explicit_until_reassessed(self):
        self.cycle(alot=[order(203)])
        soon = mock.patch.object(pipeline.ai_scorer, "quota_exhausted",
                                 return_value=datetime(2099, 1, 1, tzinfo=timezone.utc))
        with soon:
            _, score = self.cycle(alot=[order(203, body=NEW_BODY)])
        score.assert_not_called()
        row = database.freelance_get(203)
        self.assertEqual((row["desc_changed"], row["fit"]), (1, 8))             # оценка прежняя
        sc = pipeline.scored_from_row(row)
        self.assertEqual(sc.stale_note, pipeline.STALE_PENDING)
        card = pipeline.format_order(sc, detailed=True)
        self.assertIn("оценка относится к прежнему тексту", card)
        _, score2 = self.cycle(alot=[], score=mock.Mock(return_value=result(7)))   # квота вернулась
        self.assertEqual(score2.call_count, 1)
        self.assertIn("оценка обновлена после правки описания",
                      pipeline.format_order(pipeline.scored_from_row(database.freelance_get(203))))

    def test_reverting_text_reuses_stored_version_without_ai_call(self):
        original = order(204)
        self.cycle(alot=[original])
        self.cycle(alot=[order(204, body=NEW_BODY)], score=mock.Mock(return_value=result(5)))
        self.assertEqual(database.freelance_get(204)["fit"], 5)
        _, score = self.cycle(alot=[original])             # описание вернули к первой версии
        score.assert_not_called()
        row = database.freelance_get(204)
        self.assertEqual((row["fit"], row["desc_changed"]), (8, 0))
        self.assertEqual(len(self.hist(204)), 2)           # новых версий не появилось

    def test_low_quota_blocks_reassessment_without_losing_it(self):
        self.cycle(alot=[order(205)])
        with mock.patch.object(pipeline.ai_scorer, "daily_capacity", return_value=(10, 100)):
            _, score = self.cycle(alot=[order(205, body=NEW_BODY)])
        score.assert_not_called()
        self.assertEqual(database.freelance_reassess_state(205)["status"], "pending")
        _, score2 = self.cycle(alot=[])
        self.assertEqual(score2.call_count, 1)

    def test_failed_reassessment_keeps_old_score_and_stops_after_three_attempts(self):
        self.cycle(alot=[order(206)])
        failing = mock.Mock(return_value=None)
        self.cycle(alot=[order(206, body=NEW_BODY)], score=failing)
        for _ in range(4):
            self.cycle(alot=[], score=failing)
        row = database.freelance_get(206)
        self.assertEqual((row["fit"], row["desc_changed"]), (8, 1))
        self.assertEqual(failing.call_count, pipeline.REASSESS_MAX_ATTEMPTS)
        self.assertEqual(database.freelance_reassess_state(206)["attempts"], pipeline.REASSESS_MAX_ATTEMPTS)

    def test_exception_in_reassessment_does_not_break_cycle(self):
        self.cycle(alot=[order(207)])
        boom = mock.Mock(side_effect=RuntimeError("сбой"))
        res, _ = self.cycle(alot=[order(207, body=NEW_BODY)], score=boom)
        self.assertEqual(res.reassessed, 0)
        self.assertEqual(database.freelance_get(207)["fit"], 8)
        for _ in range(4):
            self.cycle(alot=[], score=boom)
        self.assertEqual(boom.call_count, pipeline.REASSESS_MAX_ATTEMPTS)    # исключение - тоже неудачная попытка
        self.assertEqual(database.freelance_reassess_state(207)["attempts"], pipeline.REASSESS_MAX_ATTEMPTS)
        self.assertEqual(database.freelance_get(207)["fit"], 8)

    def test_exception_of_one_order_does_not_block_next(self):
        titles = {214: "Выгрузка заказов из Wildberries", 215: "Отчёт по остаткам в Google Таблице"}
        for oid, title in titles.items():
            self.cycle(alot=[order(oid, title=title)])
        down = mock.patch.object(pipeline.ai_scorer, "quota_exhausted",
                                 return_value=datetime(2099, 1, 1, tzinfo=timezone.utc))
        with down:
            self.cycle(alot=[order(oid, title=t, body=NEW_BODY + f" {oid}") for oid, t in titles.items()])
        score = mock.Mock(side_effect=[RuntimeError("сбой"), result(6)])
        res, _ = self.cycle(alot=[], score=score)
        self.assertEqual((score.call_count, res.reassessed), (2, 1))
        self.assertEqual(database.freelance_reassess_state(214)["attempts"], 1)

    def test_quota_is_rechecked_before_each_order(self):
        titles = {216: "Выгрузка заказов из Wildberries", 217: "Отчёт по остаткам в Google Таблице"}
        for oid, title in titles.items():
            self.cycle(alot=[order(oid, title=title)])
        down = mock.patch.object(pipeline.ai_scorer, "quota_exhausted",
                                 return_value=datetime(2099, 1, 1, tzinfo=timezone.utc))
        with down:
            self.cycle(alot=[order(oid, title=t, body=NEW_BODY + f" {oid}") for oid, t in titles.items()])
        spent = {"v": False}

        def one_call(_order):
            spent["v"] = True
            return result(6)
        score = mock.Mock(side_effect=one_call)
        with mock.patch.object(pipeline.ai_scorer, "quota_exhausted",
                               side_effect=lambda: datetime(2099, 1, 1, tzinfo=timezone.utc) if spent["v"] else None):
            self.cycle(alot=[], score=score)
        self.assertEqual(score.call_count, 1)
        pending = [database.freelance_reassess_state(o)["status"] for o in titles]
        self.assertEqual(sorted(pending), ["done", "pending"])

    def test_minor_edit_after_pending_significant_edit_still_reassessed(self):
        self.cycle(alot=[order(218)])
        down = mock.patch.object(pipeline.ai_scorer, "quota_exhausted",
                                 return_value=datetime(2099, 1, 1, tzinfo=timezone.utc))
        with down:
            self.cycle(alot=[order(218, body=NEW_BODY)])
            self.cycle(alot=[order(218, body=NEW_BODY + " Спасибо.")])      # B -> C: мелочь
        state = database.freelance_reassess_state(218)
        self.assertEqual((state["status"], state["significant"], state["old_fit"]), ("pending", 1, 8))
        res, score = self.cycle(alot=[])
        self.assertEqual((score.call_count, res.reassessed), (1, 1))

    def test_per_cycle_limit(self):
        titles = {211: "Выгрузка заказов из Wildberries", 212: "Отчёт по остаткам в Google Таблице",
                  213: "Синхронизация Ozon с таблицей"}      # разные названия: иначе заказы склеились бы как дубли
        for oid, title in titles.items():
            self.cycle(alot=[order(oid, title=title)])
        edited = [order(oid, title=title, body=NEW_BODY + f" {oid}") for oid, title in titles.items()]
        with mock.patch.object(pipeline.ai_scorer, "quota_exhausted",
                               return_value=datetime(2099, 1, 1, tzinfo=timezone.utc)):
            self.cycle(alot=edited)
        _, score = self.cycle(alot=[])
        self.assertEqual(score.call_count, pipeline.REASSESS_PER_CYCLE)

    def test_second_edit_before_reassessment_keeps_prior_visible_score(self):
        self.cycle(alot=[order(208)])
        down = mock.patch.object(pipeline.ai_scorer, "quota_exhausted",
                                 return_value=datetime(2099, 1, 1, tzinfo=timezone.utc))
        with down:
            self.cycle(alot=[order(208, body=NEW_BODY)])
            self.cycle(alot=[order(208, body=NEW_BODY + " Ещё одна правка и новые подробности по интеграции.")])
        state = database.freelance_reassess_state(208)
        self.assertEqual((state["old_fit"], state["status"]), (8, "pending"))


class NoticeTest(ReassessBase):
    def setUp(self):
        super().setUp()
        p = mock.patch.object(pipeline, "FREELANCE_MIN_FIT", 6)
        p.start()
        self.addCleanup(p.stop)

    def edited_cycle(self, oid, new, body=NEW_BODY):
        return self.cycle(alot=[order(oid, body=body)], score=mock.Mock(return_value=new))

    def test_delivered_order_crossing_threshold_is_notified_once(self):
        self.cycle(alot=[order(301)], score=mock.Mock(return_value=result(5)))
        self.deliver(301)
        res, _ = self.edited_cycle(301, result(8))
        self.assertEqual(len(res.reassess_notes), 1)
        self.assertIn("было 5/10 → стало 8/10", res.reassess_notes[0])
        self.assertIn("Описание заказа изменилось", res.reassess_notes[0])
        self.assertFalse(database.freelance_claim_assessment_notice(301, database.freelance_get(301)["content_hash"]))
        res2, _ = self.cycle(alot=[order(301, body=NEW_BODY)])
        self.assertEqual(res2.reassess_notes, [])

    def test_small_change_updates_silently(self):
        self.cycle(alot=[order(302)], score=mock.Mock(return_value=result(8)))
        self.deliver(302)
        res, _ = self.edited_cycle(302, result(9))
        self.assertEqual(res.reassess_notes, [])
        self.assertEqual(database.freelance_get(302)["fit"], 9)

    def test_fit_delta_notifies(self):
        self.cycle(alot=[order(303)], score=mock.Mock(return_value=result(9)))
        self.deliver(303)
        res, _ = self.edited_cycle(303, result(7))
        self.assertEqual(len(res.reassess_notes), 1)

    def test_price_change_notifies(self):
        self.cycle(alot=[order(304)], score=mock.Mock(return_value=result(8, hours=4)))
        self.deliver(304)
        res, _ = self.edited_cycle(304, result(8, hours=12))
        self.assertEqual(len(res.reassess_notes), 1)
        self.assertIn("Цена полного объёма", res.reassess_notes[0])

    def test_undelivered_order_gets_first_delivery_not_a_second_notice(self):
        self.cycle(alot=[order(305)], score=mock.Mock(return_value=result(4)))
        before = database.freelance_get(305)
        self.assertEqual((before["delivery"], before["verdict"]), ("none", "low_fit"))
        res, _ = self.edited_cycle(305, result(8))
        row = database.freelance_get(305)
        self.assertEqual((row["delivery"], row["verdict"], row["fit"]), ("pending", "ok", 8))
        self.assertEqual(res.reassess_notes, [])

    def test_undelivered_good_order_turned_bad_is_not_delivered(self):
        self.cycle(alot=[order(306)], score=mock.Mock(return_value=result(8)))
        self.assertEqual(database.freelance_get(306)["delivery"], "pending")
        self.edited_cycle(306, result(3))
        row = database.freelance_get(306)
        self.assertEqual((row["delivery"], row["verdict"]), ("none", "low_fit"))

    def test_delivery_state_of_delivered_order_is_never_rolled_back(self):
        self.cycle(alot=[order(307)], score=mock.Mock(return_value=result(8)))
        self.deliver(307)
        self.edited_cycle(307, result(2))
        self.assertEqual(database.freelance_get(307)["delivery"], "card")

    def test_notice_rules(self):
        needed = pipeline._notice_needed
        self.assertTrue(needed(5, 4000, 6, 4000))      # порог пересечён вверх
        self.assertFalse(needed(6, 4000, 5, 4000))     # вниз на 1 - молча
        self.assertTrue(needed(8, 4000, 6, 4000))      # на 2
        self.assertTrue(needed(8, 4000, 8, 5400))      # цена +35%
        self.assertFalse(needed(8, 4000, 8, 4800))     # цена +20%
        self.assertFalse(needed(None, 4000, 9, 9000))  # прежней оценки нет


class BudgetInDbTest(ReassessBase):
    def test_only_comparable_ruble_budgets_are_stored(self):
        cases = {401: ("до 5 000 ₽", 5000.0), 402: ("100 $", None), 403: ("5000", None),
                 404: ("500 ₽/час", None), 405: ("по договорённости", None), 406: ("500010000 ₽", None)}
        orders = []
        for oid, (text, _) in cases.items():
            o = order(oid)
            o.price_text = text
            orders.append(o)
        self.cycle(alot=orders)
        for oid, (text, expected) in cases.items():
            self.assertEqual(database.freelance_get(oid)["budget"], expected, text)


class CardTest(unittest.TestCase):
    def sc(self, price_text, ai_over=None, hours=10.0, **ai_extra):
        o = FreelanceOrder(1, "flru", "Бот для записи", "", price_text, 0.0, [],
                           datetime(2026, 11, 16, 9, 0, tzinfo=timezone.utc), "https://x/1")
        ai = {**ai_result(8), "hours": hours, "category": "tg_bot", **ai_extra}
        s = pipeline.ScoredOrder(o, ai, "k")
        with mock.patch("database.market_stats", return_value=None), mock.patch("database.freelance_deals", return_value=[]):
            pipeline._ensure_price(s)
        return s

    def test_card_price_is_independent_of_budget_text(self):
        prices = {t: self.sc(t).ai["price_mid"] for t in ("1000 ₽", "99 000 ₽", "", "100 $")}
        self.assertEqual(len(set(prices.values())), 1, prices)

    def test_low_budget_is_a_mismatch_note_not_a_lower_price(self):
        card = pipeline.format_order(self.sc("1000 ₽"), detailed=True)
        full = self.sc("").ai["price_mid"]
        self.assertIn(f"Расчёт полного объёма: {pipeline._money(full)} ₽", card)
        brief = pipeline.format_order(self.sc("1000 ₽"))
        self.assertIn("Уточнить объём перед откликом", brief)
        self.assertNotIn("Предложить:", brief)
        self.assertNotIn("Ориентир:", brief)
        self.assertIn("диагностику или меньший объём", card)
        self.assertIn("минимально приемлемая цена", card)

    def test_vague_spec_shows_estimate_with_conditions(self):
        s = self.sc("", clarity="размыто", unknowns=["доступ к API", "макеты"])
        short = pipeline.format_order(s)
        detailed = pipeline.format_order(s, detailed=True)
        self.assertIn("Ориентир", short)
        self.assertNotIn("Предложить:", short)
        self.assertIn("условия оценки: доступ к API; макеты", detailed)

    def test_foreign_currency_without_rate_is_not_compared(self):
        card = pipeline.format_order(self.sc("500 $"), detailed=True)
        self.assertIn("нет надёжного курса", card)
        self.assertNotIn("бюджет ниже", card)

    def test_foreign_currency_with_rate_shows_source_and_date(self):
        rates = {"USD": {"rate": 90.0, "source": "ЦБ РФ", "date": "2026-11-15"}}
        with mock.patch.object(pricing, "fx") as fake:
            fake.load_rates.return_value = rates
            fake.to_rub.side_effect = lambda a, c, r, t=None: {"rub": a * 90, "rate": 90.0, "source": "ЦБ РФ",
                                                               "date": "2026-11-15"} if c == "USD" else None
            card = pipeline.format_order(self.sc("100 $"), detailed=True)
        self.assertIn("ЦБ РФ", card)
        self.assertIn("2026-11-15", card)

    def test_unknown_currency_is_not_rubles(self):
        self.assertIn("валюта неизвестна", pipeline.format_order(self.sc("500"), detailed=True))

    def test_detailed_card_has_separate_dimensions(self):
        s = self.sc("", category="other", feasibility="высокая", urgency="срочно", profile_reason="бот с оплатой",
                    sufficiency="частично", confidence="средняя", clarity="частично")
        card = pipeline.format_order(s, detailed=True)
        for fragment in ("Профиль: бот с оплатой", "выполнимость: высокая", "ясность ТЗ: частично",
                         "данных: частично", "уверенность: средняя", "срок: срочно", "не календарный срок"):
            self.assertIn(fragment, card)
        self.assertIn("Подтверждённого опыта по теме нет", card)
        self.assertLessEqual(len(card), pipeline.MAX_MESSAGE_LEN)

    def test_old_record_keeps_old_text(self):
        o = FreelanceOrder(2, "flru", "Старый", "", "5000 ₽", 5000.0, [],
                           datetime(2026, 11, 16, 9, 0, tzinfo=timezone.utc), "https://x/2")
        ai = {**ai_result(8), "price_mid": 5200, "price_low": 4400, "price_high": 6000, "price_v": 3,
              "budget": 5000, "budget_capped": True, "price_own": 6000, "price_basis": "4.5 ч × 1000 ₽ × 1.15"}
        card = pipeline.format_order(pipeline.ScoredOrder(o, ai, "k"), detailed=True)
        self.assertIn("упёрлось в бюджет", card)
        self.assertNotIn("минимально приемлемая", card)

    def test_questions_up_to_three_in_detailed_one_in_short(self):
        s = self.sc("", questions=["Один?", "Два?", "Три?", "Четыре?"])
        self.assertIn("Один?", pipeline.format_order(s))
        self.assertNotIn("Два?", pipeline.format_order(s))
        detailed = pipeline.format_order(s, detailed=True)
        self.assertIn("Три?", detailed)
        self.assertNotIn("Четыре?", detailed)


class ReassessNoteTest(unittest.TestCase):
    def test_note_escapes_html_and_has_link(self):
        note = pipeline.format_reassess_note({"title": "<b>Бот</b> & таблицы", "url": "https://x/1"}, 5, 4000,
                                             {"fit": 8, "price_mid": 6000, "summary": "суть <i>заказа</i>"})
        self.assertNotIn("<i>", note)
        self.assertIn("&lt;b&gt;Бот&lt;/b&gt;", note)
        self.assertIn("4 000 → 6 000 ₽", note)
        self.assertIn("https://x/1", note)


if __name__ == "__main__":
    unittest.main()
