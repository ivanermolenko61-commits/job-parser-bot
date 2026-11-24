import unittest
from unittest import mock

import database
from freelance import journal as jr, pipeline
from freelance.language import UKRAINIAN_REASON, is_ukrainian
from tests.test_pipeline_cycle import CycleTestBase, make_order


class LanguageTest(unittest.TestCase):
    def test_source_text(self):
        cases = (
            ("Технічне завдання: розробка Telegram-бота «Шлях до Аллаха»", "", True),
            ("Шукаю розробника бота", "", True),
            ("Створити Telegram-бота", "", True),
            ("Треба бот для магазину", "", True),
            ("Дизайн сайту на Вордпрес", "", True),
            ("Верстка адаптивного лендингу за макетом", "", True),
            ("Бот для пошти", "", True),
            ("Можна без досвіду", "", True),
            ("Збір бази контактів", "", True),
            ("Є файли для друку", "", True),
            ("Парсер для сайту", "", True),
            ("Парсер сторінок", "", True),
            ("Переклад тексту", "", True),
            ("Помочь сайту", "", False),
            ("Перекладка кабеля", "", False),
            ("Telegram-бот", "Потрібно отримувати сповіщення про нові замовлення.", True),
            ("Разработка бота", "Шукаю виконавця. Потрібно створити обробку замовлень.", True),
            ("Нужен бот для України", "Сделать интеграцию с магазином в Києві.", False),
            ("Telegram-бот", "Нужна кнопка «Підтвердити замовлення».", False),
            ("Розробка бота", "Нужен разработчик. Требуется настроить интеграцию с заказами.", False),
            ("Google Sheets / Telegram API", "бот для CRM", False),
            ("Новый дизайн сайту", "Нужно помочь магазину с интеграцией CRM.", False),
            ("Данные для Треба", "", False),
        )
        for title, body, expected in cases:
            with self.subTest(title=title, body=body):
                self.assertEqual(is_ukrainian(title, body), expected)


class LanguageDeliveryTest(CycleTestBase):
    def test_editing_only_title_changes_language_in_both_directions(self):
        order = make_order(1, "Создать Telegram-бота")
        order.body = "Telegram API"
        pipeline._intake([order], 0, set(), live=True)
        database.freelance_live_score_result(database.freelance_get(1), fit=9, ai_json='{"fit":9}')
        order.title = "Створити Telegram-бота"
        pipeline._intake([order], 0, set(), live=True)
        self.assertIsNone(database.freelance_claim_delivery(1))
        self.assertEqual(database.freelance_get(1)["reject_reason"], UKRAINIAN_REASON)
        order.title = "Сделать Telegram-бота"
        pipeline._intake([order], 0, set(), live=True)
        row = database.freelance_get(1)
        self.assertEqual((row["title"], row["tech"]), (order.title, jr.QUEUED))

    def test_intake_and_preexisting_scoring_queue(self):
        order = make_order(1, "Розробка Telegram-бота")
        order.body = "Потрібен бот для замовлень."
        pipeline._intake([order], 0, set(), live=True)
        self.assertEqual(database.freelance_get(1)["reject_reason"], UKRAINIAN_REASON)
        database._update_states(1, tech=jr.QUEUED, reject_reason=None)
        with mock.patch.object(pipeline.ai_scorer, "score_order") as score:
            pipeline.score_live(database.freelance_get(1))
        score.assert_not_called()
        self.assertEqual(database.freelance_get(1)["tech"], jr.FILTERED)

    def test_preexisting_ready_card_not_sent_but_manual_answer_allowed(self):
        pipeline._intake([make_order(1), make_order(2)], 0, set(), live=True)
        for oid, origin in [(1, "alot"), (2, "manual")]:
            database._update_states(oid, title="Telegram bot", snippet="Шукаю розробника",
                                    origin=origin, tech=jr.ASSESSED, verdict=jr.V_OK,
                                    delivery=jr.D_PENDING, ai_json='{"fit":9}', fit=9)
        self.assertIsNone(database.freelance_claim_delivery(1))
        row = database.freelance_get(1)
        self.assertEqual((row["tech"], row["delivery"], row["reject_reason"]),
                         (jr.FILTERED, jr.D_NONE, UKRAINIAN_REASON))
        self.assertIsNotNone(database.freelance_claim_delivery(2))
        with database._connect() as conn:
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM freelance_delivery_attempts WHERE order_id=1").fetchone()[0], 0)

    def test_already_delivered_excluded_before_daily_limit(self):
        pipeline._intake([make_order(1), make_order(2)], 0, set(), live=True)
        now = database._now_sql()
        for oid, title, fit in [(1, "Потрібно створити бот", 10), (2, "Нужен бот", 8)]:
            database._update_states(oid, title=title, snippet="", fit=fit, tech=jr.ASSESSED,
                                    verdict=jr.V_OK, delivery=jr.D_CARD, delivered_at=now)
        _, offers = database.freelance_claim_offers(limit=1, fresh_only=True)
        self.assertEqual([r["order_id"] for r in offers], [2])
        self.assertEqual([r["order_id"] for r in database.freelance_unprocessed(limit=1)], [2])
        self.assertEqual(database.freelance_get(1)["delivery"], jr.D_CARD)
