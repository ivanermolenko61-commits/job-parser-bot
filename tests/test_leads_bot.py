"""Ф08, единица leadui: кнопки и команды лидов в bot.py, напоминания и подборка. Telegram подменён
(AsyncMock), БД во временном файле, сети нет.

Запуск из корня проекта: python -m unittest tests.test_leads_bot -v
"""
import asyncio
import os
import re
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from aiogram.types import CallbackQuery

os.environ.setdefault("BOT_TOKEN", "123456:TEST")

import bot  # noqa: E402
import database  # noqa: E402
from freelance import deal_wait, journal as jr, leads, pipeline  # noqa: E402
from tests.test_pipeline_cycle import CycleTestBase  # noqa: E402

CHAT = 777


def run(coro):
    return asyncio.run(coro)


def save(oid, fit=8):
    database.freelance_save(oid, "flru", f"Заказ {oid}", f"https://x/{oid}", "5000 ₽", "2026-11-16T10:00:00",
                            f"k{oid}", fit, "{}", "sent", 0, 100 + oid)


def row(oid):
    return database.freelance_get(oid)


def make_call(data, markup=None):
    c = mock.MagicMock()
    c.__class__ = CallbackQuery   # isinstance(target, CallbackQuery) в bot._show_lead_card
    c.data = data
    c.answer = mock.AsyncMock()
    c.message.chat.id = CHAT
    c.message.reply_markup = markup
    c.message.edit_text = mock.AsyncMock()
    c.message.edit_reply_markup = mock.AsyncMock()
    c.message.answer = mock.AsyncMock(return_value=mock.MagicMock(message_id=555))
    c.message.delete = mock.AsyncMock()
    return c


def make_message(text):
    m = mock.MagicMock()
    m.text = text
    m.chat.id = CHAT
    m.answer = mock.AsyncMock(return_value=mock.MagicMock(message_id=556))
    return m


def sent_buttons(call):
    kb = call.message.edit_text.await_args.kwargs["reply_markup"]
    return [b.callback_data for r in kb.inline_keyboard for b in r if b.callback_data]


class BotBase(CycleTestBase):
    def setUp(self):
        super().setUp()
        for oid in (1, 2, 3):
            save(oid)
        bot._pending_deals.clear()
        self.addCleanup(bot._pending_deals.clear)


class OrderCardButtonsTest(BotBase):
    def test_save_and_apply_redraw_card_and_set_stage(self):
        c = make_call("fl_save:1")
        run(bot.cb_fl_save(c))
        self.assertEqual(row(1)["lead"], jr.LEAD_SAVED)
        self.assertIn("fl_lead:1", sent_buttons(c))
        c.answer.assert_awaited_once_with("Сохранено во «Входящих»")        # одно нажатие - один ответ
        c = make_call("fl_apply:1")
        run(bot.cb_fl_apply(c))
        self.assertEqual(row(1)["lead"], jr.LEAD_APPLIED)
        self.assertEqual(row(1)["next_action"], "проверить ответ")
        self.assertEqual(len(database.freelance_lead_events(1, 10)), 2)

    def test_missing_order_does_not_crash(self):
        c = make_call("fl_save:999")
        run(bot.cb_fl_save(c))
        c.answer.assert_awaited_once()
        self.assertTrue(c.answer.await_args.kwargs.get("show_alert"))
        c = make_call("fl_save:abc")
        run(bot.cb_fl_save(c))
        c.answer.assert_awaited_once_with("Некорректная кнопка")

    def test_not_fit_collapses_card_and_return_restores_it(self):
        c = make_call("fl_np:1")
        run(bot.cb_fl_not_mine(c))
        self.assertEqual((row(1)["lead"], row(1)["lead_reason"]), (jr.LEAD_LOST, jr.LOST_LEGACY))
        c.message.delete.assert_not_awaited()                                # не удаляется, а сворачивается
        self.assertEqual(c.message.edit_text.await_args.args[0], "❌ Не подходит: Заказ 1")
        self.assertEqual(sent_buttons(c), ["fl_back:1"])
        c = make_call("fl_back:1")
        run(bot.cb_fl_back(c))
        self.assertIsNone(row(1)["lead"])
        self.assertIn("fl_save:1", sent_buttons(c))                          # карточка заказа вернулась
        run(bot.cb_fl_back(make_call("fl_back:1")))                          # повторное «Вернуть» ничего не ломает
        self.assertIsNone(row(1)["lead"])
        self.assertEqual(len(database.freelance_lead_events(1, 10)), 2)

    def test_return_does_not_undo_a_later_action(self):
        run(bot.cb_fl_not_mine(make_call("fl_np:1")))
        leads.set_stage(1, jr.LEAD_APPLIED)                                  # решение сменилось из карточки лида
        run(bot.cb_fl_back(make_call("fl_back:1")))
        self.assertEqual(row(1)["lead"], jr.LEAD_APPLIED)


