"""Журнал увиденных заказов: состояния, дедуп до AI, очередь, миграция старых записей. Без сети.

Запуск из корня проекта: python -m unittest tests.test_journal -v
"""
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import database
from freelance import alot_client, journal as jr, pipeline, report, sources
from freelance.alot_client import FreelanceOrder
from tests.test_pipeline_cycle import CycleTestBase, ai_result

LONG_BODY = ("Нужно настроить выгрузку заказов из Wildberries в Google Таблицу каждый час, "
             "с расчётом остатков и отчётом по продажам для менеджера. Доступ к кабинету дам.")


def order(oid, title="Выгрузка заказов из Wildberries в Google Таблицу", body=LONG_BODY, site="flru",
          age_h=0.2, price=3000.0, cats=()):
    return FreelanceOrder(oid, site, title, body, f"{int(price)} ₽" if price else "по договорённости", price,
                          list(cats), datetime.now(timezone.utc) - timedelta(hours=age_h), f"https://x/{oid}")


class PureJournalTest(unittest.TestCase):
    def sl(self, a, b):
        return jr.same_listing(a, b)

    def test_long_texts_same_listing_even_if_description_edited(self):
        a = {"body": jr.norm_text(LONG_BODY), "origin": "alot"}
        edited = jr.norm_text(LONG_BODY + " Бюджет согласуем.")
        self.assertTrue(self.sl(a, {"body": edited, "origin": "flru"}))

    def test_long_texts_different_orders_not_merged(self):
        a = {"body": jr.norm_text(LONG_BODY), "origin": "alot"}
        other = jr.norm_text("Нужен телеграм бот для записи клиентов в салон красоты, оплата через ЮKassa, "
                             "админка для мастера и напоминания клиентам за сутки до визита.")
        self.assertFalse(self.sl(a, {"body": other, "origin": "flru"}))

    def test_short_texts_same_origin_never_merged(self):
        """«Создать бота» с пустым описанием у двух заказчиков одного источника - два заказа."""
        a = {"body": "", "origin": "alot", "price": 0, "published_ts": 1000, "reliable": True}
        self.assertFalse(self.sl(a, dict(a, published_ts=1010)))

    def test_short_texts_cross_source_need_same_price_and_time(self):
        a = {"body": "", "origin": "alot", "price": 3000, "published_ts": 1000, "reliable": True}
        b = {"body": "", "origin": "flru", "price": 3000, "published_ts": 1070, "reliable": True}
        self.assertTrue(self.sl(a, b))
        self.assertFalse(self.sl(a, dict(b, price=5000)))
        self.assertFalse(self.sl(a, dict(b, published_ts=1000 + 3 * 3600)))
        self.assertFalse(self.sl(a, dict(b, reliable=False)))

    def test_pub_unreliable_only_alot_kwork(self):
        self.assertTrue(jr.pub_unreliable("kworkru", "alot"))
        self.assertFalse(jr.pub_unreliable("kworkru", "kwork"))
        self.assertFalse(jr.pub_unreliable("flru", "alot"))

    def test_legacy_status_mirror(self):
        self.assertEqual(jr.legacy_status(jr.QUEUED, jr.D_NONE, None), "pending")
        self.assertEqual(jr.legacy_status(jr.ASSESSED, jr.D_PENDING, None), "pending")
        self.assertEqual(jr.legacy_status(jr.ASSESSED, jr.D_CARD, None), "sent")
        self.assertEqual(jr.legacy_status(jr.ASSESSED, jr.D_CARD, None, card_deleted=True), "expired")
        self.assertEqual(jr.legacy_status(jr.ASSESSED, jr.D_CARD, jr.LEAD_AGREED, card_deleted=True), "taken")
        self.assertEqual(jr.legacy_status(jr.ASSESSED, jr.D_DIGEST, None), "rejected")
        self.assertEqual(jr.legacy_status(jr.STALE, jr.D_NONE, None), "expired")


