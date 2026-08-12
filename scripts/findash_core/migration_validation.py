"""Fact validation and identity-guard migrations (versions 3-7)."""

from __future__ import annotations

import sqlite3

from .migration_support import (
    MigrationError,
    _add_column,
    relationship_violation_counts,
)


def _date_check(column: str) -> str:
    value = f"NEW.{column}"
    return (
        f"typeof({value})='text' AND length({value})=10 "
        f"AND {value} GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]' "
        f"AND date({value})={value}"
    )


def _nullable(check: str, column: str) -> str:
    return f"NEW.{column} IS NULL OR ({check})"


def _currency_check(column: str) -> str:
    value = f"NEW.{column}"
    return f"typeof({value})='text' AND {value} GLOB '[A-Z][A-Z][A-Z]'"


def _integer_check(column: str) -> str:
    return f"typeof(NEW.{column})='integer'"


def _nonnegative_integer_check(column: str) -> str:
    return f"typeof(NEW.{column})='integer' AND NEW.{column}>=0"


def _finite_number_check(column: str, *, positive: bool = False) -> str:
    lower_bound = f"NEW.{column}>0" if positive else f"NEW.{column}>-1e999"
    return (
        f"typeof(NEW.{column}) IN ('integer','real') "
        f"AND {lower_bound} AND NEW.{column}<1e999"
    )


def _nonempty_nullable_text_check(column: str) -> str:
    value = f"NEW.{column}"
    return f"{value} IS NULL OR (typeof({value})='text' AND length(trim({value}))>0)"


def _install_validation_triggers(
    connection: sqlite3.Connection, table: str, checks: list[str]
) -> None:
    condition = " AND ".join(f"({check})" for check in checks)
    for operation in ("INSERT", "UPDATE"):
        name = f"validate_{table}_{operation.lower()}"
        connection.execute(
            f"""CREATE TRIGGER IF NOT EXISTS {name}
                BEFORE {operation} ON {table}
                WHEN COALESCE(({condition}), 0)=0
                BEGIN
                  SELECT RAISE(ABORT, 'invalid {table} value');
                END"""
        )

def _fact_validation_specs() -> tuple[tuple[str, list[str]], ...]:
    integer = _integer_check
    nonnegative_integer = _nonnegative_integer_check
    currency = _currency_check
    date_check = _date_check
    nullable = _nullable
    payslip_money = [
        "gross_minor",
        "net_minor",
        "income_tax_minor",
        "social_security_minor",
        "health_insurance_minor",
        "pension_employee_minor",
        "pension_employer_minor",
        "study_fund_employee_minor",
        "study_fund_employer_minor",
    ]
    return (
        ("documents", [nullable(date_check("doc_date"), "doc_date")]),
        (
            "accounts",
            [
                currency("currency"),
                nullable(date_check("opened_on"), "opened_on"),
                nullable(date_check("closed_on"), "closed_on"),
                nullable(date_check("liquidity_date"), "liquidity_date"),
                "NEW.opened_on IS NULL OR NEW.closed_on IS NULL "
                "OR NEW.closed_on>=NEW.opened_on",
            ],
        ),
        (
            "balances",
            [
                date_check("as_of"),
                currency("currency"),
                integer("amount_minor"),
                _nonempty_nullable_text_check("component"),
            ],
        ),
        (
            "transactions",
            [
                date_check("date"),
                nullable(date_check("value_date"), "value_date"),
                currency("currency"),
                integer("amount_minor"),
                _nonempty_nullable_text_check("component"),
            ],
        ),
        ("securities", [currency("currency")]),
        (
            "trades",
            [
                date_check("date"),
                currency("currency"),
                _finite_number_check("shares", positive=True),
                nonnegative_integer("price_minor"),
                nonnegative_integer("fees_minor"),
            ],
        ),
        (
            "canonical_events",
            [
                date_check("occurred_on"),
                currency("currency"),
                nullable(integer("amount_minor"), "amount_minor"),
                nullable(
                    _finite_number_check("quantity", positive=True), "quantity"
                ),
                nullable(nonnegative_integer("price_minor"), "price_minor"),
                nonnegative_integer("fees_minor"),
            ],
        ),
        (
            "source_events",
            [date_check("occurred_on"), "length(trim(NEW.source_key))>0"],
        ),
        ("account_feeds", ["length(trim(NEW.provider))>0"]),
        (
            "balance_evidence",
            [integer("observed_amount_minor"), currency("observed_currency")],
        ),
        (
            "positions",
            [
                date_check("as_of"),
                currency("currency"),
                _finite_number_check("quantity"),
                nullable(nonnegative_integer("avg_cost_minor"), "avg_cost_minor"),
                nullable(
                    nonnegative_integer("market_price_minor"), "market_price_minor"
                ),
                nullable(integer("market_value_minor"), "market_value_minor"),
            ],
        ),
        (
            "position_evidence",
            [
                currency("observed_currency"),
                _finite_number_check("observed_quantity"),
                nullable(
                    nonnegative_integer("observed_avg_cost_minor"),
                    "observed_avg_cost_minor",
                ),
                nullable(
                    nonnegative_integer("observed_price_minor"),
                    "observed_price_minor",
                ),
                nullable(integer("observed_value_minor"), "observed_value_minor"),
            ],
        ),
        (
            "prices",
            [
                date_check("date"),
                currency("currency"),
                nonnegative_integer("close_minor"),
            ],
        ),
        (
            "fx_rates",
            [
                date_check("date"),
                currency("base_currency"),
                currency("quote_currency"),
                _finite_number_check("rate", positive=True),
            ],
        ),
        (
            "payslips",
            [
                date_check("period_start"),
                date_check("period_end"),
                "NEW.period_end>=NEW.period_start",
                nullable(date_check("paid_on"), "paid_on"),
                currency("currency"),
                *(integer(column) for column in payslip_money),
            ],
        ),
        ("payslip_line_items", [integer("amount_minor")]),
    )


