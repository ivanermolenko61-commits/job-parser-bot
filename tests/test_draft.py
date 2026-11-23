import json
import unittest
from unittest.mock import patch

from freelance.draft import draft_messages
from tests.test_leads_bot import BotBase, CHAT, make_call, row, run
import bot
import database


class DraftTest(unittest.TestCase):
    def test_private_and_public_fields(self):
        order = {"title": "Расчёт в таблице", "ai_json": json.dumps({
            "category": "sheets", "experience": [{"client_ok": True, "title": "секрет"}],
            "risks": "личная заметка", "hours_low": 3, "hours_high": 6,
            "price_low": 9000, "price_high": 15000, "questions": ["приватный текст"],
        })}
        with patch.dict("os.environ", {"FREELANCE_CLIENT_EXPERIENCE_IDS": ""}):
            public, private = draft_messages(order)
        self.assertIn(order["title"], public)
        self.assertNotIn("опыта", public)
        for secret in ("секрет", "личная заметка", "9000", "приватный текст"):
            self.assertNotIn(secret, public)
        for fact in ("3–6 ч", "9000–15000 ₽", "личная заметка", "проверить"):
            self.assertIn(fact, private)

    def test_current_allowlist_only_and_old_rows(self):
        order = {"title": "Telegram бот", "ai_json": '{"category":"tg_bot"}'}
        with patch.dict("os.environ", {"FREELANCE_CLIENT_EXPERIENCE_IDS": "tg_bots"}):
            self.assertIn("Telegram-боты на Python", draft_messages(order)[0])
        for broken in ("{", "null", "[]"):
            self.assertIn("нужно уточнить", draft_messages({"ai_json": broken})[1])


class DraftDeliveryTest(BotBase):
    def test_after_card_cleanup_reads_db_and_preserves_stage(self):
        database.freelance_mark_card_deleted(1)
        before = row(1)["lead"]
        call = make_call("fl_draft:1")
        with patch.object(bot, "MY_CHAT_ID", CHAT):
            run(bot.cb_fl_draft(call))
        self.assertEqual(call.message.answer.await_count, 2)
        self.assertIn("Только для Ивана", call.message.answer.await_args_list[0].args[0])
        self.assertIn("Заказ 1", call.message.answer.await_args_list[1].args[0])
        self.assertEqual(before, row(1)["lead"])
