#!/usr/bin/env python3
"""Create and upload a consistent weekly SQLite backup.

The live database uses WAL mode, so copying only ``finance.db`` can omit
committed pages that still live in ``finance.db-wal``.  This command uses
SQLite's online backup API to produce one closed, self-contained database,
checks it, uploads that snapshot twice, then removes the local temporary copy.

"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime


ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "finance.db"
DRIVE = ROOT / "scripts" / "drive.sh"
BACKUP_NAME = re.compile(r"^finance-\d{4}-\d{2}-\d{2}-\d{6}\.db$")


def create_consistent_snapshot(source_path: Path, destination_path: Path) -> None:
    """Copy a live SQLite DB into a standalone, integrity-checked snapshot."""
    source = sqlite3.connect(f"{source_path.resolve().as_uri()}?mode=ro", uri=True, timeout=30)
    destination = sqlite3.connect(destination_path)
    try:
        source.backup(destination)
        destination.commit()
        # A backup should travel as one ordinary file, never as another WAL DB.
        destination.execute("PRAGMA journal_mode=DELETE")
        result = destination.execute("PRAGMA integrity_check").fetchone()
        if not result or result[0] != "ok":
            raise RuntimeError("snapshot integrity check failed")
        foreign_key_violations = sum(
            1 for _ in destination.execute("PRAGMA foreign_key_check")
        )
        if foreign_key_violations:
            raise RuntimeError(
                "snapshot foreign key check failed "
                f"({foreign_key_violations} violation(s))"
            )
    finally:
        destination.close()
        source.close()
    os.chmod(destination_path, 0o600)


def backup_is_fresh(connection: sqlite3.Connection) -> bool:
    connection.execute(
        "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    connection.commit()
    row = connection.execute(
        """SELECT 1 FROM meta
           WHERE key='last_db_backup_at'
             AND julianday(value) > julianday('now', '-7 days')
             AND julianday(value) <= julianday('now', '+5 minutes')
           LIMIT 1"""
    ).fetchone()
    return row is not None


def drive(*args: str, capture: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(DRIVE), *args],
        cwd=ROOT,
        check=False,
        text=True,
        capture_output=capture,
    )


def prune_old_backups(retain: int) -> None:
    try:
        listing = drive("ls", "backups/", capture=True)
    except OSError:
        print("Backup prune: skipped (Drive command unavailable)")
        return
    if listing.returncode != 0:
        print("Backup prune: skipped (Drive listing failed)")
        return
    try:
        entries = json.loads(listing.stdout)
    except (TypeError, json.JSONDecodeError):
        print("Backup prune: skipped (unexpected Drive listing)")
        return

    if not isinstance(entries, list):
        print("Backup prune: skipped (unexpected Drive listing)")
        return
    backups = [
        item
        for item in entries
        if isinstance(item, dict)
        and not item.get("IsDir")
        and isinstance(item.get("Name"), str)
        and BACKUP_NAME.fullmatch(item["Name"])
    ]
    # The validated timestamp in the immutable archive name is the retention
    # authority. Drive metadata may be absent, malformed, or changed later.
    backups.sort(key=lambda item: item["Name"], reverse=True)
    failed = 0
    for item in backups[retain:]:
        result = drive("delete", f"backups/{item['Name']}", capture=True)
        failed += result.returncode != 0
    if failed:
        print(f"Backup prune: {failed} old backup(s) could not be removed")
    elif len(backups) > retain:
        print(f"Backup prune: removed {len(backups) - retain} old backup(s)")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Upload a WAL-safe SQLite snapshot to the configured Drive vault."
    )
    parser.add_argument("--force", action="store_true", help="ignore the seven-day gate")
    parser.add_argument(
        "--retain",
        type=int,
        default=8,
        metavar="N",
        help="number of timestamped Drive snapshots to retain (default: 8)",
    )
    args = parser.parse_args(argv)
    if args.retain < 1 or args.retain > 100:
        parser.error("--retain must be between 1 and 100")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    os.umask(0o077)

    unsafe_sidecar = any(
        Path(f"{DB_PATH}{suffix}").is_symlink()
        for suffix in ("-wal", "-shm", "-journal")
    )
    if (
        DB_PATH.parent.is_symlink()
        or not DB_PATH.is_file()
        or DB_PATH.is_symlink()
        or unsafe_sidecar
    ):
        print("Backup: failed (data/finance.db is missing or unsafe)", file=sys.stderr)
        return 1

    control: sqlite3.Connection | None = None
    snapshot_path: Path | None = None
    try:
        control = sqlite3.connect(DB_PATH, timeout=30)
        control.execute("PRAGMA foreign_keys=ON")
        control.execute(
            "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        control.commit()
        if not args.force and backup_is_fresh(control):
            print("Backup: skipped (fresh)")
            return 0

        fd, raw_path = tempfile.mkstemp(
            prefix=".finance-backup-", suffix=".db", dir=DB_PATH.parent
        )
        os.close(fd)
        snapshot_path = Path(raw_path)
        create_consistent_snapshot(DB_PATH, snapshot_path)

        stamp = datetime.now().astimezone().strftime("%Y-%m-%d-%H%M%S")
        current = drive("push", str(snapshot_path), "finance.db", capture=True)
        archived = drive(
            "push", str(snapshot_path), f"backups/finance-{stamp}.db", capture=True
        )
        if current.returncode != 0 or archived.returncode != 0:
            print("Backup: failed (Drive upload failed)", file=sys.stderr)
            return 1

        control.execute(
            """INSERT INTO meta(key, value) VALUES('last_db_backup_at', datetime('now'))
               ON CONFLICT(key) DO UPDATE SET value=excluded.value"""
        )
        control.commit()
        print("Backup: done (consistent SQLite snapshot)")
        prune_old_backups(args.retain)
        return 0
    except (OSError, sqlite3.Error, RuntimeError) as exc:
        print(f"Backup: failed ({exc})", file=sys.stderr)
        return 1
    finally:
        if control is not None:
            control.close()
        if snapshot_path is not None:
            try:
                snapshot_path.unlink(missing_ok=True)
            except OSError:
                print("Backup cleanup: temporary snapshot could not be removed", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
