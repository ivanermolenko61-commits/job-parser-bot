"""Переключатели источников: хранение, цикл с выключенными источниками, подсказка про alot, кнопки бота.

Запуск из корня проекта: python -m unittest tests.test_sources -v
"""
import asyncio
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import database
from freelance import fl_client, kwork_client, pipeline, sources
from freelance.alot_client import ScanResult
from tests.test_pipeline_cycle import CycleTestBase, make_order

REAL_FEED = pipeline._fetch_feed_orders   # до подмены в CycleTestBase


class SourcesStateTest(CycleTestBase):
    def test_all_enabled_by_default(self):
        self.assertTrue(all(sources.is_enabled(n) for n in sources.ALL_SOURCES))

    def test_toggle_persists_in_db(self):
        self.assertTrue(sources.set_enabled(sources.FLRU, False))
        self.assertFalse(sources.is_enabled(sources.FLRU))
        self.assertTrue(sources.is_enabled(sources.KWORK))
        self.assertEqual(database.kv_get("freelance_sources_off"), "flru")
        self.assertTrue(sources.set_enabled(sources.KWORK, False))
        self.assertEqual(database.kv_get("freelance_sources_off"), "flru,kwork")
        self.assertTrue(sources.set_enabled(sources.FLRU, True))
        self.assertEqual(database.kv_get("freelance_sources_off"), "kwork")

    def test_unknown_source_rejected(self):
        self.assertFalse(sources.set_enabled("nope", False))

    def test_garbage_in_db_ignored(self):
        database.kv_set("freelance_sources_off", "x,,alot,y")
        self.assertFalse(sources.is_enabled(sources.ALOT))
        self.assertTrue(sources.is_enabled(sources.FLRU))

    def test_env_forced_off_cannot_be_enabled(self):
        with mock.patch.dict(sources._ENV_ON, {sources.KWORK: False}):
            self.assertFalse(sources.is_enabled(sources.KWORK))
            self.assertTrue(sources.forced_off(sources.KWORK))
            self.assertFalse(sources.set_enabled(sources.KWORK, True))
            self.assertEqual([s for s in sources.states() if s[0] == "kwork"],
                             [("kwork", "Kwork", False, True)])

    def test_db_read_failure_keeps_source_on(self):
        with mock.patch.object(sources, "kv_get", side_effect=OSError("db")):
            self.assertTrue(sources.is_enabled(sources.ALOT))

    def test_source_of(self):
        self.assertEqual(sources.source_of(17360853), sources.ALOT)
        self.assertEqual(sources.source_of(sources.FL_ID_OFFSET + 5), sources.FLRU)
        self.assertEqual(sources.source_of(sources.KWORK_ID_OFFSET + 5), sources.KWORK)
        self.assertEqual(sources.source_of(sources.KWORK_ID_OFFSET - 1), sources.FLRU)

    def test_feed_id_ranges(self):
        self.assertFalse(sources.is_feed_id(17360853))
        self.assertTrue(sources.is_feed_id(sources.FL_ID_OFFSET + 1))
        self.assertTrue(sources.is_feed_id(sources.KWORK_ID_OFFSET + 1))
        self.assertTrue(fl_client.is_fl_id(sources.FL_ID_OFFSET + 1))
        self.assertFalse(fl_client.is_fl_id(sources.KWORK_ID_OFFSET + 1))
        self.assertTrue(kwork_client.is_kwork_id(sources.KWORK_ID_OFFSET + 1))