def _invalid_existing_fact_counts(
    connection: sqlite3.Connection, specs: tuple[tuple[str, list[str]], ...]
) -> list[tuple[str, int]]:
    invalid: list[tuple[str, int]] = []
    for table, checks in specs:
        condition = " AND ".join(f"({check})" for check in checks).replace(
            "NEW.", "candidate."
        )
        count = int(
            connection.execute(
                f"SELECT COUNT(*) FROM {table} AS candidate "
                f"WHERE COALESCE(({condition}), 0)=0"
            ).fetchone()[0]
        )
        if count:
            invalid.append((table, count))
    return invalid


def _migration_3_fact_validation(connection: sqlite3.Connection) -> None:
    """Validate existing facts, then guard future writes without rebuilding tables."""

    specs = _fact_validation_specs()
    invalid = _invalid_existing_fact_counts(connection, specs)
    if invalid:
        summary = ", ".join(f"{table}={count}" for table, count in invalid)
        raise MigrationError(
            f"invalid existing rows block migration v3 ({summary}); correct them and retry"
        )
    for table, checks in specs:
        _install_validation_triggers(connection, table, checks)


def _canonical_component_check() -> str:
    return (
        "NEW.component IS NULL OR (NEW.event_type='cash' "
        "AND typeof(NEW.component)='text' AND length(trim(NEW.component))>0)"
    )


def _migration_4_canonical_cash_component(connection: sqlite3.Connection) -> None:
    """Preserve the normalized sub-ledger identity of canonical cash events."""

    _add_column(connection, "canonical_events", "component", "TEXT")
    checks = dict(_fact_validation_specs())["canonical_events"] + [
        _canonical_component_check()
    ]
    invalid = _invalid_existing_fact_counts(
        connection, (("canonical_events", checks),)
    )
    if invalid:
        raise MigrationError(
            "invalid existing rows block migration v4 (canonical_events="
            f"{invalid[0][1]}); correct them and retry"
        )
    for operation in ("insert", "update"):
        connection.execute(f"DROP TRIGGER IF EXISTS validate_canonical_events_{operation}")
    _install_validation_triggers(connection, "canonical_events", checks)


