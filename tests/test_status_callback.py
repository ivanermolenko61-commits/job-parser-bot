"""The freelance status button must deliver the full daily report in Telegram-sized messages."""
import asyncio
import os
from unittest.mock import AsyncMock, Mock
from xml.etree.ElementTree import fromstring

os.environ.setdefault("BOT_TOKEN", "123456:TEST")

import bot
import database
from tests.test_pipeline_cycle import CycleTestBase


class StatusCallbackTest(CycleTestBase):
    def test_daily_report_with_multiple_sites_reaches_chat(self):
        sources = (
            ("flru", "fl"), ("flru", "alot"), ("kworkru", "kwork"), ("kworkru", "alot"),
            ("freelanceru", "freelanceru"), ("freelanceru", "alot"), ("freelancehunt", "alot"),
            ("weblancer", "alot"), ("youdoru", "alot"), ("workzilla", "alot"),
        )
        with database._connect() as conn:
            conn.executemany(
                "INSERT INTO freelance_orders "
                "(order_id, site, origin, tech, delivery, verdict, fit, assessed_at, delivered_at) "
                "VALUES (?, ?, ?, 'assessed', 'card', 'ok', 9, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                ((i, site, origin) for i, (site, origin) in enumerate(sources, 1)),
            )
        call = Mock()
        call.answer = AsyncMock()
        call.message.answer = AsyncMock()
        asyncio.run(bot.cb_fl_status(call))

        call.answer.assert_awaited_once()
        messages = []
        for sent in call.message.answer.await_args_list:
            self.assertEqual(sent.kwargs["parse_mode"], "HTML")
            message = "".join(fromstring(f"<root>{sent.args[0]}</root>").itertext())
            self.assertTrue(message)
            self.assertLessEqual(len(message), 4096)
            messages.append(message)
        self.assertGreater(len(messages), 1)
        report = "\n".join(messages)
        for site, origin in sources:
            self.assertIn(f"{site}/{origin}", report)
