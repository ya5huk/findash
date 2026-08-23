"""Versioned, transactional SQLite migrations for findash.

``init-db.sql`` describes a brand-new database. This facade preserves the public
migration API while keeping each migration family in a focused module.
"""

from __future__ import annotations

import sqlite3
from typing import Iterable

from .migration_ledger import (
    _migration_8_explicit_trade_cash_links,
    _migration_9_snapshot_provenance,
    _migration_10_accounting_integrity,
    _migration_11_ledger_relationship_integrity,
)
from .migration_legacy import (
    _migration_1_legacy_baseline,
    _migration_2_normalized_evidence,
)
from .migration_market import (
    _migration_12_security_distributions,
    _migration_13_market_observations,
)
from .migration_transfers import (
    _migration_14_inferred_account_transfers,
    _migration_15_funding_fx_links,
)
from .migration_support import (
    LATEST_SCHEMA_VERSION,
    Migration,
    MigrationError,
    UninitializedDatabaseError,
    _require_findash_schema,
    _table_exists,
    relationship_violation_counts,
    require_foreign_key_integrity,
    require_relationship_integrity,
)
from .migration_validation import (
    _migration_3_fact_validation,
    _migration_4_canonical_cash_component,
    _migration_5_evidence_account_guards,
    _migration_6_parent_identity_guards,
    _migration_7_complete_identity_guards,
)


MIGRATIONS: tuple[Migration, ...] = (
    Migration(1, "legacy_baseline", _migration_1_legacy_baseline),
    Migration(2, "normalized_event_evidence", _migration_2_normalized_evidence),
    Migration(3, "fact_validation", _migration_3_fact_validation),
    Migration(4, "canonical_cash_component", _migration_4_canonical_cash_component),
    Migration(5, "evidence_account_guards", _migration_5_evidence_account_guards),
    Migration(6, "parent_identity_guards", _migration_6_parent_identity_guards),
    Migration(7, "complete_identity_guards", _migration_7_complete_identity_guards),
    Migration(8, "explicit_trade_cash_links", _migration_8_explicit_trade_cash_links),
    Migration(9, "snapshot_provenance", _migration_9_snapshot_provenance),
    Migration(10, "accounting_integrity", _migration_10_accounting_integrity),
    Migration(
        11,
        "ledger_relationship_integrity",
        _migration_11_ledger_relationship_integrity,
    ),
    Migration(
        12,
        "security_distributions",
        _migration_12_security_distributions,
    ),
    Migration(
        13,
        "market_observations",
        _migration_13_market_observations,
    ),
    Migration(
        14,
        "inferred_account_transfers",
        _migration_14_inferred_account_transfers,
    ),
    Migration(
        15,
        "funding_fx_links",
        _migration_15_funding_fx_links,
    ),
)


def applied_versions(connection: sqlite3.Connection) -> set[int]:
    if not _table_exists(connection, "schema_migrations"):
        return set()
    rows = [
        (int(row[0]), str(row[1]))
        for row in connection.execute(
            "SELECT version, name FROM schema_migrations ORDER BY version"
        )
    ]
    known = {migration.version: migration.name for migration in MIGRATIONS}
    for version, name in rows:
        if version not in known:
            raise MigrationError(
                f"database schema v{version} is newer than this findash build"
            )
        if known[version] != name:
            raise MigrationError(
                f"migration v{version} identity mismatch ({name!r})"
            )
    versions = {version for version, _ in rows}
    if versions and versions != set(range(1, max(versions) + 1)):
        raise MigrationError("database migration history is not contiguous")
    return versions


def schema_version(connection: sqlite3.Connection) -> int:
    versions = applied_versions(connection)
    return max(versions, default=0)


def pending_migrations(connection: sqlite3.Connection) -> tuple[Migration, ...]:
    done = applied_versions(connection)
    return tuple(migration for migration in MIGRATIONS if migration.version not in done)


def apply_migrations(connection: sqlite3.Connection) -> tuple[Migration, ...]:
    """Apply every pending migration and return the migrations applied this call."""

    if int(connection.execute("PRAGMA foreign_keys").fetchone()[0]) != 1:
        raise MigrationError("foreign_keys must be enabled before migration")
    _require_findash_schema(connection)
    require_foreign_key_integrity(connection)
    connection.execute(
        """CREATE TABLE IF NOT EXISTS schema_migrations (
             version     INTEGER PRIMARY KEY,
             name        TEXT NOT NULL UNIQUE,
             applied_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
           )"""
    )
    connection.commit()

    applied: list[Migration] = []
    for migration in pending_migrations(connection):
        connection.execute("BEGIN IMMEDIATE")
        try:
            migration.apply(connection)
            require_foreign_key_integrity(connection)
            connection.execute(
                "INSERT INTO schema_migrations(version, name) VALUES (?, ?)",
                (migration.version, migration.name),
            )
            connection.commit()
            applied.append(migration)
        except Exception:
            connection.rollback()
            raise
    require_relationship_integrity(connection)
    return tuple(applied)


def require_current_schema(connection: sqlite3.Connection) -> None:
    """Fail clearly when a read-only caller opens an outdated database."""

    _require_findash_schema(connection)
    pending = pending_migrations(connection)
    if pending:
        versions = ", ".join(str(item.version) for item in pending)
        raise MigrationError(
            f"database schema is outdated (pending migrations: {versions}); "
            "run scripts/migrate_db.py"
        )
    require_foreign_key_integrity(connection)
    require_relationship_integrity(connection)


def migration_rows() -> Iterable[tuple[int, str]]:
    """Rows embedded by ``init-db.sql`` for a fresh, already-current database."""

    return ((migration.version, migration.name) for migration in MIGRATIONS)
