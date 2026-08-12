"""Ledger-link and snapshot-provenance migrations (versions 8-11)."""

from __future__ import annotations

import sqlite3

from .migration_support import (
    MigrationError,
    _add_column,
    relationship_violation_counts,
)
from .migration_validation import _fact_validation_specs, _install_validation_triggers


def _migration_8_explicit_trade_cash_links(connection: sqlite3.Connection) -> None:
    """Replace derived trade cash only through a reviewed canonical relationship."""

    connection.execute(
        """CREATE TABLE IF NOT EXISTS trade_cash_links (
             cash_event_id   INTEGER NOT NULL REFERENCES canonical_events(id),
             trade_event_id  INTEGER NOT NULL REFERENCES canonical_events(id),
             match_method    TEXT NOT NULL CHECK (length(trim(match_method)) > 0),
             created_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
             PRIMARY KEY (cash_event_id, trade_event_id)
           )"""
    )
    connection.execute(
        """CREATE INDEX IF NOT EXISTS idx_trade_cash_links_trade
           ON trade_cash_links(trade_event_id)"""
    )

    invalid = relationship_violation_counts(
        connection, include_ledger_integrity=False
    )
    if invalid.get("trade_cash_links"):
        raise MigrationError(
            "invalid existing rows block migration v8 "
            f"(trade_cash_links={invalid['trade_cash_links']}); correct them and retry"
        )

    valid_link = """EXISTS (
      SELECT 1 FROM canonical_events AS cash
      JOIN canonical_events AS trade ON trade.id=NEW.trade_event_id
      WHERE cash.id=NEW.cash_event_id
        AND cash.event_type='cash'
        AND cash.flow_type='investment'
        AND cash.status='confirmed'
        AND trade.event_type='trade'
        AND trade.status='confirmed'
        AND cash.account_id=trade.account_id
        AND cash.currency=trade.currency
    )"""
    connection.execute(
        f"""CREATE TRIGGER IF NOT EXISTS trade_cash_links_valid_insert
            BEFORE INSERT ON trade_cash_links
            WHEN NOT ({valid_link})
            BEGIN
              SELECT RAISE(ABORT, 'invalid trade cash link');
            END"""
    )
    connection.execute(
        f"""CREATE TRIGGER IF NOT EXISTS trade_cash_links_valid_update
            BEFORE UPDATE OF cash_event_id, trade_event_id, match_method
            ON trade_cash_links
            WHEN NOT ({valid_link})
            BEGIN
              SELECT RAISE(ABORT, 'invalid trade cash link');
            END"""
    )
    connection.execute(
        """CREATE TRIGGER IF NOT EXISTS canonical_events_trade_cash_identity_update
           BEFORE UPDATE OF account_id, event_type, currency, status, flow_type
           ON canonical_events
           WHEN (
             NEW.account_id IS NOT OLD.account_id
             OR NEW.event_type IS NOT OLD.event_type
             OR NEW.currency IS NOT OLD.currency
             OR NEW.status IS NOT OLD.status
             OR NEW.flow_type IS NOT OLD.flow_type
           ) AND (
             EXISTS (
               SELECT 1 FROM trade_cash_links WHERE cash_event_id=OLD.id
             ) OR EXISTS (
               SELECT 1 FROM trade_cash_links WHERE trade_event_id=OLD.id
             )
           )
           BEGIN
             SELECT RAISE(ABORT, 'linked canonical event identity is immutable');
           END"""
    )


