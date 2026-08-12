"""Safe SQLite connection entry point for findash command scripts."""

from __future__ import annotations

import os
from pathlib import Path
import sqlite3
from typing import Any

from .migrations import (
    UninitializedDatabaseError,
    apply_migrations,
    require_current_schema,
)


_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")


def validate_database_path(
    path: str | Path, *, must_exist: bool = True
) -> Path:
    """Validate one DB without following its file or immediate private parent.

    Findash's public commands use ``<repo>/data/finance.db``. The repository
    root is fixed and resolved from the script, so refusing the immediate
    ``data`` parent is sufficient to stop a checkout-local symlink escape while
    still allowing normal platform paths whose system ancestors may be links.
    """

    database = Path(os.path.abspath(path))
    parent = database.parent
    if parent.is_symlink() or (parent.exists() and not parent.is_dir()):
        raise UninitializedDatabaseError(
            "database directory is missing, unsafe, or is not a regular directory"
        )
    if database.is_symlink() or (database.exists() and not database.is_file()):
        raise UninitializedDatabaseError(
            "database is missing or is not a regular file; run scripts/migrate_db.py --init"
        )
    if must_exist and not database.is_file():
        raise UninitializedDatabaseError(
            "database is missing or is not a regular file; run scripts/migrate_db.py --init"
        )
    for suffix in _SIDECAR_SUFFIXES:
        if Path(f"{database}{suffix}").is_symlink():
            raise UninitializedDatabaseError("database sidecar path is unsafe")
    return database


def connect_db(
    path: str | Path,
    *,
    readonly: bool = False,
    migrate: bool = True,
    timeout: float = 30.0,
    **kwargs: Any,
) -> sqlite3.Connection:
    """Open a database with foreign keys enforced and the expected schema.

    Read/write callers migrate by default.  A read-only caller cannot migrate; it
    instead receives a clear error directing the user to ``scripts/migrate_db.py``.
    """

    database = validate_database_path(path)
    if readonly:
        connection = sqlite3.connect(
            f"{database.as_uri()}?mode=ro",
            uri=True,
            timeout=timeout,
            **kwargs,
        )
    else:
        connection = sqlite3.connect(database, timeout=timeout, **kwargs)

    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        if int(connection.execute("PRAGMA foreign_keys").fetchone()[0]) != 1:
            raise sqlite3.OperationalError("SQLite foreign-key enforcement is unavailable")
        if readonly:
            require_current_schema(connection)
        elif migrate:
            apply_migrations(connection)
        else:
            require_current_schema(connection)
        return connection
    except Exception:
        connection.close()
        raise