class IntakeTest(CycleTestBase):
    """Каждый увиденный заказ получает объяснимое состояние; дубль не оценивается повторно."""

    def setUp(self):
        super().setUp()
        p = mock.patch.object(pipeline, "TRIAGE_MODE", "off")   # без сетевых вызовов триажа
        p.start()
        self.addCleanup(p.stop)

    def cycle(self, alot=(), feed=(), score=None, last_id=100, real_pending=False):
        database.kv_set(pipeline.LAST_ID_KEY, last_id)
        score = score or mock.Mock(return_value=ai_result(8))
        pending = (mock.patch.object(pipeline, "_fetch_items", return_value=[]) if real_pending
                   else mock.patch.object(pipeline, "_load_pending", return_value={}))
        with mock.patch.object(pipeline, "fetch_new_orders", return_value=self.scan(list(alot))), \
                mock.patch.object(pipeline, "_fetch_feed_orders", return_value=list(feed)), \
                mock.patch.object(pipeline.ai_scorer, "score_order", score), pending:
            res = pipeline._prepare_cycle()
        return res, score

    def test_filtered_order_logged_with_reason_once(self):
        o = order(201, title="Купить диван", body="", site="youdoru")
        res, score = self.cycle(alot=[o])
        row = database.freelance_get(201)
        self.assertEqual((row["tech"], row["reject_reason"], row["delivery"]), ("filtered", "no_include", "none"))
        score.assert_not_called()
        self.cycle(alot=[o])    # повторный скан того же окна: строка одна, состояние то же
        self.assertEqual(database.freelance_get(201)["tech"], "filtered")

    def test_queued_and_assessed_rows_never_store_contacts_in_title(self):
        o = order(203, title="Нужен бот, пишите на ivan@mail.ru или +7 999 123-45-67", site="youdoru")
        self.cycle(alot=[o])
        row = database.freelance_get(203)
        self.assertEqual(row["tech"], "assessed")
        for field in ("title", "snippet", "price_text"):
            self.assertNotIn("ivan@mail.ru", row[field] or "")
            self.assertNotIn("999", row[field] or "")

    def test_filtered_row_has_masked_title_and_short_snippet(self):
        o = order(202, title="Купить диван звонить +7 999 123-45-67", body="x" * 1000, site="youdoru")
        self.cycle(alot=[o])
        row = database.freelance_get(202)
        self.assertNotIn("999", row["title"])
        self.assertLessEqual(len(row["snippet"]), jr.SNIPPET_FILTERED)

    def test_excluded_site_has_no_row_and_counted_once_with_cursor(self):
        board = order(301, title="Python developer", site="hhru")
        res, _ = self.cycle(alot=[board, order(302, site="youdoru")], last_id=300)
        self.assertIsNone(database.freelance_get(301))
        self.assertEqual(res.excluded, {"hhru": 1})
        pipeline.finish_cycle(res, [], [], [])
        self.assertEqual(report.load_excluded(1), {"hhru": 1})
        # то же окно скана при следующем цикле: заказ ниже курсора, второй раз не считается
        res2, _ = self.cycle(alot=[board], last_id=302)
        self.assertEqual(res2.excluded, {})

    def test_same_order_via_alot_and_direct_is_assessed_once(self):
        via_alot = order(401, site="flru")
        direct = order(sources.FL_ID_OFFSET + 5, site="flru", age_h=0.2 - 0.02)
        res, score = self.cycle(alot=[via_alot], feed=[direct])
        self.assertEqual(score.call_count, 1)
        dup = database.freelance_get(direct.order_id)
        self.assertEqual((dup["tech"], dup["dup_of"]), ("duplicate", 401))
        self.assertEqual(database.freelance_get(401)["tech"], "assessed")

    def test_direct_first_then_alot_also_one_assessment(self):
        direct = order(sources.FL_ID_OFFSET + 6, site="flru", age_h=0.5)
        self.cycle(feed=[direct])
        via_alot = order(402, site="flru", age_h=0.48)
        res, score = self.cycle(alot=[via_alot])
        score.assert_not_called()
        self.assertEqual(database.freelance_get(402)["dup_of"], direct.order_id)

    def test_kwork_alot_timestamp_unreliable_still_matches_by_text(self):
        direct = order(sources.KWORK_ID_OFFSET + 7, site="kworkru", age_h=30)
        via_alot = order(403, site="kworkru", age_h=0.01)    # alot отдаёт время своего сбора
        self.cycle(feed=[direct])
        res, score = self.cycle(alot=[via_alot])
        score.assert_not_called()
        self.assertEqual(database.freelance_get(403)["tech"], "duplicate")
        self.assertEqual(database.freelance_get(403)["pub_unreliable"], 1)

    def test_same_title_different_orders_not_merged(self):
        a = order(501, title="Создать телеграм бота", body="Нужен бот для записи на маникюр", site="youdoru")
        b = order(502, title="Создать телеграм бота", body="", site="youdoru")
        c = order(503, title="Создать телеграм бота", body="", site="youdoru")
        res, score = self.cycle(alot=[a, b, c])
        self.assertEqual(score.call_count, 3)
        self.assertEqual({database.freelance_get(i)["tech"] for i in (501, 502, 503)}, {"assessed"})

    def test_spam_repost_same_source_same_text_is_duplicate(self):
        a = order(511, site="youdoru", age_h=2)
        b = order(512, site="youdoru", age_h=1)
        res, score = self.cycle(alot=[a, b])
        self.assertEqual(score.call_count, 1)
        self.assertEqual(database.freelance_get(512)["dup_of"], 511)

    def test_updated_description_of_assessed_order_is_not_reassessed(self):
        o = order(601, site="flru")
        self.cycle(alot=[o])
        edited = order(601, site="flru", body=LONG_BODY + " Срок - до пятницы.")
        res, score = self.cycle(alot=[edited])
        score.assert_not_called()
        row = database.freelance_get(601)
        self.assertEqual(row["desc_changed"], 1)
        self.assertEqual(row["tech"], "assessed")

    def test_updated_description_lets_filtered_order_through(self):
        o = order(602, title="Нужна помощь", body="Купить диван", site="youdoru")
        self.cycle(alot=[o])
        self.assertEqual(database.freelance_get(602)["tech"], "filtered")
        edited = order(602, title="Нужна помощь", body="Нужен телеграм бот для заказов", site="youdoru")
        res, score = self.cycle(alot=[edited], real_pending=True)
        self.assertEqual(database.freelance_get(602)["tech"], "assessed")
        self.assertEqual(score.call_count, 1)

    def test_late_discovered_order_is_assessed_not_dropped(self):
        late = order(701, age_h=20)          # раньше отсекался жёстким пределом 6 ч
        res, score = self.cycle(alot=[late])
        self.assertEqual(score.call_count, 1)
        self.assertEqual([s.order.order_id for s in res.to_send], [701])

    def test_very_old_order_is_stale_with_reason(self):
        old = order(702, age_h=80)
        res, score = self.cycle(alot=[old])
        score.assert_not_called()
        row = database.freelance_get(702)
        self.assertEqual((row["tech"], row["stale_reason"]), ("stale", "old_at_discovery"))

    def test_queue_reason_when_no_quota(self):
        o = order(801, site="youdoru")
        with mock.patch.object(pipeline.ai_scorer, "quota_exhausted",
                               return_value=datetime.now(timezone.utc) + timedelta(hours=5)):
            res, score = self.cycle(alot=[o])
        score.assert_not_called()
        pipeline.finish_cycle(res, [], [], [])
        row = database.freelance_get(801)
        self.assertEqual((row["tech"], row["queue_reason"]), ("queued", "no_quota"))

    def test_queue_reason_on_ai_error_counts_attempts(self):
        o = order(802, site="youdoru")
        res, _ = self.cycle(alot=[o], score=mock.Mock(return_value=None))
        pipeline.finish_cycle(res, [], [], [])
        row = database.freelance_get(802)
        self.assertEqual((row["tech"], row["queue_reason"], row["attempts"]), ("queued", "ai_error", 1))

    def test_queue_reason_wait_when_cycle_limit_reached(self):
        orders = [order(900 + i, title=f"Бот номер {i}", body=f"Нужен бот {i} " * 8, site="youdoru") for i in range(3)]
        with mock.patch.object(pipeline, "MAX_AI_PER_CYCLE", 1):
            res, score = self.cycle(alot=orders)
        pipeline.finish_cycle(res, [], [], [])
        reasons = sorted((database.freelance_get(o.order_id)["tech"], database.freelance_get(o.order_id)["queue_reason"])
                         for o in orders)
        self.assertEqual(reasons.count(("queued", "ai_wait")), 2)

    def test_digest_and_rejected_have_distinct_states(self):
        good, shown, low = (order(1001, title="Бот один", body="Нужен бот для заказов " * 4, site="youdoru"),
                            order(1002, title="Бот два", body="Нужен бот для записи " * 4, site="youdoru"),
                            order(1003, title="Бот три", body="Нужен бот для учёта " * 4, site="youdoru"))
        scores = {1001: ai_result(9), 1002: ai_result(8), 1003: ai_result(2)}
        res, _ = self.cycle(alot=[good, shown, low],
                            score=mock.Mock(side_effect=lambda o: dict(scores[o.order_id])))
        by_id = {sc.order.order_id: sc for sc in res.to_send + res.rejected}
        pipeline.finish_cycle(res, [by_id[1001]], [by_id[1002]], [])
        pipeline.save_sent(by_id[1001], 77)
        r1, r2, r3 = (database.freelance_get(i) for i in (1001, 1002, 1003))
        self.assertEqual((r1["delivery"], r1["status"]), ("card", "sent"))
        self.assertEqual((r2["tech"], r2["verdict"], r2["delivery"]), ("assessed", "ok", "digest"))
        self.assertEqual((r3["tech"], r3["verdict"], r3["delivery"]), ("assessed", "low_fit", "none"))
        d = database.freelance_day_summary()
        self.assertEqual((d["good"], d["low_fit"], d["card"], d["digest"]), (2, 1, 1, 1))

    def test_every_seen_order_has_an_explainable_state(self):
        orders = [order(1101, site="hhru"),
                  order(1102, title="Купить диван", body="", site="youdoru"),
                  order(1103, site="flru"), order(1104, site="flru", age_h=90),
                  order(1105, title="Бот", body="Нужен бот " * 8, site="youdoru")]
        res, _ = self.cycle(alot=orders, last_id=1100)
        rows = {o.order_id: database.freelance_get(o.order_id) for o in orders}
        self.assertIsNone(rows[1101])                      # вакансионная доска: только счётчик
        self.assertEqual(res.excluded, {"hhru": 1})
        for oid in (1102, 1103, 1104, 1105):
            r = rows[oid]
            self.assertIn(r["tech"], jr.TECH_STATES)
            self.assertTrue(r["reject_reason"] or r["stale_reason"] or r["dup_of"] or r["queue_reason"]
                            or r["verdict"], oid)

    def test_restart_does_not_duplicate_or_reassess(self):
        o = order(1201, site="flru")
        self.cycle(alot=[o])
        res, score = self.cycle(alot=[o])        # тот же заказ в окне после «рестарта»
        score.assert_not_called()
        with database._connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM freelance_orders WHERE order_id = 1201").fetchone()[0], 1)