def _migration_9_snapshot_provenance(connection: sqlite3.Connection) -> None:
    """Give every snapshot observation an explicit document or feed provenance."""

    provider = "findash-legacy-snapshot"
    invalid_existing_feeds = int(
        connection.execute(
            """SELECT COUNT(*) FROM account_feeds
               WHERE provider=? AND external_account_ref IS NULL
                 AND feed_type!='manual'""",
            (provider,),
        ).fetchone()[0]
    )
    if invalid_existing_feeds:
        raise MigrationError(
            "invalid existing rows block migration v9 "
            f"(account_feeds={invalid_existing_feeds}); correct them and retry"
        )

    account_ids = {
        int(row[0])
        for row in connection.execute(
            """SELECT anchor.account_id
               FROM balance_evidence AS evidence
               JOIN balances AS anchor ON anchor.id=evidence.canonical_balance_id
               WHERE evidence.account_feed_id IS NULL
                 AND evidence.source_doc_id IS NULL
               UNION
               SELECT anchor.account_id
               FROM position_evidence AS evidence
               JOIN positions AS anchor ON anchor.id=evidence.canonical_position_id
               WHERE evidence.account_feed_id IS NULL
                 AND evidence.source_doc_id IS NULL"""
        )
    }
    for account_id in sorted(account_ids):
        connection.execute(
            """INSERT INTO account_feeds(account_id,provider,feed_type,label)
               SELECT ?,?,'manual','Legacy snapshot provenance'
               WHERE NOT EXISTS (
                 SELECT 1 FROM account_feeds
                 WHERE account_id=? AND provider=?
                   AND external_account_ref IS NULL
               )""",
            (account_id, provider, account_id, provider),
        )

    for evidence_table, anchor_table, anchor_column in (
        ("balance_evidence", "balances", "canonical_balance_id"),
        ("position_evidence", "positions", "canonical_position_id"),
    ):
        connection.execute(
            f"""UPDATE {evidence_table} AS evidence
                SET account_feed_id=(
                  SELECT feed.id
                  FROM {anchor_table} AS anchor
                  JOIN account_feeds AS feed
                    ON feed.account_id=anchor.account_id
                   AND feed.provider=?
                   AND feed.external_account_ref IS NULL
                  WHERE anchor.id=evidence.{anchor_column}
                )
                WHERE evidence.account_feed_id IS NULL
                  AND evidence.source_doc_id IS NULL""",
            (provider,),
        )

    remaining = {
        table: int(
            connection.execute(
                f"""SELECT COUNT(*) FROM {table}
                    WHERE account_feed_id IS NULL AND source_doc_id IS NULL"""
            ).fetchone()[0]
        )
        for table in ("balance_evidence", "position_evidence")
    }
    remaining = {table: count for table, count in remaining.items() if count}
    if remaining:
        summary = ", ".join(
            f"{table}={count}" for table, count in sorted(remaining.items())
        )
        raise MigrationError(
            f"invalid existing rows block migration v9 ({summary}); correct them and retry"
        )

    for table in ("balance_evidence", "position_evidence"):
        for operation in ("insert", "update"):
            columns = (
                " OF account_feed_id, source_doc_id" if operation == "update" else ""
            )
            connection.execute(
                f"""CREATE TRIGGER IF NOT EXISTS {table}_require_provenance_{operation}
                    BEFORE {operation.upper()}{columns} ON {table}
                    WHEN NEW.account_feed_id IS NULL AND NEW.source_doc_id IS NULL
                    BEGIN
                      SELECT RAISE(ABORT, '{table} requires provenance');
                    END"""
            )