class LeadCardHandlersTest(BotBase):
    def test_lead_command_regex_and_card_from_db_only(self):
        pattern = re.compile(r"^/lead_(\d+)(@\w+)?\s*$")
        self.assertTrue(pattern.match("/lead_12@mybot"))
        self.assertFalse(pattern.match("/lead_12 abc"))
        leads.set_stage(1, jr.LEAD_APPLIED)
        database.freelance_mark_card_deleted(1)
        m = make_message("/lead_1")
        run(bot.cmd_lead(m, pattern.match("/lead_1")))
        self.assertIn("Заказ 1", m.answer.await_args.args[0])
        run(bot.cmd_lead(m, pattern.match("/lead_404")))
        self.assertEqual(m.answer.await_args.args[0], "Заказ не найден.")

    def test_stage_buttons_and_invalid_values(self):
        run(bot.cb_fl_stage(make_call("fl_st:1:discussing")))
        self.assertEqual(row(1)["lead"], jr.LEAD_DISCUSSING)
        for bad in ("fl_st:1:lost", "fl_st:1:космос", "fl_st:x:saved", "fl_st:1"):
            c = make_call(bad)
            run(bot.cb_fl_stage(c))
            c.answer.assert_awaited_once_with("Некорректная кнопка")
        self.assertEqual(row(1)["lead"], jr.LEAD_DISCUSSING)

    def test_agreed_asks_price_once(self):
        c = make_call("fl_st:1:agreed")
        run(bot.cb_fl_stage(c))
        self.assertEqual(row(1)["lead"], jr.LEAD_AGREED)
        self.assertIn("За сколько договорились", c.message.answer.await_args.args[0])
        self.assertEqual(len(bot._pending_deals), 1)
        c = make_call("fl_st:1:agreed")
        run(bot.cb_fl_stage(c))
        c.message.answer.assert_not_awaited()

    def test_reasons_remind_and_undo_handlers(self):
        leads.set_stage(1, jr.LEAD_APPLIED)
        run(bot.cb_fl_lost_reason(make_call("fl_lr:1:1")))
        self.assertEqual((row(1)["lead"], row(1)["lead_reason"]), (jr.LEAD_LOST, jr.LOST_REASONS[1]))
        for bad in ("fl_lr:1:99", "fl_lr:1:x"):
            c = make_call(bad)
            run(bot.cb_fl_lost_reason(c))
            c.answer.assert_awaited_once_with("Некорректная кнопка")
        run(bot.cb_fl_undo(make_call("fl_undo:1")))
        self.assertEqual(row(1)["lead"], jr.LEAD_APPLIED)
        run(bot.cb_fl_remind_choice(make_call("fl_rc:1:3days")))
        self.assertIsNotNone(row(1)["remind_at"])
        c = make_call("fl_rc:1:never")
        run(bot.cb_fl_remind_choice(c))
        c.answer.assert_awaited_once_with("Некорректная кнопка")
        c = make_call("fl_undo:2")
        run(bot.cb_fl_undo(c))
        c.answer.assert_awaited_once_with("Нечего отменять")

    def test_next_command(self):
        m = make_message("/next")
        run(bot.cmd_next(m))
        self.assertIn("Формат", m.answer.await_args.args[0])
        m = make_message("/next 1 позвонить")
        run(bot.cmd_next(m))
        self.assertIn("активного лида", m.answer.await_args.args[0])
        leads.set_stage(1, jr.LEAD_SAVED)
        run(bot.cmd_next(make_message("/next 1 выслать смету")))
        self.assertEqual(row(1)["next_action"], "выслать смету")
        m = make_message("/next 404 текст")
        run(bot.cmd_next(m))
        self.assertEqual(m.answer.await_args.args[0], "Заказ не найден.")


class BackgroundJobsTest(BotBase):
    def _due(self, oid):
        leads.set_stage(oid, jr.LEAD_APPLIED)
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        database.freelance_lead_remind(oid, past)

    def test_reminder_sent_once_and_kept_on_failure(self):
        self._due(1)
        with mock.patch.object(bot, "bot") as b, mock.patch.object(bot, "MY_CHAT_ID", CHAT):
            b.send_message = mock.AsyncMock(side_effect=RuntimeError("net"))
            run(bot.send_lead_reminders())
            self.assertEqual(len(database.freelance_reminders_due()), 1)      # не отправлено - ждёт
            b.send_message = mock.AsyncMock()
            run(bot.send_lead_reminders())
            self.assertEqual(b.send_message.await_count, 1)
            self.assertEqual(b.send_message.await_args.kwargs["chat_id"], CHAT)
            run(bot.send_lead_reminders())
            self.assertEqual(b.send_message.await_count, 1)                    # повторно не уходит
        self.assertEqual(database.freelance_reminders_due(), [])

    def test_closed_lead_gets_no_reminder(self):
        self._due(1)
        leads.set_stage(1, jr.LEAD_LOST, jr.LOST_REASONS[0])
        with mock.patch.object(bot, "bot") as b:
            b.send_message = mock.AsyncMock()
            run(bot.send_lead_reminders())
            b.send_message.assert_not_awaited()

    def test_offers_sent_once_and_released_on_failure(self):
        with mock.patch.object(bot, "bot") as b, mock.patch.object(bot, "MY_CHAT_ID", CHAT), \
                mock.patch.object(bot, "_freelance_subscribed", mock.AsyncMock(return_value=True)):
            b.send_message = mock.AsyncMock(side_effect=RuntimeError("net"))
            run(bot.send_lead_offers())
            b.send_message = mock.AsyncMock()
            run(bot.send_lead_offers())
            self.assertEqual(b.send_message.await_count, 1)                    # после сбоя заказы вернулись
            run(bot.send_lead_offers())
            self.assertEqual(b.send_message.await_count, 1)                    # за день - одна подборка

    def test_offers_skipped_when_paused(self):
        with mock.patch.object(bot, "bot") as b, \
                mock.patch.object(bot, "_freelance_subscribed", mock.AsyncMock(return_value=False)):
            b.send_message = mock.AsyncMock()
            run(bot.send_lead_offers())
            b.send_message.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()

