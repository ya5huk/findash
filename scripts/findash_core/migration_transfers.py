"""Evidence-preserving inferred account-transfer migration (version 14)."""

from __future__ import annotations

import sqlite3


def _migration_14_inferred_account_transfers(
    connection: sqlite3.Connection,
) -> None:
    """Link a bank outflow to a snapshot-proven brokerage inflow."""

    connection.execute(
        """CREATE TABLE IF NOT EXISTS inferred_transfer_links (
             outflow_event_id  INTEGER PRIMARY KEY
                                 REFERENCES canonical_events(id) ON DELETE RESTRICT,
             inflow_event_id   INTEGER NOT NULL UNIQUE
                                 REFERENCES canonical_events(id) ON DELETE RESTRICT,
             before_balance_id INTEGER NOT NULL
                                 REFERENCES balances(id) ON DELETE RESTRICT,
             after_balance_id  INTEGER NOT NULL
                                 REFERENCES balances(id) ON DELETE RESTRICT,
             match_method      TEXT NOT NULL
                                 CHECK (length(trim(match_method)) > 0),
             created_at        TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
             CHECK (before_balance_id <> after_balance_id)
           )"""
    )

    valid_link = """EXISTS (
      SELECT 1
        FROM canonical_events AS outflow
        JOIN canonical_events AS inflow ON inflow.id=NEW.inflow_event_id
        JOIN accounts AS source_account ON source_account.id=outflow.account_id
        JOIN accounts AS destination_account ON destination_account.id=inflow.account_id
        JOIN balances AS before_balance ON before_balance.id=NEW.before_balance_id
        JOIN balances AS after_balance ON after_balance.id=NEW.after_balance_id
       WHERE outflow.id=NEW.outflow_event_id
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
               ON after_evidence.account_feed_id=before_evidence.account_feed_id
             JOIN account_feeds AS feed
               ON feed.id=before_evidence.account_feed_id
            WHERE before_evidence.canonical_balance_id=before_balance.id
              AND after_evidence.canonical_balance_id=after_balance.id
              AND before_evidence.account_snapshot_complete=1
              AND after_evidence.account_snapshot_complete=1
              AND feed.account_id=inflow.account_id
              AND feed.is_active=1
         )
    )"""
    for operation in ("insert", "update"):
        update_columns = (
            " OF outflow_event_id, inflow_event_id, before_balance_id, "
            "after_balance_id, match_method"
            if operation == "update"
            else ""
        )
        connection.execute(
            f"""CREATE TRIGGER IF NOT EXISTS inferred_transfer_links_valid_{operation}
                BEFORE {operation.upper()}{update_columns}
                ON inferred_transfer_links
                WHEN NOT ({valid_link})
                BEGIN
                  SELECT RAISE(ABORT, 'invalid inferred transfer link');
                END"""
        )

    connection.execute(
        """CREATE TRIGGER IF NOT EXISTS canonical_events_inferred_transfer_identity_update
           BEFORE UPDATE OF account_id,event_type,occurred_on,currency,
             amount_minor,flow_type,component,status
           ON canonical_events
           WHEN (
             NEW.account_id IS NOT OLD.account_id
             OR NEW.event_type IS NOT OLD.event_type
             OR NEW.occurred_on IS NOT OLD.occurred_on
             OR NEW.currency IS NOT OLD.currency
             OR NEW.amount_minor IS NOT OLD.amount_minor
             OR NEW.flow_type IS NOT OLD.flow_type
             OR NEW.component IS NOT OLD.component
             OR NEW.status IS NOT OLD.status
           ) AND (
             EXISTS (
               SELECT 1 FROM inferred_transfer_links
                WHERE outflow_event_id=OLD.id OR inflow_event_id=OLD.id
             )
           )
           BEGIN
             SELECT RAISE(ABORT, 'linked inferred transfer is immutable');
           END"""
    )
    connection.execute(
        """CREATE TRIGGER IF NOT EXISTS balances_inferred_transfer_identity_update
           BEFORE UPDATE OF account_id,as_of,component,amount_minor,currency
           ON balances
           WHEN (
             NEW.account_id IS NOT OLD.account_id
             OR NEW.as_of IS NOT OLD.as_of
             OR NEW.component IS NOT OLD.component
             OR NEW.amount_minor IS NOT OLD.amount_minor
             OR NEW.currency IS NOT OLD.currency
           ) AND EXISTS (
             SELECT 1 FROM inferred_transfer_links
              WHERE before_balance_id=OLD.id OR after_balance_id=OLD.id
           )
           BEGIN
             SELECT RAISE(ABORT, 'inferred transfer snapshot is immutable');
           END"""
    )
