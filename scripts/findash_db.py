#!/usr/bin/env python3
"""Constrained database gateway for skill-driven ingestion.

The command always targets this checkout's ``data/finance.db``.  It deliberately
does not expose SQLite dot commands, extension loading, attached databases, arbitrary
filesystem paths, or schema mutation; migrations have their own fixed command.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
from typing import Any

from findash_core.corrections import CorrectionError, remove_incorrect_balance
from findash_core.db import connect_db
from findash_core.migrations import MigrationError, require_current_schema
from findash_core.reconciliation import (
    ReconciliationError,
    reconcile_logical_accounts,
)


ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "finance.db"
BATCH_ROOT = ROOT / "inbox" / "staging" / "operations"
MAX_QUERY_ROWS = 500
MAX_BATCH_STATEMENTS = 2_000
MAX_REQUEST_BYTES = 5 * 1024 * 1024

_COMMENT_OR_SEPARATOR = re.compile(r";|--|/\*|\*/")
_FORBIDDEN = re.compile(
    r"\b(ATTACH|DETACH|PRAGMA|VACUUM|DROP|ALTER|CREATE|REINDEX|ANALYZE|"
    r"LOAD_EXTENSION|READFILE|WRITEFILE|REPLACE)\b",
    re.IGNORECASE,
)
_PROTECTED_METADATA = re.compile(r"\bschema_migrations\b", re.IGNORECASE)


class GatewayError(ValueError):
    pass


def _one_statement(sql: str) -> str:
    statement = sql.strip()
    if not statement:
        raise GatewayError("SQL statement is empty")
    if _COMMENT_OR_SEPARATOR.search(statement):
        raise GatewayError("SQL comments and statement separators are not allowed")
    if _FORBIDDEN.search(statement):
        raise GatewayError("SQL contains an operation unavailable through this gateway")
    return statement


def validate_query(sql: str) -> str:
    statement = _one_statement(sql)
    if not re.match(r"^(SELECT|WITH)\b", statement, re.IGNORECASE):
        raise GatewayError("query must start with SELECT or WITH")
    # A WITH statement can hide a modifying CTE on newer SQLite builds.
    if re.search(r"\b(INSERT|UPDATE|DELETE|REPLACE)\b", statement, re.IGNORECASE):
        raise GatewayError("query must be read-only")
    return statement


def validate_mutation(sql: str) -> str:
    statement = _one_statement(sql)
    match = re.match(r"^(INSERT(?:\s+OR\s+IGNORE)?|UPDATE)\b", statement, re.IGNORECASE)
    if not match:
        raise GatewayError(
            "batch statements must be INSERT, INSERT OR IGNORE, or UPDATE; "
            "use ON CONFLICT DO UPDATE instead of destructive REPLACE"
        )
    if re.match(r"^UPDATE\b", statement, re.IGNORECASE) and not re.search(
        r"\bWHERE\b", statement, re.IGNORECASE
    ):
        raise GatewayError("UPDATE requires a WHERE clause")
    if _PROTECTED_METADATA.search(statement):
        raise GatewayError("migration metadata is unavailable through this gateway")
    return statement


def query_database(
    path: Path, sql: str, params: list[Any] | dict[str, Any] | None = None
) -> list[dict]:
    statement = validate_query(sql)
    con = connect_db(path, readonly=True, migrate=False)
    con.row_factory = sqlite3.Row
    try:
        cursor = con.execute(statement, params or [])
        rows = cursor.fetchmany(MAX_QUERY_ROWS + 1)
        if len(rows) > MAX_QUERY_ROWS:
            raise GatewayError(f"query exceeds the {MAX_QUERY_ROWS}-row output limit")
        return [dict(row) for row in rows]
    finally:
        con.close()


def apply_batch(path: Path, payload: dict) -> int:
    statements = payload.get("statements")
    if not isinstance(statements, list) or not statements:
        raise GatewayError("batch requires a non-empty statements list")
    if len(statements) > MAX_BATCH_STATEMENTS:
        raise GatewayError(f"batch exceeds {MAX_BATCH_STATEMENTS} statements")
    prepared: list[tuple[str, list[Any] | dict[str, Any]]] = []
    for index, item in enumerate(statements):
        if not isinstance(item, dict) or not isinstance(item.get("sql"), str):
            raise GatewayError(f"statement {index} requires a SQL string")
        params = item.get("params", [])
        if not isinstance(params, (list, dict)):
            raise GatewayError(f"statement {index} params must be a list or object")
        prepared.append((validate_mutation(item["sql"]), params))

    con = connect_db(path)
    try:
        with con:
            for statement, params in prepared:
                con.execute(statement, params)
            # Raw convenience rows may be written before their normalized
            # evidence inside this transaction.  Validate the completed graph
            # before the context manager is allowed to commit it.
            require_current_schema(con)
        return len(prepared)
    finally:
        con.close()


def _request_path(value: str) -> Path:
    repository = Path(os.path.abspath(ROOT))
    request_root = Path(os.path.abspath(BATCH_ROOT))
    try:
        root_relative = request_root.relative_to(repository)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("request directory is outside this checkout") from exc
    current = repository
    for part in root_relative.parts:
        current /= part
        if current.is_symlink():
            raise argparse.ArgumentTypeError("request directory may not contain symlinks")

    path = Path(value)
    candidate = Path(os.path.abspath(repository / path if not path.is_absolute() else path))
    try:
        relative = candidate.relative_to(request_root)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"request file must be a .json under {BATCH_ROOT.relative_to(ROOT)}"
        ) from exc
    current = request_root
    for part in relative.parent.parts:
        current /= part
        if current.is_symlink():
            raise argparse.ArgumentTypeError("request path may not contain symlinks")
    if (
        candidate.suffix.lower() != ".json"
        or candidate.is_symlink()
        or not candidate.is_file()
    ):
        raise argparse.ArgumentTypeError(
            f"request file must be a regular .json under {BATCH_ROOT.relative_to(ROOT)}"
        )
    try:
        if candidate.stat().st_size > MAX_REQUEST_BYTES:
            raise argparse.ArgumentTypeError("request file is too large")
        os.chmod(candidate, 0o600)
    except OSError as exc:
        raise argparse.ArgumentTypeError("request file could not be secured") from exc
    return candidate


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Safely query or update the FinDash database.")
    sub = parser.add_subparsers(dest="command", required=True)
    query = sub.add_parser("query", help="run one bounded parameterized SELECT request")
    query.add_argument("file", type=_request_path)
    batch = sub.add_parser("apply", help="apply one atomic parameterized JSON batch")
    batch.add_argument("file", type=_request_path)
    reconcile = sub.add_parser(
        "reconcile-account",
        help="merge one explicitly reviewed duplicate logical account",
    )
    reconcile.add_argument("file", type=_request_path)
    correction = sub.add_parser(
        "remove-incorrect-balance",
        help="remove one fully guarded, explicitly reviewed balance",
    )
    correction.add_argument("file", type=_request_path)
    sub.add_parser("integrity", help="run foreign-key and integrity checks")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)
    args = parse_args(argv or sys.argv[1:])
    try:
        if args.command == "query":
            request = json.loads(args.file.read_text(encoding="utf-8"))
            if not isinstance(request, dict) or not isinstance(request.get("sql"), str):
                raise GatewayError("query request requires a SQL string")
            params = request.get("params", [])
            if not isinstance(params, (list, dict)):
                raise GatewayError("query params must be a list or object")
            print(json.dumps(query_database(DB, request["sql"], params), ensure_ascii=False))
        elif args.command == "apply":
            payload = json.loads(args.file.read_text(encoding="utf-8"))
            count = apply_batch(DB, payload)
            print(f"Database batch applied: {count} statement(s)")
        elif args.command == "reconcile-account":
            payload = json.loads(args.file.read_text(encoding="utf-8"))
            counts = reconcile_logical_accounts(DB, payload)
            print(json.dumps({"status": "ok", "counts": counts}, sort_keys=True))
        elif args.command == "remove-incorrect-balance":
            payload = json.loads(args.file.read_text(encoding="utf-8"))
            counts = remove_incorrect_balance(DB, payload)
            print(json.dumps({"status": "ok", "counts": counts}, sort_keys=True))
        else:
            con = connect_db(DB, readonly=True, migrate=False)
            try:
                integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
                foreign_keys = con.execute("PRAGMA foreign_key_check").fetchall()
            finally:
                con.close()
            if integrity != "ok" or foreign_keys:
                raise GatewayError(
                    f"integrity={integrity}; foreign_key_violations={len(foreign_keys)}"
                )
            print("Database integrity: ok")
        return 0
    except (
        GatewayError,
        CorrectionError,
        MigrationError,
        ReconciliationError,
        json.JSONDecodeError,
        OSError,
        sqlite3.Error,
    ) as exc:
        print(f"Database command failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
