#!/usr/bin/env python3
"""Initialize or migrate a findash SQLite database."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sqlite3
import sys
import tempfile

from findash_core.db import connect_db, validate_database_path
from findash_core.migrations import (
    LATEST_SCHEMA_VERSION,
    MigrationError,
    require_current_schema,
    schema_version,
)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "finance.db"
SCHEMA = ROOT / "scripts" / "init-db.sql"


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Initialize or transactionally migrate a findash database."
    )
    parser.add_argument(
        "--init",
        action="store_true",
        help="initialize the database when it does not exist or is empty",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="report whether migrations are pending without changing the database",
    )
    return parser.parse_args(argv)


def initialize(path: Path) -> None:
    path = validate_database_path(path, must_exist=False)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    path = validate_database_path(path, must_exist=False)
    if path.exists() and path.stat().st_size:
        raise MigrationError("refusing to initialize a non-empty database")

    # Build beside the destination and publish only a completely initialized
    # database. A failed schema statement, full disk, or interrupted validation
    # must not strand a half-created finance.db that the next setup run refuses.
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".init", dir=path.parent
    )
    temporary = Path(temporary_name)
    os.fchmod(descriptor, 0o600)
    os.close(descriptor)
    connection: sqlite3.Connection | None = None
    try:
        try:
            connection = sqlite3.connect(temporary)
            connection.executescript(SCHEMA.read_text(encoding="utf-8"))
            result = connection.execute("PRAGMA integrity_check").fetchone()
            if not result or result[0] != "ok":
                raise MigrationError(f"new database failed integrity_check: {result!r}")
            require_current_schema(connection)
            connection.commit()
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            if connection is not None:
                connection.close()

        path = validate_database_path(path, must_exist=False)
        if path.exists() and path.stat().st_size:
            raise MigrationError("refusing to initialize a non-empty database")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        temporary.unlink(missing_ok=True)
        for suffix in ("-wal", "-shm", "-journal"):
            Path(f"{temporary}{suffix}").unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    os.umask(0o077)
    path = DEFAULT_DB
    try:
        path = validate_database_path(path, must_exist=False)
        if not path.exists() or path.stat().st_size == 0:
            if not args.init:
                raise MigrationError("database is missing; rerun with --init")
            initialize(path)

        if args.check:
            connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
            try:
                connection.execute("PRAGMA foreign_keys=ON")
                current = schema_version(connection)
                if current == LATEST_SCHEMA_VERSION:
                    require_current_schema(connection)
            finally:
                connection.close()
            if current != LATEST_SCHEMA_VERSION:
                print(f"Database schema: outdated (v{current} → v{LATEST_SCHEMA_VERSION})")
                return 1
            print(f"Database schema: current (v{current})")
            return 0

        connection = connect_db(path)
        try:
            current = schema_version(connection)
        finally:
            connection.close()
        os.chmod(path, 0o600)
        print(f"Database schema: current (v{current})")
        return 0
    except (MigrationError, OSError, sqlite3.Error) as exc:
        print(f"Database migration failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