class QueueAgingTest(unittest.TestCase):
    def sc(self, title, waited_h, cats=()):
        o = order(1, title=title, body="", site="youdoru", cats=cats)
        return pipeline.ScoredOrder(o, None, "k", first_seen=datetime.now(timezone.utc) - timedelta(hours=waited_h))

    def test_web_category_has_no_priority_advantage(self):
        web = self.sc("Нужен разработчик", 0, cats=["WEB"])
        table = self.sc("Формулы в Google Таблицах", 0)
        now = datetime.now(timezone.utc)
        self.assertLess(pipeline._queue_rank(table, now), pipeline._queue_rank(web, now))

    def test_waiting_order_is_boosted_but_not_above_group_1(self):
        now = datetime.now(timezone.utc)
        low = self.sc("Нужен разработчик", 13, cats=["WEB"])          # группа 3, ждёт 13 ч
        fresh_mid = self.sc("Помощь", 0, cats=["PROG_OTHER"])          # группа 2
        fresh_top = self.sc("Парсер сайта", 0)                         # группа 1
        self.assertLessEqual(pipeline._queue_rank(low, now), pipeline._queue_rank(fresh_mid, now))
        self.assertGreater(pipeline._queue_rank(low, now), pipeline._queue_rank(fresh_top, now))
        self.assertEqual(pipeline._eff_group(low, now), 2)
        self.assertEqual(pipeline._eff_group(fresh_top, now), 1)