def _migration_5_evidence_account_guards(connection: sqlite3.Connection) -> None:
    """Keep snapshot evidence on the same logical account as its source feed."""

    invalid: list[tuple[str, int]] = []
    for evidence_table, anchor_table, anchor_column in (
        ("balance_evidence", "balances", "canonical_balance_id"),
        ("position_evidence", "positions", "canonical_position_id"),
    ):
        count = int(
            connection.execute(
                f"""SELECT COUNT(*) FROM {evidence_table} AS evidence
                    WHERE evidence.account_feed_id IS NOT NULL
                      AND NOT EXISTS (
                        SELECT 1 FROM {anchor_table} AS anchor
                        JOIN account_feeds AS feed
                          ON feed.id=evidence.account_feed_id
                        WHERE anchor.id=evidence.{anchor_column}
                          AND anchor.account_id=feed.account_id
                      )"""
            ).fetchone()[0]
        )
        if count:
            invalid.append((evidence_table, count))
    if invalid:
        summary = ", ".join(f"{table}={count}" for table, count in invalid)
        raise MigrationError(
            f"invalid existing rows block migration v5 ({summary}); correct them and retry"
        )

    for evidence_table, anchor_table, anchor_column in (
        ("balance_evidence", "balances", "canonical_balance_id"),
        ("position_evidence", "positions", "canonical_position_id"),
    ):
        for operation in ("INSERT", "UPDATE"):
            trigger = f"{evidence_table}_same_account_{operation.lower()}"
            update_columns = (
                f" OF {anchor_column}, account_feed_id" if operation == "UPDATE" else ""
            )
            connection.execute(
                f"""CREATE TRIGGER IF NOT EXISTS {trigger}
                    BEFORE {operation}{update_columns} ON {evidence_table}
                    WHEN NEW.account_feed_id IS NOT NULL AND NOT EXISTS (
                      SELECT 1 FROM {anchor_table} AS anchor
                      JOIN account_feeds AS feed ON feed.id=NEW.account_feed_id
                      WHERE anchor.id=NEW.{anchor_column}
                        AND anchor.account_id=feed.account_id
                    )
                    BEGIN
                      SELECT RAISE(ABORT, '{evidence_table} account mismatch');
                    END"""
            )

    connection.execute(
        """CREATE TRIGGER IF NOT EXISTS account_feeds_retain_evidence
           BEFORE DELETE ON account_feeds
           WHEN EXISTS (SELECT 1 FROM source_events WHERE account_feed_id=OLD.id)
             OR EXISTS (SELECT 1 FROM balance_evidence WHERE account_feed_id=OLD.id)
             OR EXISTS (SELECT 1 FROM position_evidence WHERE account_feed_id=OLD.id)
           BEGIN
             SELECT RAISE(ABORT, 'cannot delete account feed with evidence');
           END"""
    )


def _migration_6_parent_identity_guards(connection: sqlite3.Connection) -> None:
    """Prevent parent-key edits from silently invalidating linked evidence."""

    invalid = relationship_violation_counts(
        connection, include_ledger_integrity=False
    )
    if invalid:
        summary = ", ".join(
            f"{table}={count}" for table, count in sorted(invalid.items())
        )
        raise MigrationError(
            f"invalid existing rows block migration v6 ({summary}); correct them and retry"
        )

    triggers = (
        (
            "account_feeds_identity_update",
            "account_feeds",
            "account_id",
            "NEW.account_id IS NOT OLD.account_id AND ( "
            "EXISTS (SELECT 1 FROM source_events WHERE account_feed_id=OLD.id) OR "
            "EXISTS (SELECT 1 FROM balance_evidence WHERE account_feed_id=OLD.id) OR "
            "EXISTS (SELECT 1 FROM position_evidence WHERE account_feed_id=OLD.id))",
            "account feed identity has dependent evidence",
        ),
        (
            "source_events_identity_update",
            "source_events",
            "account_feed_id, event_type",
            "(NEW.account_feed_id IS NOT OLD.account_feed_id OR "
            "NEW.event_type IS NOT OLD.event_type) AND ( "
            "EXISTS (SELECT 1 FROM event_evidence WHERE source_event_id=OLD.id) OR "
            "EXISTS (SELECT 1 FROM transactions WHERE source_event_id=OLD.id) OR "
            "EXISTS (SELECT 1 FROM trades WHERE source_event_id=OLD.id))",
            "source event identity has dependent evidence",
        ),
        (
            "canonical_events_identity_update",
            "canonical_events",
            "account_id, event_type",
            "(NEW.account_id IS NOT OLD.account_id OR "
            "NEW.event_type IS NOT OLD.event_type) AND ( "
            "EXISTS (SELECT 1 FROM event_evidence WHERE canonical_event_id=OLD.id) OR "
            "EXISTS (SELECT 1 FROM transactions WHERE canonical_event_id=OLD.id) OR "
            "EXISTS (SELECT 1 FROM trades WHERE canonical_event_id=OLD.id))",
            "canonical event identity has dependent evidence",
        ),
        (
            "balances_identity_update",
            "balances",
            "account_id",
            "NEW.account_id IS NOT OLD.account_id AND EXISTS ( "
            "SELECT 1 FROM balance_evidence WHERE canonical_balance_id=OLD.id)",
            "balance identity has dependent evidence",
        ),
        (
            "positions_identity_update",
            "positions",
            "account_id",
            "NEW.account_id IS NOT OLD.account_id AND EXISTS ( "
            "SELECT 1 FROM position_evidence WHERE canonical_position_id=OLD.id)",
            "position identity has dependent evidence",
        ),
    )
    for name, table, columns, condition, message in triggers:
        connection.execute(
            f"""CREATE TRIGGER IF NOT EXISTS {name}
                BEFORE UPDATE OF {columns} ON {table}
                WHEN {condition}
                BEGIN
                  SELECT RAISE(ABORT, '{message}');
                END"""
        )


