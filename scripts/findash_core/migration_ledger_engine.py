"""Ledger-engine relationships (version 16).

Own-account transfers, FX conversions and dividends each get one explicit
relationship table. The v14/v15 single-purpose funding links are folded into
them and removed.
"""

from __future__ import annotations

from decimal import Decimal
import sqlite3

from .ledger.rates import implied_rate, rate_orientation
from .ledger_relationships import (
    DIVIDEND_EVENT_VALID,
    FX_CONVERSION_VALID,
    TRANSFER_LINK_VALID,
)
from .migration_support import _table_exists


def _date_check(column: str, *, nullable: bool = False) -> str:
    rule = (
        f"typeof({column})='text' AND length({column})=10 "
        f"AND {column} GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]' "
        f"AND date({column})={column}"
    )
    return f"({column} IS NULL OR ({rule}))" if nullable else f"({rule})"


def _create_tables(connection: sqlite3.Connection) -> None:
    connection.execute(
        """CREATE TABLE IF NOT EXISTS transfer_links (
             outflow_event_id  INTEGER PRIMARY KEY
                                 REFERENCES canonical_events(id) ON DELETE RESTRICT,
             inflow_event_id   INTEGER NOT NULL UNIQUE
                                 REFERENCES canonical_events(id) ON DELETE RESTRICT,
             match_method      TEXT NOT NULL CHECK (length(trim(match_method)) > 0),
             created_at        TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
             CHECK (outflow_event_id <> inflow_event_id)
           )"""
    )
    connection.execute(
        f"""CREATE TABLE IF NOT EXISTS fx_conversions (
              id                INTEGER PRIMARY KEY,
              account_id        INTEGER NOT NULL
                                  REFERENCES accounts(id) ON DELETE RESTRICT,
              converted_on      TEXT NOT NULL,
              sell_event_id     INTEGER NOT NULL UNIQUE
                                  REFERENCES canonical_events(id) ON DELETE RESTRICT,
              buy_event_id      INTEGER NOT NULL UNIQUE
                                  REFERENCES canonical_events(id) ON DELETE RESTRICT,
              base_currency     TEXT NOT NULL,
              quote_currency    TEXT NOT NULL,
              actual_rate       REAL NOT NULL CHECK (actual_rate > 0 AND actual_rate < 1e999),
              market_rate       REAL CHECK (market_rate IS NULL
                                            OR (market_rate > 0 AND market_rate < 1e999)),
              market_rate_date  TEXT,
              match_method      TEXT NOT NULL CHECK (length(trim(match_method)) > 0),
              created_at        TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              CHECK (base_currency GLOB '[A-Z][A-Z][A-Z]'),
              CHECK (quote_currency GLOB '[A-Z][A-Z][A-Z]'),
              CHECK (base_currency <> quote_currency),
              CHECK {_date_check('converted_on')},
              CHECK {_date_check('market_rate_date', nullable=True)},
              CHECK ((market_rate IS NULL) = (market_rate_date IS NULL)),
              CHECK (market_rate_date IS NULL OR market_rate_date <= converted_on)
            )"""
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_fx_conversions_account_date "
        "ON fx_conversions(account_id, converted_on)"
    )
    connection.execute(
        f"""CREATE TABLE IF NOT EXISTS dividend_events (
              cash_event_id       INTEGER PRIMARY KEY
                                    REFERENCES canonical_events(id) ON DELETE CASCADE,
              account_id          INTEGER NOT NULL
                                    REFERENCES accounts(id) ON DELETE RESTRICT,
              security_id         INTEGER NOT NULL
                                    REFERENCES securities(id) ON DELETE RESTRICT,
              ex_date             TEXT,
              quantity            REAL CHECK (quantity IS NULL
                                              OR (quantity > 0 AND quantity < 1e999)),
              gross_minor         INTEGER CHECK (gross_minor IS NULL
                                                 OR (typeof(gross_minor)='integer'
                                                     AND gross_minor >= 0)),
              withholding_minor   INTEGER CHECK (withholding_minor IS NULL
                                                 OR (typeof(withholding_minor)='integer'
                                                     AND withholding_minor >= 0)),
              expected_net_minor  INTEGER CHECK (expected_net_minor IS NULL
                                                 OR typeof(expected_net_minor)='integer'),
              distribution_id     INTEGER
                                    REFERENCES security_distributions(id) ON DELETE SET NULL,
              created_at          TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              CHECK {_date_check('ex_date', nullable=True)}
            )"""
    )
    connection.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS uq_dividend_events_entitlement
           ON dividend_events(account_id, security_id, ex_date)
           WHERE ex_date IS NOT NULL"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS ledger_log (
             id            INTEGER PRIMARY KEY,
             applied_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
             kind          TEXT NOT NULL CHECK (kind IN ('intents','raw_sql','reconcile')),
             summary       TEXT NOT NULL CHECK (length(trim(summary)) > 0),
             request_hash  TEXT
           )"""
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_ledger_log_kind_time ON ledger_log(kind, applied_at)"
    )


def _create_guards(connection: sqlite3.Connection) -> None:
    guards = (
        (
            "transfer_links",
            "outflow_event_id, inflow_event_id, match_method",
            TRANSFER_LINK_VALID,
            "invalid transfer link",
        ),
        (
            "fx_conversions",
            "account_id, converted_on, sell_event_id, buy_event_id, "
            "base_currency, quote_currency",
            FX_CONVERSION_VALID,
            "invalid FX conversion",
        ),
        (
            "dividend_events",
            "cash_event_id, account_id",
            DIVIDEND_EVENT_VALID,
            "invalid dividend event",
        ),
    )
    for table, update_columns, predicate, message in guards:
        rule = predicate.format(row="NEW")
        for operation in ("insert", "update"):
            columns = f" OF {update_columns}" if operation == "update" else ""
            connection.execute(
                f"""CREATE TRIGGER IF NOT EXISTS {table}_valid_{operation}
                    BEFORE {operation.upper()}{columns} ON {table}
                    WHEN NOT ({rule})
                    BEGIN
                      SELECT RAISE(ABORT, '{message}');
                    END"""
            )

    # Linked legs keep their economics. Dates and review status may still move:
    # an expected leg is confirmed on the day its cash is actually observed.
    connection.execute(
        """CREATE TRIGGER IF NOT EXISTS canonical_events_transfer_identity_update
           BEFORE UPDATE OF account_id,event_type,currency,amount_minor,
             flow_type,component,status
           ON canonical_events
           WHEN (
             NEW.account_id IS NOT OLD.account_id
             OR NEW.event_type IS NOT OLD.event_type
             OR NEW.currency IS NOT OLD.currency
             OR NEW.amount_minor IS NOT OLD.amount_minor
             OR NEW.flow_type IS NOT OLD.flow_type
             OR NEW.component IS NOT OLD.component
             OR (NEW.status='excluded' AND OLD.status<>'excluded')
           ) AND EXISTS (
             SELECT 1 FROM transfer_links
              WHERE outflow_event_id=OLD.id OR inflow_event_id=OLD.id
           )
           BEGIN
             SELECT RAISE(ABORT, 'linked transfer economics are immutable');
           END"""
    )
    connection.execute(
        """CREATE TRIGGER IF NOT EXISTS canonical_events_fx_conversion_identity_update
           BEFORE UPDATE OF account_id,event_type,occurred_on,currency,
             amount_minor,flow_type,category,component,status
           ON canonical_events
           WHEN (
             NEW.account_id IS NOT OLD.account_id
             OR NEW.event_type IS NOT OLD.event_type
             OR NEW.occurred_on IS NOT OLD.occurred_on
             OR NEW.currency IS NOT OLD.currency
             OR NEW.amount_minor IS NOT OLD.amount_minor
             OR NEW.flow_type IS NOT OLD.flow_type
             OR NEW.category IS NOT OLD.category
             OR NEW.component IS NOT OLD.component
             OR (NEW.status='excluded' AND OLD.status<>'excluded')
           ) AND EXISTS (
             SELECT 1 FROM fx_conversions
              WHERE sell_event_id=OLD.id OR buy_event_id=OLD.id
           )
           BEGIN
             SELECT RAISE(ABORT, 'linked FX conversion economics are immutable');
           END"""
    )
    connection.execute(
        """CREATE TRIGGER IF NOT EXISTS canonical_events_dividend_identity_update
           BEFORE UPDATE OF account_id,event_type,currency,flow_type,category
           ON canonical_events
           WHEN (
             NEW.account_id IS NOT OLD.account_id
             OR NEW.event_type IS NOT OLD.event_type
             OR NEW.currency IS NOT OLD.currency
             OR NEW.flow_type IS NOT OLD.flow_type
             OR NEW.category IS NOT OLD.category
           ) AND EXISTS (
             SELECT 1 FROM dividend_events WHERE cash_event_id=OLD.id
           )
           BEGIN
             SELECT RAISE(ABORT, 'linked dividend identity is immutable');
           END"""
    )


def _document_rate(
    connection: sqlite3.Connection, on_date: str, base: str, quote: str
) -> Decimal | None:
    for row_base, row_quote, inverse in ((base, quote, False), (quote, base, True)):
        row = connection.execute(
            """SELECT rate FROM fx_rates
                WHERE date=? AND base_currency=? AND quote_currency=?
                  AND lower(source)='document'""",
            (on_date, row_base, row_quote),
        ).fetchone()
        if row is not None and float(row[0]) > 0:
            value = Decimal(str(row[0]))
            return Decimal(1) / value if inverse else value
    return None


def _move_funding_fx_links(connection: sqlite3.Connection) -> None:
    rows = connection.execute(
        """SELECT link.bank_outflow_event_id,link.brokerage_inflow_event_id,
                  link.match_method,
                  sell.id AS sell_id,sell.account_id,sell.occurred_on,
                  sell.currency AS sell_currency,sell.amount_minor AS sell_amount,
                  buy.id AS buy_id,buy.currency AS buy_currency,
                  buy.amount_minor AS buy_amount
             FROM funding_fx_links AS link
             JOIN canonical_events AS sell ON sell.id=link.fx_source_event_id
             JOIN canonical_events AS buy ON buy.id=link.fx_destination_event_id
            ORDER BY link.bank_outflow_event_id"""
    ).fetchall()
    for row in rows:
        (
            outflow,
            inflow,
            method,
            sell_id,
            account_id,
            converted_on,
            sell_currency,
            sell_amount,
            buy_id,
            buy_currency,
            buy_amount,
        ) = row
        connection.execute(
            """INSERT INTO transfer_links(outflow_event_id,inflow_event_id,match_method)
               VALUES (?,?,?)""",
            (outflow, inflow, method),
        )
        base, quote = rate_orientation(sell_currency, buy_currency)
        documented = _document_rate(connection, converted_on, base, quote)
        actual = documented or implied_rate(
            int(sell_amount), sell_currency, int(buy_amount), buy_currency, base
        )
        connection.execute(
            """INSERT INTO fx_conversions(
                   account_id,converted_on,sell_event_id,buy_event_id,
                   base_currency,quote_currency,actual_rate,match_method
               ) VALUES (?,?,?,?,?,?,?,?)""",
            (
                account_id,
                converted_on,
                sell_id,
                buy_id,
                base,
                quote,
                float(actual),
                method,
            ),
        )
        if documented is not None:
            # The user's rate now lives on the conversion; the day returns to
            # market reference data and is refilled by the next price refresh.
            connection.execute(
                """DELETE FROM fx_rates
                    WHERE date=? AND lower(source)='document'
                      AND ((base_currency=? AND quote_currency=?)
                           OR (base_currency=? AND quote_currency=?))""",
                (converted_on, base, quote, quote, base),
            )


def _move_inferred_transfer_links(connection: sqlite3.Connection) -> None:
    connection.execute(
        """INSERT INTO transfer_links(outflow_event_id,inflow_event_id,match_method)
           SELECT outflow_event_id,inflow_event_id,match_method
             FROM inferred_transfer_links
            ORDER BY outflow_event_id"""
    )


def _drop_retired_links(connection: sqlite3.Connection) -> None:
    for trigger in (
        "inferred_transfer_links_valid_insert",
        "inferred_transfer_links_valid_update",
        "canonical_events_inferred_transfer_identity_update",
        "balances_inferred_transfer_identity_update",
        "funding_fx_links_valid_insert",
        "funding_fx_links_valid_update",
        "canonical_events_funding_fx_identity_update",
    ):
        connection.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    connection.execute("DROP TABLE IF EXISTS funding_fx_links")
    connection.execute("DROP TABLE IF EXISTS inferred_transfer_links")


def _migration_16_ledger_engine(connection: sqlite3.Connection) -> None:
    _create_tables(connection)
    _create_guards(connection)
    if _table_exists(connection, "funding_fx_links"):
        _move_funding_fx_links(connection)
    if _table_exists(connection, "inferred_transfer_links"):
        _move_inferred_transfer_links(connection)
    _drop_retired_links(connection)
