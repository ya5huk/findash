#!/usr/bin/env python3
"""Render a self-contained dashboard from normalized, read-only account state."""

from __future__ import annotations

import argparse
from datetime import date
import os
from pathlib import Path
import sqlite3
import sys
import tempfile

from findash_core.errors import AccountingError
from findash_core.assets import AssetIntegrityError, require_vendor_assets
from findash_core.dashboard_html import render
from findash_core.dashboard_model import DashboardModelBuilder
from findash_core.db import connect_db
from findash_core.migrations import MigrationError


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "finance.db"
DEFAULT_OUTPUT = ROOT / "output" / "dashboard.html"
TEMPLATE_DIR = ROOT / "templates"
def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render the FinDash HTML dashboard.")
    parser.add_argument("--as-of", default=date.today().isoformat())
    return parser.parse_args(argv)


def _check_inputs(db: Path) -> None:
    if not db.is_file():
        raise FileNotFoundError("database not found; run scripts/migrate_db.py --init")
    require_vendor_assets(TEMPLATE_DIR / "vendor")


def _write_private_atomic(path: Path, content: str) -> None:
    if path.parent.is_symlink() or path.is_symlink():
        raise ValueError("dashboard output path may not contain symlinks")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.parent.is_symlink() or not path.parent.is_dir():
        raise ValueError("dashboard output directory is unsafe")
    os.chmod(path.parent, 0o700)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if temporary.exists():
            temporary.unlink()


def render_dashboard(db: Path, output_path: Path, as_of: str) -> None:
    """Render explicit paths for trusted in-process callers and tests."""

    date.fromisoformat(as_of)
    _check_inputs(db)
    connection = connect_db(db, readonly=True, migrate=False)
    try:
        model = DashboardModelBuilder(connection).build(as_of)
        output = render(model, TEMPLATE_DIR)
    finally:
        connection.close()
    _write_private_atomic(output_path, output)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    os.umask(0o077)
    try:
        render_dashboard(DEFAULT_DB, DEFAULT_OUTPUT, args.as_of)
        # Keep unattended/public diagnostics free of the local username and
        # checkout path. The command always targets this fixed relative file.
        print("Dashboard written: output/dashboard.html")
        return 0
    except (FileNotFoundError, OSError):
        print("Dashboard render failed: a required local file is missing or unsafe", file=sys.stderr)
        return 1
    except (
        AccountingError,
        AssetIntegrityError,
        MigrationError,
        sqlite3.Error,
        ValueError,
    ) as exc:
        print(f"Dashboard render failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