def _migration_7_complete_identity_guards(connection: sqlite3.Connection) -> None:
    """Guard every stable parent identity and scope legacy trade ids correctly."""

    connection.execute("DROP INDEX IF EXISTS idx_trades_external")
    connection.execute(
        """CREATE UNIQUE INDEX idx_trades_external
           ON trades(account_id, external_id)
           WHERE external_id IS NOT NULL AND source_event_id IS NULL"""
    )

    for name in (
        "account_feeds_identity_update",
        "source_events_identity_update",
        "canonical_events_identity_update",
        "balances_identity_update",
        "positions_identity_update",
    ):
        connection.execute(f"DROP TRIGGER IF EXISTS {name}")

    triggers = (
        (
            "account_feeds_identity_update",
            "account_feeds",
            "account_id, provider, external_account_ref",
            "(NEW.account_id IS NOT OLD.account_id OR "
            "NEW.provider IS NOT OLD.provider OR "
            "NEW.external_account_ref IS NOT OLD.external_account_ref) AND ( "
            "EXISTS (SELECT 1 FROM source_events WHERE account_feed_id=OLD.id) OR "
            "EXISTS (SELECT 1 FROM balance_evidence WHERE account_feed_id=OLD.id) OR "
            "EXISTS (SELECT 1 FROM position_evidence WHERE account_feed_id=OLD.id))",
            "account feed identity has dependent evidence",
        ),
        (
            "source_events_identity_update",
            "source_events",
            "account_feed_id, source_key, event_type",
            "(NEW.account_feed_id IS NOT OLD.account_feed_id OR "
            "NEW.source_key IS NOT OLD.source_key OR "
            "NEW.event_type IS NOT OLD.event_type) AND ( "
            "EXISTS (SELECT 1 FROM event_evidence WHERE source_event_id=OLD.id) OR "
            "EXISTS (SELECT 1 FROM transactions WHERE source_event_id=OLD.id) OR "
            "EXISTS (SELECT 1 FROM trades WHERE source_event_id=OLD.id))",
            "source event identity has dependent evidence",
        ),
        (
            "canonical_events_identity_update",
            "canonical_events",
            "account_id, event_type",
            "(NEW.account_id IS NOT OLD.account_id OR "
            "NEW.event_type IS NOT OLD.event_type) AND ( "
            "EXISTS (SELECT 1 FROM event_evidence WHERE canonical_event_id=OLD.id) OR "
            "EXISTS (SELECT 1 FROM transactions WHERE canonical_event_id=OLD.id) OR "
            "EXISTS (SELECT 1 FROM trades WHERE canonical_event_id=OLD.id))",
            "canonical event identity has dependent evidence",
        ),
        (
            "balances_identity_update",
            "balances",
            "account_id, as_of, component",
            "(NEW.account_id IS NOT OLD.account_id OR "
            "NEW.as_of IS NOT OLD.as_of OR "
            "NEW.component IS NOT OLD.component) AND EXISTS ( "
            "SELECT 1 FROM balance_evidence WHERE canonical_balance_id=OLD.id)",
            "balance identity has dependent evidence",
        ),
        (
            "positions_identity_update",
            "positions",
            "account_id, security_id, as_of",
            "(NEW.account_id IS NOT OLD.account_id OR "
            "NEW.security_id IS NOT OLD.security_id OR "
            "NEW.as_of IS NOT OLD.as_of) AND EXISTS ( "
            "SELECT 1 FROM position_evidence WHERE canonical_position_id=OLD.id)",
            "position identity has dependent evidence",
        ),
    )
    for name, table, columns, condition, message in triggers:
        connection.execute(
            f"""CREATE TRIGGER {name}
                BEFORE UPDATE OF {columns} ON {table}
                WHEN {condition}
                BEGIN
                  SELECT RAISE(ABORT, '{message}');
                END"""
        )