def _migration_10_accounting_integrity(
    connection: sqlite3.Connection,
) -> None:
    """Tighten snapshot completeness and trade-settlement accounting metadata."""

    duplicate_trade_settlements = int(
        connection.execute(
            """SELECT COUNT(*) FROM (
                 SELECT trade_event_id
                   FROM trade_cash_links
                  GROUP BY trade_event_id
                 HAVING COUNT(*)>1
               )"""
        ).fetchone()[0]
    )
    if duplicate_trade_settlements:
        raise MigrationError(
            "ambiguous split trade settlements block migration v10 "
            f"(trade_cash_links={duplicate_trade_settlements}); reconcile them and retry"
        )
    connection.execute("DROP INDEX IF EXISTS idx_trade_cash_links_trade")
    connection.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS uq_trade_cash_links_trade
           ON trade_cash_links(trade_event_id)"""
    )

    for table in ("balance_evidence", "position_evidence"):
        _add_column(
            connection,
            table,
            "account_snapshot_complete",
            "INTEGER NOT NULL DEFAULT 0 CHECK (account_snapshot_complete IN (0,1))",
        )

    # Migration v3 predates these columns. Rebuild only the two evidence tables'
    # validation triggers so upgraded and fresh databases enforce the same shape.
    specs = dict(_fact_validation_specs())
    for table in ("balance_evidence", "position_evidence"):
        for operation in ("insert", "update"):
            connection.execute(
                f"DROP TRIGGER IF EXISTS validate_{table}_{operation}"
            )
        _install_validation_triggers(
            connection,
            table,
            [
                *specs[table],
                "typeof(NEW.account_snapshot_complete)='integer' "
                "AND NEW.account_snapshot_complete IN (0,1)",
            ],
        )


def _migration_11_ledger_relationship_integrity(
    connection: sqlite3.Connection,
) -> None:
    """Make normalized convenience links and settlement links self-consistent."""

    violations = relationship_violation_counts(connection)
    if violations:
        summary = ", ".join(
            f"{label}={count}" for label, count in sorted(violations.items())
        )
        raise MigrationError(
            f"invalid existing rows block migration v11 ({summary}); "
            "reconcile them and retry"
        )

    # Very old databases accepted by migration 1 may predate these ordinary
    # lookup indexes. They do not change semantics, but fresh and migrated
    # databases should have the same query plan options.
    baseline_indexes = (
        ("idx_accounts_institution", "accounts", "institution"),
        ("idx_accounts_kind", "accounts", "kind"),
        ("idx_balances_account_date", "balances", "account_id, as_of"),
        ("idx_documents_doc_date", "documents", "doc_date"),
        ("idx_documents_doc_type", "documents", "doc_type"),
        ("idx_event_evidence_source", "event_evidence", "source_event_id"),
        ("idx_payslip_line_items_payslip", "payslip_line_items", "payslip_id"),
        ("idx_payslips_period", "payslips", "period_start"),
        ("idx_trades_account_security", "trades", "account_id, security_id"),
        ("idx_trades_date", "trades", "date"),
        ("idx_transactions_account_date", "transactions", "account_id, date"),
        ("idx_transactions_category", "transactions", "category"),
        ("idx_transactions_reference", "transactions", "reference"),
    )
    for name, table, columns in baseline_indexes:
        connection.execute(
            f"CREATE INDEX IF NOT EXISTS {name} ON {table}({columns})"
        )

    valid_link = """EXISTS (
      SELECT 1 FROM canonical_events AS cash
      JOIN canonical_events AS trade ON trade.id=NEW.trade_event_id
      WHERE cash.id=NEW.cash_event_id
        AND cash.event_type='cash'
        AND cash.flow_type='investment'
        AND cash.status='confirmed'
        AND trade.event_type='trade'
        AND trade.status='confirmed'
        AND cash.account_id=trade.account_id
        AND cash.currency=trade.currency
        AND cash.occurred_on>=trade.occurred_on
    )"""
    for operation in ("insert", "update"):
        connection.execute(f"DROP TRIGGER IF EXISTS trade_cash_links_valid_{operation}")
        update_columns = (
            " OF cash_event_id, trade_event_id, match_method"
            if operation == "update"
            else ""
        )
        connection.execute(
            f"""CREATE TRIGGER trade_cash_links_valid_{operation}
                BEFORE {operation.upper()}{update_columns} ON trade_cash_links
                WHEN NOT ({valid_link})
                BEGIN
                  SELECT RAISE(ABORT, 'invalid trade cash link');
                END"""
        )

    connection.execute("DROP TRIGGER IF EXISTS canonical_events_trade_cash_identity_update")
    connection.execute(
        """CREATE TRIGGER canonical_events_trade_cash_identity_update
           BEFORE UPDATE OF account_id, event_type, occurred_on, currency,
             amount_minor, flow_type, component, security_id, side, quantity,
             price_minor, fees_minor, status
           ON canonical_events
           WHEN (
             NEW.account_id IS NOT OLD.account_id
             OR NEW.event_type IS NOT OLD.event_type
             OR NEW.occurred_on IS NOT OLD.occurred_on
             OR NEW.currency IS NOT OLD.currency
             OR NEW.amount_minor IS NOT OLD.amount_minor
             OR NEW.flow_type IS NOT OLD.flow_type
             OR NEW.component IS NOT OLD.component
             OR NEW.security_id IS NOT OLD.security_id
             OR NEW.side IS NOT OLD.side
             OR NEW.quantity IS NOT OLD.quantity
             OR NEW.price_minor IS NOT OLD.price_minor
             OR NEW.fees_minor IS NOT OLD.fees_minor
             OR NEW.status IS NOT OLD.status
           ) AND (
             EXISTS (
               SELECT 1 FROM trade_cash_links WHERE cash_event_id=OLD.id
             ) OR EXISTS (
               SELECT 1 FROM trade_cash_links WHERE trade_event_id=OLD.id
             )
           )
           BEGIN
             SELECT RAISE(ABORT, 'linked canonical event economics are immutable');
           END"""
    )

    pointer_condition = """NEW.canonical_event_id IS NOT NULL
      AND (
        NEW.source_event_id IS NULL
        OR (
          EXISTS (
            SELECT 1 FROM event_evidence
             WHERE source_event_id=NEW.source_event_id
          )
          AND (
            (SELECT COUNT(*) FROM event_evidence
              WHERE source_event_id=NEW.source_event_id) != 1
            OR NOT EXISTS (
              SELECT 1 FROM event_evidence
               WHERE source_event_id=NEW.source_event_id
                 AND canonical_event_id=NEW.canonical_event_id
                 AND evidence_role!='aggregate'
            )
          )
        )
      )"""
    for table in ("transactions", "trades"):
        for operation in ("insert", "update"):
            name = f"{table}_event_pointer_{operation}"
            connection.execute(f"DROP TRIGGER IF EXISTS {name}")
            update_columns = (
                " OF source_event_id, canonical_event_id"
                if operation == "update"
                else ""
            )
            connection.execute(
                f"""CREATE TRIGGER {name}
                    BEFORE {operation.upper()}{update_columns} ON {table}
                    WHEN {pointer_condition}
                    BEGIN
                      SELECT RAISE(ABORT, 'raw event pointer contradicts evidence');
                    END"""
            )

    evidence_pointer_conflict = """EXISTS (
      SELECT 1 FROM transactions AS fact
       WHERE fact.source_event_id=NEW.source_event_id
         AND fact.canonical_event_id IS NOT NULL
         AND (
           NEW.evidence_role='aggregate'
           OR fact.canonical_event_id IS NOT NEW.canonical_event_id
         )
      UNION ALL
      SELECT 1 FROM trades AS fact
       WHERE fact.source_event_id=NEW.source_event_id
         AND fact.canonical_event_id IS NOT NULL
         AND (
           NEW.evidence_role='aggregate'
           OR fact.canonical_event_id IS NOT NEW.canonical_event_id
         )
    )"""
    for operation in ("insert", "update"):
        name = f"event_evidence_raw_pointer_{operation}"
        connection.execute(f"DROP TRIGGER IF EXISTS {name}")
        update_columns = (
            " OF canonical_event_id, source_event_id, evidence_role"
            if operation == "update"
            else ""
        )
        connection.execute(
            f"""CREATE TRIGGER {name}
                BEFORE {operation.upper()}{update_columns} ON event_evidence
                WHEN {evidence_pointer_conflict}
                BEGIN
                  SELECT RAISE(ABORT, 'event evidence contradicts raw pointer');
                END"""
        )
