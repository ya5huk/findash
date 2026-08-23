"""Tests for the WAL-safe local snapshot backup in backup_database.py."""

from __future__ import annotations

import importlib.util
import io
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
import unittest
from contextlib import redirect_stderr


MODULE_PATH = Path(__file__).with_name("backup_database.py")
SPEC = importlib.util.spec_from_file_location("backup_database", MODULE_PATH)
assert SPEC and SPEC.loader
backup_database = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(backup_database)


def _seed_database(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE facts(value TEXT)")
    connection.execute("INSERT INTO facts VALUES ('kept')")
    connection.commit()
    connection.close()


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

    def test_full_backup_writes_private_local_archive_then_stamps_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "data"
            data.mkdir()
            database_path = data / "finance.db"
            _seed_database(database_path)

            old = (backup_database.ROOT, backup_database.DB_PATH)
            backup_database.ROOT = root
            backup_database.DB_PATH = database_path
            try:
                self.assertEqual(backup_database.main(["--force"]), 0)
            finally:
                backup_database.ROOT, backup_database.DB_PATH = old

            backups = data / "backups"
            self.assertEqual(stat.S_IMODE(backups.stat().st_mode), 0o700)
            archives = sorted(backups.iterdir())
            self.assertEqual(len(archives), 1)
            self.assertRegex(archives[0].name, backup_database.BACKUP_NAME)
            self.assertEqual(stat.S_IMODE(archives[0].stat().st_mode), 0o600)
            archive = sqlite3.connect(archives[0])
            try:
                self.assertEqual(archive.execute("SELECT value FROM facts").fetchall(), [("kept",)])
            finally:
                archive.close()
            self.assertEqual(list(data.glob(".finance-backup-*.db")), [])
            connection = sqlite3.connect(database_path)
            try:
                self.assertIsNotNone(
                    connection.execute(
                        "SELECT value FROM meta WHERE key='last_db_backup_at'"
                    ).fetchone()
                )
            finally:
                connection.close()

    def test_fresh_backup_is_skipped_without_the_force_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "data"
            data.mkdir()
            database_path = data / "finance.db"
            _seed_database(database_path)
            connection = sqlite3.connect(database_path)
            connection.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            connection.execute("INSERT INTO meta VALUES('last_db_backup_at', datetime('now'))")
            connection.commit()
            connection.close()

            old = (backup_database.ROOT, backup_database.DB_PATH)
            backup_database.ROOT = root
            backup_database.DB_PATH = database_path
            try:
                self.assertEqual(backup_database.main([]), 0)
            finally:
                backup_database.ROOT, backup_database.DB_PATH = old
            self.assertFalse((data / "backups").exists())

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
                with redirect_stderr(stderr):
                    self.assertEqual(backup_database.main(["--force"]), 1)
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
            self.assertEqual(list((data / "backups").glob("finance-*.db")), [])

    def test_backup_refuses_symlinked_sqlite_sidecar(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "data"
            data.mkdir()
            database_path = data / "finance.db"
            _seed_database(database_path)
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

    def test_backup_refuses_symlinked_backup_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "data"
            data.mkdir()
            database_path = data / "finance.db"
            _seed_database(database_path)
            elsewhere = root / "elsewhere"
            elsewhere.mkdir()
            (data / "backups").symlink_to(elsewhere, target_is_directory=True)

            old = (backup_database.ROOT, backup_database.DB_PATH)
            backup_database.ROOT = root
            backup_database.DB_PATH = database_path
            stderr = io.StringIO()
            try:
                with redirect_stderr(stderr):
                    self.assertEqual(backup_database.main(["--force"]), 1)
            finally:
                backup_database.ROOT, backup_database.DB_PATH = old
            self.assertIn("must not be a symlink", stderr.getvalue())
            self.assertEqual(list(elsewhere.iterdir()), [])

    def test_prune_orders_valid_archives_by_filename_and_ignores_other_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            older = directory / "finance-2026-01-01-000000.db"
            newer = directory / "finance-2026-01-08-000000.db"
            stranger = directory / "notes.txt"
            for entry in (older, newer, stranger):
                entry.write_bytes(b"x")
            # A misleading modification time must not override the name order.
            os.utime(older, (4_102_444_800, 4_102_444_800))

            backup_database.prune_old_backups(1, directory)

            self.assertFalse(older.exists())
            self.assertTrue(newer.exists())
            self.assertTrue(stranger.exists())

    def test_prune_skips_missing_or_symlinked_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            target = directory / "real"
            target.mkdir()
            (target / "finance-2026-01-01-000000.db").write_bytes(b"x")
            link = directory / "link"
            link.symlink_to(target, target_is_directory=True)

            backup_database.prune_old_backups(0, link)
            backup_database.prune_old_backups(0, directory / "missing")

            self.assertTrue((target / "finance-2026-01-01-000000.db").exists())


if __name__ == "__main__":
    unittest.main()
