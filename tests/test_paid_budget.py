import os
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest import mock

import database
from freelance import paid_budget


class PaidPilotFileTest(unittest.TestCase):
    NOW = datetime(2026, 11, 24, 12, tzinfo=timezone.utc)
    BODY = {"messages": [{"role": "user", "content": "test"}], "max_tokens": 2500}

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db = Path(directory.name) / "instance" / "jobs.sqlite"
        self.db.parent.mkdir()
        self.marker = self.db.with_name("yandex-paid-pilot-start.txt")
        for patch in (mock.patch.object(database, "DB_PATH", str(self.db)),
                      mock.patch.dict(os.environ, {"YANDEX_ENABLED": "1"})):
            patch.start()
            self.addCleanup(patch.stop)
        os.environ.pop("YANDEX_PAID_PILOT", None)
        os.environ.pop("YANDEX_PAID_PILOT_START", None)

    def test_only_enabled_instance_with_marker_next_to_its_database_is_active(self):
        self.assertFalse(paid_budget.active(self.NOW))
        self.marker.write_bytes(b"2026-11-24\r\n")
        self.assertTrue(paid_budget.active(self.NOW))
        self.assertFalse(paid_budget.active(self.NOW.replace(day=23)))
        self.assertFalse(paid_budget.active(self.NOW.replace(month=12, day=1)))
        with mock.patch.object(database, "DB_PATH", str(self.db.parent.parent / "other.sqlite")):
            self.assertFalse(paid_budget.active(self.NOW))
        with mock.patch.dict(os.environ, {"YANDEX_ENABLED": "0"}):
            self.assertFalse(paid_budget.active(self.NOW))

    def test_explicit_flag_has_precedence_even_when_env_start_is_missing(self):
        self.marker.write_bytes(b"2026-11-24")
        for flag in ("0", "", "1"):
            with self.subTest(flag=flag), mock.patch.dict(os.environ, {"YANDEX_PAID_PILOT": flag}):
                self.assertIsNone(paid_budget.start_date())
        with mock.patch.dict(os.environ, {"YANDEX_PAID_PILOT": "1", "YANDEX_PAID_PILOT_START": "2026-11-20"}):
            self.assertEqual(paid_budget.start_date(), date(2026, 11, 20))

    def test_invalid_or_unreadable_marker_fails_closed(self):
        for content in (b"", b"20261124", b"2026-W48-2", b"2026-11-33", b"2026-11-24\nignored", b"\xff"):
            with self.subTest(content=content):
                self.marker.write_bytes(content)
                self.assertIsNone(paid_budget.start_date())
        self.marker.write_bytes(b"2026-11-24")
        with mock.patch.object(Path, "open", side_effect=PermissionError):
            self.assertIsNone(paid_budget.start_date())

    def test_file_uses_existing_pilot_ledger_and_cannot_restart_it(self):
        database.init_db()
        def reserve():
            return paid_budget.reserve(paid_budget.MODEL, self.BODY, self.NOW)

        with mock.patch.dict(os.environ, {"YANDEX_PAID_PILOT": "1", "YANDEX_PAID_PILOT_START": "2026-11-23"}):
            self.assertIsNotNone(reserve())
        self.marker.write_bytes(b"2026-11-24")
        self.assertIsNone(reserve())
        self.assertFalse(paid_budget.summary(self.NOW)["active"])
        self.marker.write_bytes(b"2026-11-23\n")
        self.assertIsNotNone(reserve())
        self.assertIsNotNone(reserve())
        self.assertIsNone(reserve())
        self.assertAlmostEqual(paid_budget.summary(self.NOW)["held_rub"], 11.3304)
