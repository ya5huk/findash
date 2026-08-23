"""Shared types, schema checks, and integrity checks for migrations."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import sqlite3
from typing import Callable


LATEST_SCHEMA_VERSION = 15


class MigrationError(RuntimeError):
    """The database cannot be migrated without a human accounting decision."""


class UninitializedDatabaseError(MigrationError):
    """The file is not an initialized findash database."""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    apply: Callable[[sqlite3.Connection], None]


_CORE_TABLES = frozenset(
    {
        "documents",
        "accounts",
        "balances",
        "transactions",
        "securities",
        "trades",
        "prices",
        "fx_rates",
        "payslips",
        "payslip_line_items",
    }
)


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        is not None
    )


def _trigger_exists(connection: sqlite3.Connection, trigger: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='trigger' AND name=?", (trigger,)
        ).fetchone()
        is not None
    )


def _column_names(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}


def _add_column(
    connection: sqlite3.Connection, table: str, column: str, declaration: str
) -> None:
    if column not in _column_names(connection, table):
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")


def _require_findash_schema(connection: sqlite3.Connection) -> None:
    tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    missing = _CORE_TABLES - tables
    if missing:
        names = ", ".join(sorted(missing))
        raise UninitializedDatabaseError(
            f"not an initialized findash database (missing: {names})"
        )


def require_foreign_key_integrity(connection: sqlite3.Connection) -> None:
    """Refuse existing orphan rows without exposing their private values or row ids."""

    counts: dict[str, int] = defaultdict(int)
    for row in connection.execute("PRAGMA foreign_key_check"):
        counts[str(row[0])] += 1
    if counts:
        summary = ", ".join(
            f"{table}={count}" for table, count in sorted(counts.items())
        )
        raise MigrationError(
            f"foreign-key violations block database use ({summary}); correct them and retry"
        )


def relationship_violation_counts(
    connection: sqlite3.Connection, *, include_ledger_integrity: bool = True
) -> dict[str, int]:
    """Count cross-table identity violations without reading private row values."""

    checks: list[tuple[str, str]] = [
        (
            "event_evidence",
            """SELECT COUNT(*) FROM event_evidence AS evidence
               WHERE NOT EXISTS (
                 SELECT 1 FROM canonical_events AS canonical
                 JOIN source_events AS source ON source.id=evidence.source_event_id
                 JOIN account_feeds AS feed ON feed.id=source.account_feed_id
                 WHERE canonical.id=evidence.canonical_event_id
                   AND canonical.account_id=feed.account_id
                   AND canonical.event_type=source.event_type
               )""",
        ),
        (
            "transactions",
            """SELECT COUNT(*) FROM transactions AS fact
               WHERE (fact.source_event_id IS NOT NULL AND NOT EXISTS (
                        SELECT 1 FROM source_events AS source
                        JOIN account_feeds AS feed ON feed.id=source.account_feed_id
                        WHERE source.id=fact.source_event_id
                          AND source.event_type='cash'
                          AND feed.account_id=fact.account_id
                      ))
                  OR (fact.canonical_event_id IS NOT NULL AND NOT EXISTS (
                        SELECT 1 FROM canonical_events AS canonical
                        WHERE canonical.id=fact.canonical_event_id
                          AND canonical.event_type='cash'
                          AND canonical.account_id=fact.account_id
                      ))""",
        ),
        (
            "trades",
            """SELECT COUNT(*) FROM trades AS fact
               WHERE (fact.source_event_id IS NOT NULL AND NOT EXISTS (
                        SELECT 1 FROM source_events AS source
                        JOIN account_feeds AS feed ON feed.id=source.account_feed_id
                        WHERE source.id=fact.source_event_id
                          AND source.event_type='trade'
                          AND feed.account_id=fact.account_id
                      ))
                  OR (fact.canonical_event_id IS NOT NULL AND NOT EXISTS (
                        SELECT 1 FROM canonical_events AS canonical
                        WHERE canonical.id=fact.canonical_event_id
                          AND canonical.event_type='trade'
                          AND canonical.account_id=fact.account_id
                      ))""",
        ),
        (
            "balance_evidence",
            """SELECT COUNT(*) FROM balance_evidence AS evidence
               WHERE evidence.account_feed_id IS NOT NULL AND NOT EXISTS (
                 SELECT 1 FROM balances AS anchor
                 JOIN account_feeds AS feed ON feed.id=evidence.account_feed_id
                 WHERE anchor.id=evidence.canonical_balance_id
                   AND anchor.account_id=feed.account_id
               )""",
        ),
        (
            "position_evidence",
            """SELECT COUNT(*) FROM position_evidence AS evidence
               WHERE evidence.account_feed_id IS NOT NULL AND NOT EXISTS (
                 SELECT 1 FROM positions AS anchor
                 JOIN account_feeds AS feed ON feed.id=evidence.account_feed_id
                 WHERE anchor.id=evidence.canonical_position_id
                   AND anchor.account_id=feed.account_id
               )""",
        ),
    ]
    if include_ledger_integrity:
        checks.extend(
            (
                (
                    "transaction_event_pointer",
                    """SELECT COUNT(*) FROM transactions AS fact
                       WHERE fact.canonical_event_id IS NOT NULL
                         AND (
                           fact.source_event_id IS NULL
                           OR (SELECT COUNT(*) FROM event_evidence AS evidence
                                WHERE evidence.source_event_id=fact.source_event_id) != 1
                           OR NOT EXISTS (
                             SELECT 1 FROM event_evidence AS evidence
                              WHERE evidence.source_event_id=fact.source_event_id
                                AND evidence.canonical_event_id=fact.canonical_event_id
                                AND evidence.evidence_role!='aggregate'
                           )
                         )""",
                ),
                (
                    "trade_event_pointer",
                    """SELECT COUNT(*) FROM trades AS fact
                       WHERE fact.canonical_event_id IS NOT NULL
                         AND (
                           fact.source_event_id IS NULL
                           OR (SELECT COUNT(*) FROM event_evidence AS evidence
                                WHERE evidence.source_event_id=fact.source_event_id) != 1
                           OR NOT EXISTS (
                             SELECT 1 FROM event_evidence AS evidence
                              WHERE evidence.source_event_id=fact.source_event_id
                                AND evidence.canonical_event_id=fact.canonical_event_id
                                AND evidence.evidence_role!='aggregate'
                           )
                         )""",
                ),
            )
        )
    if _table_exists(connection, "trade_cash_links"):
        chronology = (
            "AND cash.occurred_on>=trade.occurred_on"
            if include_ledger_integrity
            else ""
        )
        checks.append(
            (
                "trade_cash_links",
                f"""SELECT COUNT(*) FROM trade_cash_links AS link
                   WHERE NOT EXISTS (
                     SELECT 1 FROM canonical_events AS cash
                     JOIN canonical_events AS trade ON trade.id=link.trade_event_id
                     WHERE cash.id=link.cash_event_id
                       AND cash.event_type='cash'
                       AND cash.flow_type='investment'
                       AND cash.status='confirmed'
                       AND trade.event_type='trade'
                       AND trade.status='confirmed'
                       AND cash.account_id=trade.account_id
                       AND cash.currency=trade.currency
                       {chronology}
                   )""",
            )
        )
        if include_ledger_integrity:
            checks.append(
                (
                    "trade_cash_economics",
                    """SELECT COUNT(*) FROM (
                         SELECT cash.id
                           FROM canonical_events AS cash
                           JOIN trade_cash_links AS link
                             ON link.cash_event_id=cash.id
                           JOIN canonical_events AS trade
                             ON trade.id=link.trade_event_id
                          GROUP BY cash.id, cash.amount_minor
                         HAVING ABS(
                           CAST(cash.amount_minor AS REAL) - SUM(
                             CASE trade.side
                               WHEN 'buy' THEN
                                 -(trade.quantity * trade.price_minor) - trade.fees_minor
                               ELSE
                                 (trade.quantity * trade.price_minor) - trade.fees_minor
                             END
                           )
                         ) > MAX(1.0, CAST(COUNT(*) AS REAL))
                       )""",
                )
            )
    if _table_exists(connection, "inferred_transfer_links"):
        checks.append(
            (
                "inferred_transfer_links",
                """SELECT COUNT(*) FROM inferred_transfer_links AS link
                   WHERE NOT EXISTS (
                     SELECT 1
                       FROM canonical_events AS outflow
                       JOIN canonical_events AS inflow
                         ON inflow.id=link.inflow_event_id
                       JOIN accounts AS source_account
                         ON source_account.id=outflow.account_id
                       JOIN accounts AS destination_account
                         ON destination_account.id=inflow.account_id
                       JOIN balances AS before_balance
                         ON before_balance.id=link.before_balance_id
                       JOIN balances AS after_balance
                         ON after_balance.id=link.after_balance_id
                      WHERE outflow.id=link.outflow_event_id
                        AND outflow.event_type='cash'
                        AND outflow.flow_type='transfer'
                        AND outflow.status='confirmed'
                        AND outflow.amount_minor<0
                        AND EXISTS (
                          SELECT 1 FROM event_evidence AS source_evidence
                           WHERE source_evidence.canonical_event_id=outflow.id
                        )
                        AND inflow.event_type='cash'
                        AND inflow.flow_type='transfer'
                        AND inflow.status='confirmed'
                        AND inflow.amount_minor>0
                        AND inflow.amount_minor=-outflow.amount_minor
                        AND inflow.currency=outflow.currency
                        AND inflow.occurred_on=outflow.occurred_on
                        AND source_account.id<>destination_account.id
                        AND source_account.kind<>'brokerage'
                        AND destination_account.kind='brokerage'
                        AND before_balance.account_id=inflow.account_id
                        AND after_balance.account_id=inflow.account_id
                        AND before_balance.currency=inflow.currency
                        AND after_balance.currency=inflow.currency
                        AND before_balance.component='cash_' || lower(inflow.currency)
                        AND after_balance.component=before_balance.component
                        AND before_balance.as_of<inflow.occurred_on
                        AND inflow.occurred_on<=after_balance.as_of
                        AND EXISTS (
                          SELECT 1
                            FROM balance_evidence AS before_evidence
                            JOIN balance_evidence AS after_evidence
                              ON after_evidence.account_feed_id=
                                 before_evidence.account_feed_id
                            JOIN account_feeds AS feed
                              ON feed.id=before_evidence.account_feed_id
                           WHERE before_evidence.canonical_balance_id=
                                 before_balance.id
                             AND after_evidence.canonical_balance_id=
                                 after_balance.id
                             AND before_evidence.account_snapshot_complete=1
                             AND after_evidence.account_snapshot_complete=1
                             AND feed.account_id=inflow.account_id
                             AND feed.is_active=1
                        )
                   )""",
            )
        )
    if _table_exists(connection, "funding_fx_links"):
        checks.append(
            (
                "funding_fx_links",
                """SELECT COUNT(*) FROM funding_fx_links AS link
                   WHERE NOT EXISTS (
                     SELECT 1
                       FROM canonical_events AS bank_outflow
                       JOIN canonical_events AS brokerage_inflow
                         ON brokerage_inflow.id=link.brokerage_inflow_event_id
                       JOIN canonical_events AS fx_source
                         ON fx_source.id=link.fx_source_event_id
                       JOIN canonical_events AS fx_destination
                         ON fx_destination.id=link.fx_destination_event_id
                       JOIN accounts AS bank_account
                         ON bank_account.id=bank_outflow.account_id
                       JOIN accounts AS brokerage_account
                         ON brokerage_account.id=brokerage_inflow.account_id
                      WHERE bank_outflow.id=link.bank_outflow_event_id
                        AND bank_outflow.event_type='cash'
                        AND bank_outflow.flow_type='transfer'
                        AND bank_outflow.status='confirmed'
                        AND bank_outflow.amount_minor<0
                        AND EXISTS (
                          SELECT 1 FROM event_evidence
                           WHERE canonical_event_id=bank_outflow.id
                        )
                        AND bank_account.kind<>'brokerage'
                        AND brokerage_account.kind='brokerage'
                        AND brokerage_inflow.event_type='cash'
                        AND brokerage_inflow.flow_type='transfer'
                        AND brokerage_inflow.status='confirmed'
                        AND brokerage_inflow.amount_minor=-bank_outflow.amount_minor
                        AND brokerage_inflow.currency=bank_outflow.currency
                        AND brokerage_inflow.occurred_on=bank_outflow.occurred_on
                        AND brokerage_inflow.component=
                            'cash_' || lower(brokerage_inflow.currency)
                        AND fx_source.account_id=brokerage_inflow.account_id
                        AND fx_source.event_type='cash'
                        AND fx_source.flow_type='investment'
                        AND lower(COALESCE(fx_source.category,''))='fx'
                        AND fx_source.status='confirmed'
                        AND fx_source.amount_minor=-brokerage_inflow.amount_minor
                        AND fx_source.currency=brokerage_inflow.currency
                        AND fx_source.component='cash_' || lower(fx_source.currency)
                        AND fx_source.occurred_on>=brokerage_inflow.occurred_on
                        AND fx_source.occurred_on<=
                            date(brokerage_inflow.occurred_on,'+3 days')
                        AND fx_destination.account_id=brokerage_inflow.account_id
                        AND fx_destination.event_type='cash'
                        AND fx_destination.flow_type='investment'
                        AND lower(COALESCE(fx_destination.category,''))='fx'
                        AND fx_destination.status='confirmed'
                        AND fx_destination.amount_minor>0
                        AND fx_destination.currency<>fx_source.currency
                        AND fx_destination.component=
                            'cash_' || lower(fx_destination.currency)
                        AND fx_destination.occurred_on=fx_source.occurred_on
                        AND EXISTS (
                          SELECT 1
                            FROM event_evidence AS source_evidence
                            JOIN source_events AS source_observation
                              ON source_observation.id=source_evidence.source_event_id
                            JOIN event_evidence AS destination_evidence
                              ON destination_evidence.canonical_event_id=
                                 fx_destination.id
                            JOIN source_events AS destination_observation
                              ON destination_observation.id=
                                 destination_evidence.source_event_id
                            JOIN documents AS document
                              ON document.id=source_observation.source_doc_id
                           WHERE source_evidence.canonical_event_id=fx_source.id
                             AND destination_observation.source_doc_id=document.id
                             AND lower(replace(
                                   COALESCE(document.doc_type,''),'-','_'))
                                   IN ('fx_conversion',
                                       'brokerage_fx_conversion')
                        )
                   )""",
            )
        )
    if _trigger_exists(connection, "balance_evidence_require_provenance_insert"):
        checks.extend(
            (
                (
                    "balance_evidence_provenance",
                    """SELECT COUNT(*) FROM balance_evidence
                       WHERE account_feed_id IS NULL AND source_doc_id IS NULL""",
                ),
                (
                    "position_evidence_provenance",
                    """SELECT COUNT(*) FROM position_evidence
                       WHERE account_feed_id IS NULL AND source_doc_id IS NULL""",
                ),
            )
        )
    return {
        label: count
        for label, query in checks
        if (count := int(connection.execute(query).fetchone()[0]))
    }


def require_relationship_integrity(connection: sqlite3.Connection) -> None:
    """Refuse contradictory normalized links without exposing private values."""

    violations = relationship_violation_counts(connection)
    if violations:
        summary = ", ".join(
            f"{label}={count}" for label, count in sorted(violations.items())
        )
        raise MigrationError(
            f"normalized relationship violations block database use ({summary}); "
            "reconcile them and retry"
        )