class TriageGuardTest(CycleTestBase):
    def test_on_without_evidence_falls_back_to_shadow(self):
        with mock.patch.object(pipeline, "TRIAGE_MODE", "on"):
            self.assertEqual(pipeline.effective_triage_mode(), "shadow")
            ok, why = pipeline.triage_allowed()
            self.assertFalse(ok)
            self.assertIn("мало данных", why)

    def test_misses_block_on(self):
        for i in range(pipeline.TRIAGE_MIN_SAMPLE):
            pipeline._record_triage_evidence(miss=(i == 0), good=(i < 30))
        with mock.patch.object(pipeline, "TRIAGE_MODE", "on"):
            self.assertEqual(pipeline.effective_triage_mode(), "shadow")
            self.assertIn("пропуски", pipeline.triage_allowed()[1])

    def test_agreement_with_full_ai_alone_does_not_allow_on(self):
        # совпадение с полной AI-оценкой не доказывает полноту: нужна ещё разметка владельца
        for i in range(pipeline.TRIAGE_MIN_SAMPLE):
            pipeline._record_triage_evidence(miss=False, good=(i < 30))
        with mock.patch.object(pipeline, "TRIAGE_MODE", "on"):
            self.assertEqual(pipeline.effective_triage_mode(), "shadow")
            self.assertIn("разметки", pipeline.triage_allowed()[1])

    def test_enough_clean_evidence_with_owner_labels_allows_on(self):
        for i in range(pipeline.TRIAGE_MIN_SAMPLE):
            pipeline._record_triage_evidence(miss=False, good=(i < 30), order_id=9000 + i, triage=7)
        for i in range(pipeline.TRIAGE_MIN_LABELED):
            self.assertEqual(pipeline.record_triage_label(9000 + i, True), "good")
        with mock.patch.object(pipeline, "TRIAGE_MODE", "on"):
            self.assertEqual(pipeline.effective_triage_mode(), "on")

    def test_shadow_triage_limited_per_day(self):
        o = order(1301, site="youdoru")
        database.kv_set(pipeline.LAST_ID_KEY, 100)
        with mock.patch.object(pipeline, "TRIAGE_MODE", "shadow"), \
                mock.patch.object(pipeline, "TRIAGE_SHADOW_MAX_PER_DAY", 5), \
                mock.patch.object(pipeline.ai_scorer, "triage_today", return_value=5), \
                mock.patch.object(pipeline.ai_scorer, "triage_order") as triage, \
                mock.patch.object(pipeline, "fetch_new_orders", return_value=self.scan([o])), \
                mock.patch.object(pipeline.ai_scorer, "score_order", return_value=ai_result(8)):
            pipeline._prepare_cycle()
        triage.assert_not_called()


class ReportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = database.DB_PATH
        database.DB_PATH = os.path.join(self.tmp.name, "t.db")
        database.init_db()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(setattr, database, "DB_PATH", self.old_path)

    def test_funnel_delays_and_export_do_not_mix_dimensions(self):
        pub = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        database.freelance_save(1, "flru", "Бот", "u", "", pub, "k1", 8, "", "sent", message_id=1, origin="alot")
        database.freelance_save(2, "flru", "Купить диван +7 999 123-45-67", "u", "", pub, "k2", None, "",
                                "pending", tech=jr.FILTERED, reject_reason="no_include", origin="alot")
        rows = report.load_rows(1)
        fn = report.funnel(rows)["flru/alot"]
        self.assertEqual((fn["discovered"], fn["filtered"], fn["assessed"], fn["card"]), (2, 1, 1, 1))
        self.assertIn("flru/alot", report.format_funnel(rows, {"hhru": 5}, 1))
        csv_text = report.export_csv(rows)
        self.assertNotIn("999", csv_text)
        self.assertIn("no_include", csv_text)
        loss = report.losses(rows)
        self.assertEqual(loss["filtered"]["no_include"]["n"], 1)

    def test_delays_skip_unreliable_publication(self):
        pub = (datetime.now(timezone.utc) - timedelta(hours=40)).isoformat()
        database.freelance_save(3, "kworkru", "t", "u", "", pub, "k", None, "", "pending", origin="alot",
                                pub_unreliable=True)
        self.assertIsNone(report.delays(report.load_rows(1))["kworkru/alot"]["source"])


