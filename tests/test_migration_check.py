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

    def test_unknown_ref_reports_error(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(migration_check.main(["--old-ref", "no-such-ref-xyz"]), 2)


if __name__ == "__main__":
    unittest.main()
