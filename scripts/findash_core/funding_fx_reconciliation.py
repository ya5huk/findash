"""Guarded reconciliation of bank funding followed by brokerage FX."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
import sqlite3
from typing import Any

from .categories import flow_type_for
from .db import connect_db
from .errors import AccountingError
from .market_data import FxBook
from .migrations import require_relationship_integrity


OPERATION = "reconcile_funding_fx"
MATCH_METHOD = "reviewed-bank-outflow+documented-brokerage-fx"
DOCUMENT_PROVIDER = "document-ledger"
MAX_DOCUMENT_RATE_ROUNDING_MINOR = Decimal(2)


class FundingFxReconciliationError(ValueError):
    """The proposed transfer/conversion relationship is not fully proven."""


@dataclass(frozen=True)
class FundingFxRequest:
    bank_transaction_id: int
    fx_destination_transaction_id: int
    brokerage_account_id: int


def _positive_id(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise FundingFxReconciliationError(f"{label} must be a positive integer")
    return value


def parse_funding_fx_request(payload: Any) -> FundingFxRequest:
    required = {
        "operation",
        "bank_transaction_id",
        "fx_destination_transaction_id",
        "brokerage_account_id",
        "confirmed_same_owned_transfer",
        "confirmed_complete_conversion",
    }
    if not isinstance(payload, dict) or set(payload) != required:
        raise FundingFxReconciliationError(
            "funding FX reconciliation request has an invalid shape"
        )
    if payload["operation"] != OPERATION:
        raise FundingFxReconciliationError(
            "funding FX reconciliation operation is not allowlisted"
        )
    if (
        payload["confirmed_same_owned_transfer"] is not True
        or payload["confirmed_complete_conversion"] is not True
    ):
        raise FundingFxReconciliationError(
            "all funding FX reconciliation confirmations are required"
        )
    return FundingFxRequest(
        bank_transaction_id=_positive_id(
            payload["bank_transaction_id"], "bank transaction id"
        ),
        fx_destination_transaction_id=_positive_id(
            payload["fx_destination_transaction_id"],
            "FX destination transaction id",
        ),
        brokerage_account_id=_positive_id(
            payload["brokerage_account_id"], "brokerage account id"
        ),
    )


def _document_feed(connection: sqlite3.Connection, account_id: int) -> int:
    row = connection.execute(
        """SELECT id FROM account_feeds
            WHERE account_id=? AND provider=? AND external_account_ref IS NULL""",
        (account_id, DOCUMENT_PROVIDER),
    ).fetchone()
    if row is not None:
        return int(row["id"])
    return int(
        connection.execute(
            """INSERT INTO account_feeds(account_id,provider,feed_type,label)
               VALUES (?,?,'document','Normalized document ledger')
               RETURNING id""",
            (account_id, DOCUMENT_PROVIDER),
        ).fetchone()[0]
    )


def _canonicalize_transaction(
    connection: sqlite3.Connection,
    transaction: sqlite3.Row,
    *,
    feed_id: int,
    source_key: str,
    category: str,
    flow_type: str,
    component: str | None,
    amount_minor: int | None = None,
) -> int:
    amount = int(
        transaction["amount_minor"] if amount_minor is None else amount_minor
    )
    source = int(
        connection.execute(
            """INSERT INTO source_events(
                   account_feed_id,source_key,event_type,occurred_on,source_doc_id
               ) VALUES (?,?,'cash',?,?) RETURNING id""",
            (
                feed_id,
                source_key,
                transaction["date"],
                transaction["source_doc_id"],
            ),
        ).fetchone()[0]
    )
    canonical = int(
        connection.execute(
            """INSERT INTO canonical_events(
                   account_id,event_type,occurred_on,currency,amount_minor,
                   flow_type,category,component,status
               ) VALUES (?,'cash',?,?,?,?,?,?,'confirmed') RETURNING id""",
            (
                transaction["account_id"],
                transaction["date"],
                transaction["currency"],
                amount,
                flow_type,
                category,
                component,
            ),
        ).fetchone()[0]
    )
    connection.execute(
        """INSERT INTO event_evidence(
               canonical_event_id,source_event_id,evidence_role,match_method,
               confidence
           ) VALUES (?,?,'primary','document-row',1.0)""",
        (canonical, source),
    )
    connection.execute(
        """UPDATE transactions
              SET amount_minor=?,category=?,flow_type=?,component=?,
                  source_event_id=?,canonical_event_id=?
            WHERE id=?""",
        (
            amount,
            category,
            flow_type,
            component,
            source,
            canonical,
            transaction["id"],
        ),
    )
    return canonical


def _load_request_rows(
    connection: sqlite3.Connection, request: FundingFxRequest
) -> tuple[sqlite3.Row, sqlite3.Row, sqlite3.Row]:
    bank = connection.execute(
        """SELECT raw.*,account.kind AS account_kind
             FROM transactions AS raw
             JOIN accounts AS account ON account.id=raw.account_id
            WHERE raw.id=?""",
        (request.bank_transaction_id,),
    ).fetchone()
    destination = connection.execute(
        """SELECT raw.*,account.kind AS account_kind,
                  document.doc_type,document.doc_date
             FROM transactions AS raw
             JOIN accounts AS account ON account.id=raw.account_id
             LEFT JOIN documents AS document
               ON document.id=raw.source_doc_id
            WHERE raw.id=?""",
        (request.fx_destination_transaction_id,),
    ).fetchone()
    brokerage = connection.execute(
        "SELECT * FROM accounts WHERE id=?", (request.brokerage_account_id,)
    ).fetchone()
    if bank is None or destination is None or brokerage is None:
        raise FundingFxReconciliationError("reviewed funding FX evidence is missing")
    return bank, destination, brokerage


def _validate_economics(
    connection: sqlite3.Connection,
    request: FundingFxRequest,
    bank: sqlite3.Row,
    destination: sqlite3.Row,
    brokerage: sqlite3.Row,
) -> None:
    bank_kind = flow_type_for(
        bank["category"], bank["flow_type"], bank["amount_minor"]
    )
    document_type = (
        str(destination["doc_type"] or "").strip().casefold().replace("-", "_")
    )
    day_gap = (
        date.fromisoformat(str(destination["date"]))
        - date.fromisoformat(str(bank["date"]))
    ).days
    if (
        bank["account_kind"] == "brokerage"
        or int(bank["amount_minor"]) >= 0
        or bank_kind != "transfer"
        or bank["source_doc_id"] is None
        or bank["source_event_id"] is not None
        or bank["canonical_event_id"] is not None
        or destination["account_kind"] != "brokerage"
        or int(destination["account_id"]) != request.brokerage_account_id
        or int(destination["amount_minor"]) <= 0
        or destination["source_doc_id"] is None
        or destination["source_event_id"] is not None
        or destination["canonical_event_id"] is not None
        or document_type not in {"fx_conversion", "brokerage_fx_conversion"}
        or destination["doc_date"] != destination["date"]
        or destination["currency"] == bank["currency"]
        or day_gap < 0
        or day_gap > 3
        or brokerage["kind"] != "brokerage"
    ):
        raise FundingFxReconciliationError(
            "rows do not describe one bank transfer followed by brokerage FX"
        )
    if brokerage["opened_on"] and (
        bank["date"] < brokerage["opened_on"]
        or destination["date"] < brokerage["opened_on"]
    ):
        raise FundingFxReconciliationError(
            "funding predates the brokerage account lifetime"
        )
    if brokerage["closed_on"] and (
        bank["date"] >= brokerage["closed_on"]
        or destination["date"] >= brokerage["closed_on"]
    ):
        raise FundingFxReconciliationError(
            "funding falls outside the brokerage account lifetime"
        )
    try:
        fx = FxBook(connection)
        point = fx.latest(
            str(destination["currency"]),
            str(bank["currency"]),
            str(destination["date"]),
        )
        converted = fx.convert_minor(
            int(destination["amount_minor"]),
            str(destination["currency"]),
            str(bank["currency"]),
            str(destination["date"]),
        )
    except AccountingError as exc:
        raise FundingFxReconciliationError(
            "documented FX rate is missing or invalid"
        ) from exc
    if (
        point.date != destination["date"]
        or point.source.strip().casefold() != "document"
        or abs(converted - Decimal(-int(bank["amount_minor"])))
        > MAX_DOCUMENT_RATE_ROUNDING_MINOR
    ):
        raise FundingFxReconciliationError(
            "documented FX does not explain the reviewed bank transfer"
        )


def reconcile_funding_fx(database: Path, payload: Any) -> dict[str, int]:
    """Atomically normalize and link one funded brokerage FX conversion."""

    request = parse_funding_fx_request(payload)
    connection = connect_db(database)
    connection.row_factory = sqlite3.Row
    try:
        with connection:
            bank, destination, brokerage = _load_request_rows(connection, request)
            existing = connection.execute(
                """SELECT 1 FROM funding_fx_links AS link
                   WHERE link.bank_outflow_event_id=?
                     AND link.fx_destination_event_id=?""",
                (bank["canonical_event_id"], destination["canonical_event_id"]),
            ).fetchone()
            if existing is not None:
                return {
                    "funding_inflows_created": 0,
                    "fx_source_legs_created": 0,
                    "links_created": 0,
                }
            _validate_economics(connection, request, bank, destination, brokerage)
            bank_feed = _document_feed(connection, int(bank["account_id"]))
            brokerage_feed = _document_feed(
                connection, request.brokerage_account_id
            )
            bank_event = _canonicalize_transaction(
                connection,
                bank,
                feed_id=bank_feed,
                source_key=f"document:{bank['source_doc_id']}:transaction:{bank['id']}",
                category="transfer",
                flow_type="transfer",
                component=bank["component"],
            )
            destination_event = _canonicalize_transaction(
                connection,
                destination,
                feed_id=brokerage_feed,
                source_key=(
                    f"document:{destination['source_doc_id']}:"
                    f"transaction:{destination['id']}"
                ),
                category="fx",
                flow_type="investment",
                component=f"cash_{str(destination['currency']).lower()}",
            )
            source_cursor = connection.execute(
                """INSERT INTO transactions(
                       account_id,date,amount_minor,currency,category,component,
                       reference,description,source_doc_id,flow_type
                   ) VALUES (?,?,?,?,?,?,?,?,?,'investment') RETURNING *""",
                (
                    request.brokerage_account_id,
                    destination["date"],
                    int(bank["amount_minor"]),
                    bank["currency"],
                    "fx",
                    f"cash_{str(bank['currency']).lower()}",
                    destination["reference"],
                    "Source-currency leg of documented brokerage FX conversion",
                    destination["source_doc_id"],
                ),
            ).fetchone()
            source_event = _canonicalize_transaction(
                connection,
                source_cursor,
                feed_id=brokerage_feed,
                source_key=(
                    f"document:{destination['source_doc_id']}:fx-source:"
                    f"{destination['id']}"
                ),
                category="fx",
                flow_type="investment",
                component=f"cash_{str(bank['currency']).lower()}",
            )
            inflow_event = int(
                connection.execute(
                    """INSERT INTO canonical_events(
                           account_id,event_type,occurred_on,currency,
                           amount_minor,flow_type,category,component,status,notes
                       ) VALUES (?,'cash',?,?,?,'transfer','deposit',?,
                                 'confirmed',?) RETURNING id""",
                    (
                        request.brokerage_account_id,
                        bank["date"],
                        bank["currency"],
                        -int(bank["amount_minor"]),
                        f"cash_{str(bank['currency']).lower()}",
                        "Reviewed brokerage counterpart of a bank transfer, "
                        "corroborated by a complete documented FX conversion.",
                    ),
                ).fetchone()[0]
            )
            connection.execute(
                """INSERT INTO funding_fx_links(
                       bank_outflow_event_id,brokerage_inflow_event_id,
                       fx_source_event_id,fx_destination_event_id,match_method
                   ) VALUES (?,?,?,?,?)""",
                (
                    bank_event,
                    inflow_event,
                    source_event,
                    destination_event,
                    MATCH_METHOD,
                ),
            )
            require_relationship_integrity(connection)
        return {
            "funding_inflows_created": 1,
            "fx_source_legs_created": 1,
            "links_created": 1,
        }
    finally:
        connection.close()


__all__ = [
    "FundingFxReconciliationError",
    "parse_funding_fx_request",
    "reconcile_funding_fx",
]
