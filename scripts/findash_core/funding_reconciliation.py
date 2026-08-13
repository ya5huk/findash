"""Guarded inference of brokerage funding from bank and cash-snapshot evidence."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
import sqlite3
from typing import Any

from .accounting import AccountingEngine
from .db import connect_db
from .errors import AccountingError
from .migrations import require_relationship_integrity


OPERATION = "infer_brokerage_funding"
MAX_OUTFLOWS = 100
MATCH_METHOD = "bank-outflow+complete-brokerage-cash-residual"


class FundingReconciliationError(ValueError):
    """The proposed funding inference is incomplete, ambiguous, or inconsistent."""


@dataclass(frozen=True)
class FundingReconciliationRequest:
    outflow_event_ids: tuple[int, ...]
    brokerage_account_id: int
    before_balance_id: int
    after_balance_id: int


def _positive_id(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise FundingReconciliationError(f"{label} must be a positive integer")
    return value


def parse_funding_reconciliation_request(
    payload: Any,
) -> FundingReconciliationRequest:
    """Validate a closed request shape without echoing private identifiers."""

    required = {
        "operation",
        "outflow_event_ids",
        "brokerage_account_id",
        "before_balance_id",
        "after_balance_id",
        "confirmed_bank_destination",
        "confirmed_no_competing_cash_explanation",
        "confirmed_complete_snapshot_pair",
    }
    if not isinstance(payload, dict) or set(payload) != required:
        raise FundingReconciliationError(
            "funding reconciliation request has an invalid shape"
        )
    if payload["operation"] != OPERATION:
        raise FundingReconciliationError(
            "funding reconciliation operation is not allowlisted"
        )
    if any(
        payload[name] is not True
        for name in (
            "confirmed_bank_destination",
            "confirmed_no_competing_cash_explanation",
            "confirmed_complete_snapshot_pair",
        )
    ):
        raise FundingReconciliationError(
            "all funding reconciliation confirmations are required"
        )
    raw_outflows = payload["outflow_event_ids"]
    if (
        not isinstance(raw_outflows, list)
        or not raw_outflows
        or len(raw_outflows) > MAX_OUTFLOWS
    ):
        raise FundingReconciliationError(
            "outflow event ids must be a non-empty bounded list"
        )
    outflows = tuple(
        _positive_id(value, "outflow event id") for value in raw_outflows
    )
    if len(outflows) != len(set(outflows)):
        raise FundingReconciliationError("outflow event ids contain duplicates")
    return FundingReconciliationRequest(
        outflow_event_ids=outflows,
        brokerage_account_id=_positive_id(
            payload["brokerage_account_id"], "brokerage account id"
        ),
        before_balance_id=_positive_id(
            payload["before_balance_id"], "before balance id"
        ),
        after_balance_id=_positive_id(
            payload["after_balance_id"], "after balance id"
        ),
    )


def _snapshot_pair(
    connection: sqlite3.Connection, request: FundingReconciliationRequest
) -> tuple[sqlite3.Row, sqlite3.Row]:
    before = connection.execute(
        "SELECT * FROM balances WHERE id=?", (request.before_balance_id,)
    ).fetchone()
    after = connection.execute(
        "SELECT * FROM balances WHERE id=?", (request.after_balance_id,)
    ).fetchone()
    if before is None or after is None:
        raise FundingReconciliationError("snapshot pair is missing")
    expected_component = f"cash_{str(before['currency']).lower()}"
    if (
        before["account_id"] != request.brokerage_account_id
        or after["account_id"] != request.brokerage_account_id
        or before["currency"] != after["currency"]
        or before["component"] != expected_component
        or after["component"] != expected_component
        or before["as_of"] >= after["as_of"]
    ):
        raise FundingReconciliationError(
            "snapshot pair does not identify one chronological brokerage cash component"
        )
    shared_complete_feed = connection.execute(
        """SELECT 1
             FROM balance_evidence AS before_evidence
             JOIN balance_evidence AS after_evidence
               ON after_evidence.account_feed_id=before_evidence.account_feed_id
             JOIN account_feeds AS feed ON feed.id=before_evidence.account_feed_id
            WHERE before_evidence.canonical_balance_id=?
              AND after_evidence.canonical_balance_id=?
              AND before_evidence.account_snapshot_complete=1
              AND after_evidence.account_snapshot_complete=1
              AND feed.account_id=? AND feed.is_active=1
            LIMIT 1""",
        (
            request.before_balance_id,
            request.after_balance_id,
            request.brokerage_account_id,
        ),
    ).fetchone()
    if shared_complete_feed is None:
        raise FundingReconciliationError(
            "snapshot pair lacks complete evidence from one active brokerage feed"
        )
    return before, after


def _outflows(
    connection: sqlite3.Connection,
    request: FundingReconciliationRequest,
    before: sqlite3.Row,
    after: sqlite3.Row,
) -> list[sqlite3.Row]:
    rows: list[sqlite3.Row] = []
    for event_id in request.outflow_event_ids:
        row = connection.execute(
            """SELECT event.*,account.kind AS account_kind,
                      account.opened_on AS account_opened_on,
                      account.closed_on AS account_closed_on,
                      EXISTS (
                        SELECT 1 FROM event_evidence
                         WHERE canonical_event_id=event.id
                      ) AS has_evidence
                 FROM canonical_events AS event
                 JOIN accounts AS account ON account.id=event.account_id
                WHERE event.id=?""",
            (event_id,),
        ).fetchone()
        if (
            row is None
            or row["event_type"] != "cash"
            or row["flow_type"] != "transfer"
            or row["status"] != "confirmed"
            or row["amount_minor"] >= 0
            or row["currency"] != before["currency"]
            or row["account_kind"] == "brokerage"
            or not row["has_evidence"]
            or (
                row["account_opened_on"] is not None
                and row["occurred_on"] < row["account_opened_on"]
            )
            or (
                row["account_closed_on"] is not None
                and row["occurred_on"] >= row["account_closed_on"]
            )
            or not (before["as_of"] < row["occurred_on"] <= after["as_of"])
        ):
            raise FundingReconciliationError(
                "an outflow lacks confirmed bank-transfer evidence inside the snapshot interval"
            )
        rows.append(row)
    return rows


def _existing_links(
    connection: sqlite3.Connection, request: FundingReconciliationRequest
) -> int:
    placeholders = ",".join("?" for _ in request.outflow_event_ids)
    rows = connection.execute(
        f"""SELECT link.before_balance_id,link.after_balance_id,
                   inflow.account_id
              FROM inferred_transfer_links AS link
              JOIN canonical_events AS inflow ON inflow.id=link.inflow_event_id
             WHERE link.outflow_event_id IN ({placeholders})""",
        request.outflow_event_ids,
    ).fetchall()
    if not rows:
        return 0
    if len(rows) != len(request.outflow_event_ids) or any(
        row["before_balance_id"] != request.before_balance_id
        or row["after_balance_id"] != request.after_balance_id
        or row["account_id"] != request.brokerage_account_id
        for row in rows
    ):
        raise FundingReconciliationError(
            "outflow events already participate in a different inference"
        )
    return len(rows)


def infer_brokerage_funding(database: Path, payload: Any) -> dict[str, int]:
    """Validate one residual and atomically create its brokerage transfer legs."""

    request = parse_funding_reconciliation_request(payload)
    connection = connect_db(database)
    connection.row_factory = sqlite3.Row
    try:
        with connection:
            existing = _existing_links(connection, request)
            if existing:
                return {"inflows_created": 0, "links_created": 0}

            account = connection.execute(
                "SELECT * FROM accounts WHERE id=?",
                (request.brokerage_account_id,),
            ).fetchone()
            if account is None or account["kind"] != "brokerage":
                raise FundingReconciliationError(
                    "funding destination is not a brokerage account"
                )
            before, after = _snapshot_pair(connection, request)
            outflows = _outflows(connection, request, before, after)
            if any(
                (
                    account["opened_on"] is not None
                    and row["occurred_on"] < account["opened_on"]
                )
                or (
                    account["closed_on"] is not None
                    and row["occurred_on"] >= account["closed_on"]
                )
                for row in outflows
            ):
                raise FundingReconciliationError(
                    "funding event falls outside the brokerage account lifetime"
                )

            try:
                known_movement, derived_trade_count = (
                    AccountingEngine(connection).brokerage_cash_movement_between(
                        request.brokerage_account_id,
                        str(before["currency"]),
                        str(before["as_of"]),
                        str(after["as_of"]),
                    )
                )
            except AccountingError as exc:
                raise FundingReconciliationError(
                    "brokerage cash interval cannot be reconstructed"
                ) from exc
            residual = (
                Decimal(after["amount_minor"])
                - Decimal(before["amount_minor"])
                - known_movement
            )
            expected = sum(
                (-Decimal(row["amount_minor"]) for row in outflows), Decimal(0)
            )
            tolerance = Decimal(max(1, derived_trade_count))
            if expected <= 0 or abs(residual - expected) > tolerance:
                raise FundingReconciliationError(
                    "bank transfers do not explain the brokerage cash residual"
                )

            for outflow in sorted(
                outflows, key=lambda row: (row["occurred_on"], row["id"])
            ):
                cursor = connection.execute(
                    """INSERT INTO canonical_events(
                           account_id,event_type,occurred_on,currency,amount_minor,
                           flow_type,category,component,status,notes
                       ) VALUES (?,'cash',?,?,?,'transfer','deposit',?,'confirmed',?)""",
                    (
                        request.brokerage_account_id,
                        outflow["occurred_on"],
                        outflow["currency"],
                        -int(outflow["amount_minor"]),
                        f"cash_{str(outflow['currency']).lower()}",
                        "Inferred from a reviewed bank outflow and complete "
                        "brokerage cash-snapshot reconciliation.",
                    ),
                )
                inflow_id = int(cursor.lastrowid)
                connection.execute(
                    """INSERT INTO inferred_transfer_links(
                           outflow_event_id,inflow_event_id,before_balance_id,
                           after_balance_id,match_method
                       ) VALUES (?,?,?,?,?)""",
                    (
                        outflow["id"],
                        inflow_id,
                        request.before_balance_id,
                        request.after_balance_id,
                        MATCH_METHOD,
                    ),
                )
            require_relationship_integrity(connection)
        return {
            "inflows_created": len(outflows),
            "links_created": len(outflows),
        }
    finally:
        connection.close()


__all__ = [
    "FundingReconciliationError",
    "infer_brokerage_funding",
    "parse_funding_reconciliation_request",
]
