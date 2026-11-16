"""Проверка миграции БД на копии (tools/migration_check): прежний код 110afe6 -> новый, откат, резервная копия.

Запуск из корня проекта: python -m unittest tests.test_migration_check -v
"""
import contextlib
import io
import os
import sqlite3
import tempfile
import unittest

from tools import migration_check


class MigrationCheckTest(unittest.TestCase):
    def test_synthetic_legacy_db_migrates_rolls_back_and_restores(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = migration_check.main([])
        self.assertEqual(code, 0, buf.getvalue())
        text = buf.getvalue()
        self.assertIn("идемпотентность", text)
        self.assertIn("прежний код открыл мигрированную БД", text)
        self.assertNotIn("ПРОВАЛ", text)

    def test_source_db_is_not_modified(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "prod.db")
            conn = sqlite3.connect(src)
            conn.execute("CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT)")
            conn.execute("INSERT INTO kv VALUES ('alot_last_id', '1')")
            conn.commit()
            conn.close()
            before = open(src, "rb").read()
            with contextlib.redirect_stdout(io.StringIO()):
                migration_check.main(["--db", src])
            self.assertEqual(open(src, "rb").read(), before)

    def _db(self, tmp, seen="2026-11-01T10:00:00", sent="2026-11-01T11:00:00"):
        path = os.path.join(tmp, "x.db")
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE freelance_orders (order_id INTEGER PRIMARY KEY, title TEXT, seen_at TEXT, sent_at TEXT)")
        conn.execute("INSERT INTO freelance_orders VALUES (1, 'a', ?, ?)", (seen, sent))
        conn.commit()
        conn.close()
        return path

    def test_old_columns_include_seen_and_sent_and_corruption_is_detected(self):
        self.assertIn("seen_at", migration_check.OLD_ORDER_COLS)
        self.assertIn("sent_at", migration_check.OLD_ORDER_COLS)
        with tempfile.TemporaryDirectory() as tmp:
            a = migration_check.snapshot(self._db(tmp))
            os.remove(os.path.join(tmp, "x.db"))
            b = migration_check.snapshot(self._db(tmp, seen="2026-11-02T10:00:00"))
            os.remove(os.path.join(tmp, "x.db"))
            c = migration_check.snapshot(self._db(tmp, sent="2026-11-02T11:00:00"))
        self.assertNotEqual(a["_orders_hash"], b["_orders_hash"])
        self.assertNotEqual(a["_orders_hash"], c["_orders_hash"])

    def test_diff_rows_finds_lost_or_changed_old_data(self):
        exp = {"1": {"title": "a", "seen_at": "t1", "sent_at": "t2"}, "2": {"title": "b", "seen_at": "t1", "sent_at": None}}
        self.assertEqual(migration_check.diff_rows(exp, exp), [])
        got = {"1": {"title": "a", "seen_at": "t9", "sent_at": "t2"}}
        bad = migration_check.diff_rows(exp, got)
        self.assertTrue(any("seen_at" in x for x in bad) and any("заказ 2 пропал" in x for x in bad))
        # поля, которые проверка меняла сама, не считаются искажением
        self.assertEqual(migration_check.diff_rows({"1": {"status": "a"}}, {"1": {"status": "b"}}, {"1": {"status"}}), [])

    def test_file_sha_changes_with_any_byte_and_detects_modified_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._db(tmp)
            first = migration_check.file_sha(path)
            self.assertEqual(first, migration_check.file_sha(path))
            with open(path, "ab") as f:
                f.write(b"\0")
            self.assertNotEqual(first, migration_check.file_sha(path))

    def test_run_reports_source_unchanged_snapshot_restore_old_reads_and_export(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = migration_check.main([])
        text = buf.getvalue()
        self.assertEqual(code, 0, text)
        for fragment in ("прежний код читает прежние строки", "экспорт и отчёты читают мигрированную БД",
                         "снимок источника не изменился", "возвращает те же байты и данные"):
            self.assertIn(fragment, text)

    def test_real_source_file_hash_checked_with_db_option(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "prod.db")
            conn = sqlite3.connect(src)
            conn.execute("CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT)")
            conn.commit()
            conn.close()
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                migration_check.main(["--db", src])
            self.assertIn("исходный файл БД не изменился за время проверки", buf.getvalue())

    def test_unknown_ref_reports_error(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(migration_check.main(["--old-ref", "no-such-ref-xyz"]), 2)


if __name__ == "__main__":
    unittest.main()
