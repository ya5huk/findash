"""Tests for the WAL-safe local snapshot portion of backup_database.py."""

from __future__ import annotations

import importlib.util
import io
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr
from unittest import mock


MODULE_PATH = Path(__file__).with_name("backup_database.py")
SPEC = importlib.util.spec_from_file_location("backup_database", MODULE_PATH)
assert SPEC and SPEC.loader
backup_database = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(backup_database)


class ConsistentSnapshotTests(unittest.TestCase):
    def test_backup_gate_rejects_malformed_stale_and_far_future_stamps(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        for value, expected in (
            ("not-a-date", False),
            ("2000-01-01 00:00:00", False),
            ("2999-01-01 00:00:00", False),
        ):
            with self.subTest(value=value):
                connection.execute("DELETE FROM meta")
                connection.execute(
                    "INSERT INTO meta VALUES('last_db_backup_at', ?)", (value,)
                )
                self.assertEqual(backup_database.backup_is_fresh(connection), expected)
        connection.execute("DELETE FROM meta")
        connection.execute(
            "INSERT INTO meta VALUES('last_db_backup_at', datetime('now'))"
        )
        self.assertTrue(backup_database.backup_is_fresh(connection))

    def test_snapshot_contains_committed_wal_rows_and_is_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            source_path = Path(tmp) / "source.db"
            snapshot_path = Path(tmp) / "snapshot.db"
            source = sqlite3.connect(source_path)
            source.execute("PRAGMA journal_mode=WAL")
            source.execute("PRAGMA wal_autocheckpoint=0")
            source.execute("CREATE TABLE facts(value TEXT)")
            source.commit()
            source.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            source.execute("INSERT INTO facts VALUES ('committed-in-wal')")
            source.commit()

            self.assertTrue(Path(f"{source_path}-wal").exists())
            backup_database.create_consistent_snapshot(source_path, snapshot_path)

            snapshot = sqlite3.connect(snapshot_path)
            try:
                self.assertEqual(
                    snapshot.execute("SELECT value FROM facts").fetchall(),
                    [("committed-in-wal",)],
                )
                self.assertEqual(snapshot.execute("PRAGMA integrity_check").fetchone(), ("ok",))
                self.assertEqual(snapshot.execute("PRAGMA foreign_key_check").fetchall(), [])
                self.assertEqual(snapshot.execute("PRAGMA journal_mode").fetchone(), ("delete",))
            finally:
                snapshot.close()
                source.close()
            self.assertEqual(os.stat(snapshot_path).st_mode & 0o777, 0o600)

    def test_full_backup_uploads_snapshot_twice_then_stamps_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "data"
            scripts = root / "scripts"
            data.mkdir()
            scripts.mkdir()
            database_path = data / "finance.db"
            connection = sqlite3.connect(database_path)
            connection.execute("CREATE TABLE facts(value TEXT)")
            connection.execute("INSERT INTO facts VALUES ('kept')")
            connection.commit()
            connection.close()

            log = root / "drive.log"
            fake_drive = scripts / "drive.sh"
            fake_drive.write_text(
                "#!/bin/sh\n"
                f"printf '%s\\n' \"$*\" >> {log!s}\n"
                "[ \"$1\" = ls ] && printf '[]\\n'\n"
                "exit 0\n"
            )
            fake_drive.chmod(0o700)

            old = (backup_database.ROOT, backup_database.DB_PATH, backup_database.DRIVE)
            backup_database.ROOT = root
            backup_database.DB_PATH = database_path
            backup_database.DRIVE = fake_drive
            try:
                self.assertEqual(backup_database.main(["--force"]), 0)
            finally:
                backup_database.ROOT, backup_database.DB_PATH, backup_database.DRIVE = old

            calls = log.read_text().splitlines()
            push_calls = [line for line in calls if line.startswith("push ")]
            self.assertEqual(len(push_calls), 2)
            uploaded_path = Path(push_calls[0].split()[1])
            self.assertFalse(uploaded_path.exists())
            connection = sqlite3.connect(database_path)
            try:
                self.assertIsNotNone(
                    connection.execute(
                        "SELECT value FROM meta WHERE key='last_db_backup_at'"
                    ).fetchone()
                )
            finally:
                connection.close()

    def test_full_backup_rejects_foreign_key_violations_without_leaking_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "data"
            data.mkdir()
            database_path = data / "finance.db"
            connection = sqlite3.connect(database_path)
            connection.execute(
                "CREATE TABLE private_parent(id INTEGER PRIMARY KEY)"
            )
            connection.execute(
                """CREATE TABLE private_child(
                       id INTEGER PRIMARY KEY,
                       parent_id INTEGER NOT NULL REFERENCES private_parent(id)
                   )"""
            )
            connection.execute(
                "INSERT INTO private_child(id, parent_id) VALUES(987654, 123456)"
            )
            connection.commit()
            connection.close()

            old = (backup_database.ROOT, backup_database.DB_PATH)
            backup_database.ROOT = root
            backup_database.DB_PATH = database_path
            stderr = io.StringIO()
            try:
                with mock.patch.object(backup_database, "drive") as drive, redirect_stderr(
                    stderr
                ):
                    self.assertEqual(backup_database.main(["--force"]), 1)
                drive.assert_not_called()
            finally:
                backup_database.ROOT, backup_database.DB_PATH = old

            error = stderr.getvalue()
            self.assertIn(
                "snapshot foreign key check failed (1 violation(s))", error
            )
            for private_value in (
                "private_parent",
                "private_child",
                "987654",
                "123456",
            ):
                self.assertNotIn(private_value, error)
            connection = sqlite3.connect(database_path)
            try:
                self.assertIsNone(
                    connection.execute(
                        "SELECT value FROM meta WHERE key='last_db_backup_at'"
                    ).fetchone()
                )
            finally:
                connection.close()
            self.assertEqual(list(data.glob(".finance-backup-*.db")), [])

    def test_backup_refuses_symlinked_sqlite_sidecar(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "data"
            data.mkdir()
            database_path = data / "finance.db"
            connection = sqlite3.connect(database_path)
            connection.execute("CREATE TABLE facts(value TEXT)")
            connection.commit()
            connection.close()
            outside = root / "outside-wal"
            outside.write_bytes(b"keep")
            Path(f"{database_path}-wal").symlink_to(outside)

            old = (backup_database.ROOT, backup_database.DB_PATH)
            backup_database.ROOT = root
            backup_database.DB_PATH = database_path
            try:
                self.assertEqual(backup_database.main(["--force"]), 1)
            finally:
                backup_database.ROOT, backup_database.DB_PATH = old

            self.assertEqual(outside.read_bytes(), b"keep")
            self.assertEqual(list(data.glob(".finance-backup-*.db")), [])

    def test_prune_ignores_unexpected_listing_entries(self):
        listing = mock.Mock(returncode=0, stdout='[null, 7, {"Name": null}]')
        with mock.patch.object(backup_database, "drive", return_value=listing):
            backup_database.prune_old_backups(8)

    def test_prune_orders_valid_archives_by_filename_not_nullable_modtime(self):
        listing = mock.Mock(
            returncode=0,
            stdout=(
                '[{"Name":"finance-2026-01-01-000000.db",'
                '"ModTime":"2999-01-01T00:00:00Z"},'
                '{"Name":"finance-2026-01-08-000000.db","ModTime":null}]'
            ),
        )
        calls = []

        def fake_drive(*args, **kwargs):
            calls.append((args, kwargs))
            if args[0] == "ls":
                return listing
            return mock.Mock(returncode=0)

        with mock.patch.object(backup_database, "drive", side_effect=fake_drive):
            backup_database.prune_old_backups(1)

        deleted = [args[1] for args, _kwargs in calls if args[0] == "delete"]
        self.assertEqual(deleted, ["backups/finance-2026-01-01-000000.db"])


if __name__ == "__main__":
    unittest.main()