class CycleWithSwitchesTest(CycleTestBase):
    def run_cycle(self, feed=(), alot_orders=(), alot_error=None):
        fetch = (mock.patch.object(pipeline, "fetch_new_orders", side_effect=alot_error) if alot_error
                 else mock.patch.object(pipeline, "fetch_new_orders",
                                        return_value=self.scan(list(alot_orders))))
        with mock.patch.object(pipeline, "find_upper_bound", return_value=1100), fetch as scan, \
                mock.patch.object(pipeline, "_fetch_feed_orders", return_value=list(feed)), \
                mock.patch.object(pipeline, "_load_pending", return_value={}) as pend:
            res = pipeline._prepare_cycle()
        return res, scan, pend

    def kwork_order(self, pid=9001):
        when = datetime.now(timezone.utc) - timedelta(minutes=5)
        w = {"id": pid, "name": "Парсер сайта на Python", "description": "нужен парсер",
             "category_id": "38", "priceLimit": "5000.00", "status": "active",
             "date_create": (when + timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S")}
        return kwork_client._parse_want(w)

    def test_alot_disabled_is_not_called(self):
        sources.set_enabled(sources.ALOT, False)
        order = self.kwork_order()
        res, scan, pend = self.run_cycle(feed=[order])
        scan.assert_not_called()
        self.assertEqual(res.error, "")
        self.assertEqual(res.new_last_id, 0)          # курсор alot не трогаем
        self.assertFalse(pend.call_args.kwargs["fetch_alot"])
        ids = {sc.order.order_id for sc in res.to_send + res.unscored + res.rejected}
        self.assertIn(order.order_id, ids)

    def test_disabled_alot_resets_health_streak(self):
        sources.set_enabled(sources.ALOT, False)
        with mock.patch.object(pipeline.monitor, "reset_streak") as reset:
            self.run_cycle(feed=[self.kwork_order()])
        reset.assert_any_call("alot")

    def test_all_sources_off_is_error(self):
        for n in sources.ALL_SOURCES:
            sources.set_enabled(n, False)
        res, scan, pend = self.run_cycle(feed=[])
        scan.assert_not_called()
        self.assertIn("нет включённых источников", res.error)
        pend.assert_not_called()

    def test_alot_off_empty_feed_still_processes_pending(self):
        # лента включённых FL.ru/Kwork пуста - это не ошибка: pending из БД дообрабатываются
        sources.set_enabled(sources.ALOT, False)
        res, scan, pend = self.run_cycle(feed=[])
        scan.assert_not_called()
        self.assertEqual(res.error, "")
        pend.assert_called_once()

    def test_alot_down_empty_feed_still_processes_pending(self):
        res, _, pend = self.run_cycle(feed=[], alot_error=RuntimeError("down"))
        self.assertEqual(res.error, "")
        pend.assert_called_once()

    def test_alot_down_and_no_feed_sources_reports_alot_error(self):
        sources.set_enabled(sources.FLRU, False)
        sources.set_enabled(sources.KWORK, False)
        res, _, pend = self.run_cycle(feed=[], alot_error=RuntimeError("alot down"))
        self.assertIn("alot down", res.error)
        pend.assert_not_called()

    def test_alot_down_does_not_fetch_alot_pending(self):
        res, _, pend = self.run_cycle(feed=[self.kwork_order()], alot_error=RuntimeError("down"))
        self.assertEqual(res.error, "")
        self.assertFalse(pend.call_args.kwargs["fetch_alot"])

    def test_alot_enabled_fetches_alot_pending(self):
        _, _, pend = self.run_cycle(alot_orders=[make_order(1100)])
        self.assertTrue(pend.call_args.kwargs["fetch_alot"])

    def test_disabled_feed_sources_not_polled(self):
        sources.set_enabled(sources.FLRU, False)
        sources.set_enabled(sources.KWORK, False)
        with mock.patch.object(pipeline, "fetch_fl_orders") as fl, \
                mock.patch.object(pipeline, "fetch_kwork_orders") as kw:
            self.assertEqual(REAL_FEED(), [])
        fl.assert_not_called()
        kw.assert_not_called()

    def test_kwork_failure_does_not_break_fl(self):
        fl = [make_order(5)]
        with mock.patch.object(pipeline, "fetch_fl_orders", return_value=fl), \
                mock.patch.object(pipeline, "fetch_kwork_orders", side_effect=RuntimeError("captcha")):
            got = REAL_FEED()
        self.assertEqual(got, fl)
        pipeline.monitor.record.assert_any_call("kwork", 0, mock.ANY, label="Источник фриланс-заказов Kwork")

    def test_pending_alot_not_fetched_when_fetch_alot_false(self):
        oid = 1234
        now = datetime.now(timezone.utc)
        row = {"order_id": oid, "published_at": now.isoformat(), "ai_json": None}
        with mock.patch.object(pipeline, "freelance_pending", return_value=[row]), \
                mock.patch.object(pipeline, "_fetch_items") as items:
            self.assertEqual(pipeline._load_pending(now - timedelta(hours=6), set(), [], fetch_alot=False), {})
        items.assert_not_called()

    def test_pending_of_disabled_source_not_restored(self):
        now = datetime.now(timezone.utc)
        fl = sources.FL_ID_OFFSET + 70
        kw = sources.KWORK_ID_OFFSET + 71
        for oid, site in ((fl, "flru"), (kw, "kworkru")):
            database.freelance_save(oid, site, "Парсер", f"https://x/{oid}", "", (now - timedelta(hours=1)).isoformat(),
                                    "dk" + site, None, "", "pending")
        sources.set_enabled(sources.KWORK, False)
        res = pipeline._load_pending(now - timedelta(hours=6), set(), [], fetch_alot=False)
        self.assertEqual(list(res), [fl])                    # Kwork выключен - его заказ не воскресает
        sources.set_enabled(sources.FLRU, False)
        self.assertEqual(pipeline._load_pending(now - timedelta(hours=6), set(), [], fetch_alot=False), {})
        sources.set_enabled(sources.KWORK, True)
        res = pipeline._load_pending(now - timedelta(hours=6), set(), [], fetch_alot=False)
        self.assertEqual(list(res), [kw])

    def test_kwork_pending_restored_from_db_when_feed_missing(self):
        oid = sources.KWORK_ID_OFFSET + 55
        now = datetime.now(timezone.utc)
        database.freelance_save(oid, "kworkru", "Парсер", "https://kwork.ru/projects/55/view", "до 5 000 ₽",
                                (now - timedelta(hours=1)).isoformat(), "dk", None, "", "pending", attempts=1)
        res = pipeline._load_pending(now - timedelta(hours=6), set(), [], fetch_alot=False)
        self.assertEqual(list(res), [oid])
        self.assertEqual(res[oid].order.site, "kworkru")


class AlotWakeUpHintTest(CycleTestBase):
    def run_track(self, has_new, last_new_ago_h=None):
        if last_new_ago_h is not None:
            database.kv_set("alot_last_new_at", int(time.time() - last_new_ago_h * 3600))
        with mock.patch.object(pipeline.monitor, "notify") as notify:
            pipeline._track_alot_activity(has_new)
        return notify

    def test_first_cycle_only_seeds(self):
        notify = self.run_track(has_new=False)
        notify.assert_not_called()
        self.assertIsNotNone(database.kv_get("alot_last_new_at"))

    def test_first_cycle_with_new_orders_does_not_notify(self):
        self.run_track(has_new=True).assert_not_called()

    def test_wakes_after_long_silence_notifies_once(self):
        notify = self.run_track(has_new=True, last_new_ago_h=5)
        notify.assert_called_once()
        text = notify.call_args.args[0]
        self.assertIn("alot.pro снова отдаёт заказы", text)
        self.assertIn("FL.ru и Kwork", text)
        self.assertIn("Источники", text)
        # сразу следующий цикл с новыми заказами - уже без подсказки
        self.run_track(has_new=True).assert_not_called()

    def test_short_gap_does_not_notify(self):
        self.run_track(has_new=True, last_new_ago_h=1).assert_not_called()

    def test_silence_without_new_orders_keeps_timestamp(self):
        before = int(time.time() - 5 * 3600)
        database.kv_set("alot_last_new_at", before)
        with mock.patch.object(pipeline.monitor, "notify") as notify:
            pipeline._track_alot_activity(False)
        notify.assert_not_called()
        self.assertEqual(int(database.kv_get("alot_last_new_at")), before)

    def test_no_hint_when_direct_sources_already_off(self):
        sources.set_enabled(sources.FLRU, False)
        sources.set_enabled(sources.KWORK, False)
        self.run_track(has_new=True, last_new_ago_h=5).assert_not_called()

    def test_hint_names_only_enabled_sources(self):
        sources.set_enabled(sources.FLRU, False)
        text = self.run_track(has_new=True, last_new_ago_h=5).call_args.args[0]
        self.assertIn("Kwork", text)
        self.assertNotIn("FL.ru", text)


class BotSourcesUiTest(CycleTestBase):
    def setUp(self):
        super().setUp()
        import bot
        self.bot = bot

    def test_keyboard_reflects_state(self):
        sources.set_enabled(sources.KWORK, False)
        rows = self.bot._sources_kb().inline_keyboard
        got = {r[0].callback_data: r[0].text for r in rows}
        self.assertEqual(set(got), {"fl_src_t:alot", "fl_src_t:flru", "fl_src_t:kwork"})
        self.assertIn("🟢 alot.pro", got["fl_src_t:alot"])
        self.assertIn("⚪ Kwork", got["fl_src_t:kwork"])

    def test_forced_off_button_is_noop(self):
        with mock.patch.dict(sources._ENV_ON, {sources.FLRU: False}):
            rows = self.bot._sources_kb().inline_keyboard
        self.assertEqual([r[0].callback_data for r in rows if "FL.ru" in r[0].text], ["fl_src_noop"])

    def test_menu_has_sources_button(self):
        kb = asyncio.run(self.bot.freelance_menu_kb())
        self.assertIn("fl_src", [b.callback_data for row in kb.inline_keyboard for b in row])

    def call(self, data):
        c = mock.MagicMock()
        c.data = data
        c.answer = mock.AsyncMock()
        c.message.edit_reply_markup = mock.AsyncMock()
        asyncio.run(self.bot.cb_fl_sources_toggle(c))
        return c

    def test_toggle_callback_switches_and_redraws(self):
        c = self.call("fl_src_t:flru")
        self.assertFalse(sources.is_enabled(sources.FLRU))
        c.message.edit_reply_markup.assert_awaited_once()
        self.call("fl_src_t:flru")
        self.assertTrue(sources.is_enabled(sources.FLRU))

    def test_toggle_callback_unknown_and_forced(self):
        c = self.call("fl_src_t:zzz")
        c.message.edit_reply_markup.assert_not_awaited()
        with mock.patch.dict(sources._ENV_ON, {sources.KWORK: False}):
            c = self.call("fl_src_t:kwork")
            self.assertFalse(sources.is_enabled(sources.KWORK))
        c.message.edit_reply_markup.assert_not_awaited()
        self.assertIsNone(database.kv_get("freelance_sources_off"))   # настройка не менялась

    def test_status_lists_every_source(self):
        sources.set_enabled(sources.FLRU, False)
        text = self.bot._freelance_status_text()
        self.assertIn("alot.pro", text)
        self.assertIn("⚪ FL.ru: выключен", text)
        self.assertIn("Kwork", text)


if __name__ == "__main__":
    unittest.main()