class ReportToolsReadOnlyTest(unittest.TestCase):
    """tools/loss_report.py и capacity_report.py не меняют боевую БД (в том числе старой схемы)."""

    def test_old_schema_db_untouched(self):
        import contextlib
        import hashlib
        import io
        from tools import capacity_report, loss_report
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "old.db")
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE freelance_orders (order_id INTEGER PRIMARY KEY, site TEXT, title TEXT, url TEXT, "
                     "price_text TEXT, fit INTEGER, ai_json TEXT, published_at TEXT, dedup_key TEXT, "
                     "seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, sent_at TIMESTAMP, status TEXT)")
        conn.execute("INSERT INTO freelance_orders (order_id, site, title, fit, status, published_at) "
                     "VALUES (1, 'flru', 'Бот', 8, 'pending', '2026-11-01T00:00:00+00:00')")
        conn.commit()
        conn.close()
        digest = lambda: hashlib.sha256(open(path, "rb").read()).hexdigest()
        before = digest()
        old = database.DB_PATH
        self.addCleanup(setattr, database, "DB_PATH", old)
        for tool in (loss_report, capacity_report):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(tool.main(["--db", path]), 0)
        self.assertEqual(digest(), before)
        conn = sqlite3.connect(path)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(freelance_orders)")}
        conn.close()
        self.assertNotIn("tech", cols)          # миграция схемы выполнена только на копии


    def test_snapshot_includes_uncheckpointed_wal_rows(self):
        """Бот пишет в WAL: снимок не должен терять строки, ещё не перенесённые в основной файл."""
        from freelance import report
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "live.db")
        old = database.DB_PATH
        self.addCleanup(setattr, database, "DB_PATH", old)
        database.DB_PATH = path
        database.init_db()
        live = sqlite3.connect(path)          # читающая транзакция бота не даёт сбросить WAL в основной файл
        live.execute("BEGIN")
        live.execute("SELECT COUNT(*) FROM freelance_orders").fetchone()
        database.freelance_save(7, "flru", "Бот", "u", "", datetime.now(timezone.utc).isoformat(), "k", 8, "",
                                "sent", message_id=1)
        self.addCleanup(live.close)
        self.assertTrue(os.path.exists(path + "-wal"))
        report.use_copy_of(path)
        self.assertEqual([r["order_id"] for r in report.load_rows(1)], [7])


