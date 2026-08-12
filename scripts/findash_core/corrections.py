"""Fixed, audited removal of one incorrect interpreted balance.

This module does not expose general destructive SQL. The caller must identify one
reviewed balance and repeat its complete identity and provenance shape. Raw source
documents remain in place and receive a local correction note.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
import re
import sqlite3
from typing import Any

from .db import connect_db
from .migrations import require_current_schema


OPERATION = "remove_incorrect_balance"
MAX_REASON_LENGTH = 500
_CURRENCY = re.compile(r"^[A-Z]{3}$")


class CorrectionError(ValueError):
    """A reviewed correction request is incomplete, stale, or unsafe."""


@dataclass(frozen=True)
class BalanceCorrectionRequest:
    balance_id: int
    account_id: int
    as_of: str
    component: str | None
    amount_minor: int
    currency: str
    source_doc_ids: tuple[int, ...]
    evidence_count: int
    reason: str


def _exact_object(value: Any, keys: frozenset[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise CorrectionError(f"{label} has an invalid shape")
    return value


def _positive_id(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise CorrectionError(f"{label} must be a positive integer")
    return value


def _nonnegative_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CorrectionError(f"{label} must be a nonnegative integer")
    return value


def parse_balance_correction_request(payload: Any) -> BalanceCorrectionRequest:
    """Validate the closed request shape without echoing private values."""

    request = _exact_object(
        payload,
        frozenset(
            {
                "operation",
                "balance_id",
                "expected_account_id",
                "expected_as_of",
                "expected_component",
                "expected_amount_minor",
                "expected_currency",
                "expected_source_doc_ids",
                "expected_evidence_count",
                "confirmed_incorrect_historical_interpretation",
                "confirmed_source_documents_preserved",
                "reason",
            }
        ),
        "balance correction request",
    )
    if request["operation"] != OPERATION:
        raise CorrectionError("balance correction operation is not allowlisted")
    if (
        request["confirmed_incorrect_historical_interpretation"] is not True
        or request["confirmed_source_documents_preserved"] is not True
    ):
        raise CorrectionError("all balance correction confirmations are required")

    as_of = request["expected_as_of"]
    if not isinstance(as_of, str):
        raise CorrectionError("expected date must be an ISO date")
    try:
        date.fromisoformat(as_of)
    except ValueError as exc:
        raise CorrectionError("expected date must be an ISO date") from exc

    component = request["expected_component"]
    if component is not None and (
        not isinstance(component, str) or not component.strip()
    ):
        raise CorrectionError("expected component must be null or a non-empty string")

    amount_minor = request["expected_amount_minor"]
    if isinstance(amount_minor, bool) or not isinstance(amount_minor, int):
        raise CorrectionError("expected amount must be an integer")

    currency = request["expected_currency"]
    if not isinstance(currency, str) or _CURRENCY.fullmatch(currency) is None:
        raise CorrectionError("expected currency must be a three-letter ISO code")

    raw_source_doc_ids = request["expected_source_doc_ids"]
    if not isinstance(raw_source_doc_ids, list) or not raw_source_doc_ids:
        raise CorrectionError("expected source documents must be a non-empty list")
    source_doc_ids = tuple(
        _positive_id(value, "expected source document id")
        for value in raw_source_doc_ids
    )
    if len(source_doc_ids) != len(set(source_doc_ids)):
        raise CorrectionError("expected source documents contain duplicate ids")

    reason = request["reason"]
    if (
        not isinstance(reason, str)
        or not reason.strip()
        or reason != reason.strip()
        or len(reason) > MAX_REASON_LENGTH
        or "\n" in reason
        or "\r" in reason
    ):
        raise CorrectionError("correction reason must be one bounded line")

    return BalanceCorrectionRequest(
        balance_id=_positive_id(request["balance_id"], "balance id"),
        account_id=_positive_id(request["expected_account_id"], "expected account id"),
        as_of=as_of,
        component=component,
        amount_minor=amount_minor,
        currency=currency,
        source_doc_ids=tuple(sorted(source_doc_ids)),
        evidence_count=_nonnegative_int(
            request["expected_evidence_count"], "expected evidence count"
        ),
        reason=reason,
    )


def _remove_balance(
    connection: sqlite3.Connection, request: BalanceCorrectionRequest
) -> dict[str, int]:
    row = connection.execute(
        """SELECT account_id,as_of,component,amount_minor,currency,source_doc_id
           FROM balances WHERE id=?""",
        (request.balance_id,),
    ).fetchone()
    if row is None:
        raise CorrectionError("reviewed balance no longer exists")
    actual_identity = (
        int(row["account_id"]),
        str(row["as_of"]),
        row["component"],
        int(row["amount_minor"]),
        str(row["currency"]),
    )
    expected_identity = (
        request.account_id,
        request.as_of,
        request.component,
        request.amount_minor,
        request.currency,
    )
    if actual_identity != expected_identity:
        raise CorrectionError("reviewed balance identity no longer matches")

    evidence_rows = connection.execute(
        """SELECT source_doc_id FROM balance_evidence
           WHERE canonical_balance_id=? ORDER BY id""",
        (request.balance_id,),
    ).fetchall()
    if len(evidence_rows) != request.evidence_count:
        raise CorrectionError("reviewed balance evidence count no longer matches")

    source_doc_ids = {
        int(source_doc_id)
        for source_doc_id in [row["source_doc_id"], *[item[0] for item in evidence_rows]]
        if source_doc_id is not None
    }
    if tuple(sorted(source_doc_ids)) != request.source_doc_ids:
        raise CorrectionError("reviewed balance source documents no longer match")
    placeholders = ",".join("?" for _ in source_doc_ids)
    existing_documents = connection.execute(
        f"SELECT COUNT(*) FROM documents WHERE id IN ({placeholders})",
        tuple(sorted(source_doc_ids)),
    ).fetchone()[0]
    if int(existing_documents) != len(source_doc_ids):
        raise CorrectionError("reviewed source document set is incomplete")

    note = (
        f"Correction: removed an interpreted balance dated {request.as_of} because "
        f"{request.reason}. Source document retained."
    )
    for source_doc_id in sorted(source_doc_ids):
        connection.execute(
            """UPDATE documents
               SET notes=CASE
                   WHEN notes IS NULL OR notes='' THEN ?
                   ELSE notes || char(10) || ?
               END
               WHERE id=?""",
            (note, note, source_doc_id),
        )

    deleted = connection.execute(
        "DELETE FROM balances WHERE id=?", (request.balance_id,)
    ).rowcount
    if deleted != 1:
        raise CorrectionError("reviewed balance could not be removed exactly once")
    if connection.execute(
        "SELECT 1 FROM balance_evidence WHERE canonical_balance_id=?",
        (request.balance_id,),
    ).fetchone():
        raise CorrectionError("reviewed balance evidence was not removed")

    require_current_schema(connection)
    return {
        "balances_removed": 1,
        "balance_evidence_removed": len(evidence_rows),
        "documents_preserved": len(source_doc_ids),
    }


def remove_incorrect_balance(path: Path, payload: Any) -> dict[str, int]:
    """Atomically remove one fully guarded incorrect interpreted balance."""

    request = parse_balance_correction_request(payload)
    connection = connect_db(path)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("BEGIN IMMEDIATE")
        try:
            counts = _remove_balance(connection, request)
            connection.commit()
            return counts
        except CorrectionError:
            connection.rollback()
            raise
        except sqlite3.Error:
            connection.rollback()
            raise CorrectionError(
                "incorrect-balance correction failed; no changes committed"
            ) from None
    finally:
        connection.close()