class CardAgeTest(unittest.TestCase):
    def sc(self, oid, age_h, site="flru", seen_after_min=None):
        o = order(oid, site=site, age_h=age_h)
        first = o.published_at + timedelta(minutes=seen_after_min) if seen_after_min is not None else None
        return pipeline.ScoredOrder(o, {"fit": 8, "summary": "s"}, "k", first_seen=first)

    def test_fresh_order_has_no_age_warning(self):
        self.assertNotIn("не свежий", pipeline.format_order(self.sc(1, 1)))

    def test_old_order_is_not_presented_as_fresh(self):
        text = pipeline.format_order(self.sc(2, 20))
        self.assertIn("опубликован 20 ч назад", text)
        self.assertIn("не свежий", text)

    def test_alot_kwork_date_is_flagged_unknown(self):
        text = pipeline.format_order(self.sc(3, 0.1, site="kworkru"))
        self.assertIn("дата публикации неизвестна", text)

    def test_source_lag_shown_in_detailed_card_only(self):
        sc = self.sc(4, 2, seen_after_min=95)
        self.assertIn("обнаружен через", pipeline.format_order(sc, detailed=True))
        self.assertNotIn("обнаружен через", pipeline.format_order(sc))


class CapacityTest(unittest.TestCase):
    STATUS = {"providers": {
        "cloudflare": {"has_key": True, "enabled": True, "dead": False, "limit": 50},
        "openrouter": {"has_key": True, "enabled": True, "dead": False, "limit": 45},
        "yandex": {"has_key": True, "enabled": True, "dead": False, "limit": 400},
        "groq": {"has_key": False, "enabled": False, "dead": False, "limit": 900},
        "github": {"has_key": True, "enabled": True, "dead": False, "limit": 290},
        "mistral": {"has_key": True, "enabled": True, "dead": True, "limit": 1000},
    }}

    def test_yandex_dead_and_unkeyed_providers_not_counted(self):
        from datetime import date
        from freelance import capacity
        f = capacity.forecast(self.STATUS, 127, 0, date(2026, 11, 13), date(2026, 11, 20), overhead=0.0)
        self.assertEqual(f["providers"], {"cloudflare": 50, "openrouter": 45})
        self.assertEqual(f["total_without_yandex"], 95)
        self.assertEqual(f["unconfirmed"], {"github": 290})     # настроен, но с IP BotHost не отвечает
        self.assertTrue(f["yandex_active_now"])
        self.assertEqual(f["days_to_yandex_off"], 7)
        self.assertAlmostEqual(f["deficit_per_day"], 32)
        self.assertLess(f["coverage"], 1)

    def test_shadow_triage_takes_shared_cloudflare_quota(self):
        from datetime import date
        from freelance import capacity
        base = capacity.forecast(self.STATUS, 100, 0, date(2026, 11, 13), date(2026, 11, 20))
        shadow = capacity.forecast(self.STATUS, 100, 300, date(2026, 11, 13), date(2026, 11, 20))
        self.assertGreater(shadow["triage_cost_orders"], 0)
        self.assertLess(shadow["net_without_yandex"], base["net_without_yandex"])
        no_cf = {"providers": {"openrouter": self.STATUS["providers"]["openrouter"]}}
        self.assertEqual(capacity.forecast(no_cf, 100, 300, date(2026, 11, 13),
                                           date(2026, 11, 20))["triage_cost_orders"], 0)

    def test_text_names_assumptions(self):
        from datetime import date
        from freelance import capacity
        text = capacity.format_forecast(capacity.forecast(self.STATUS, 127, 100, date(2026, 11, 21), date(2026, 11, 20)))
        self.assertIn("Допущения", text)
        self.assertIn("не замер", text)


if __name__ == "__main__":
    unittest.main()
