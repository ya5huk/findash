"""Evidence-based classification for brokerage cash movements."""

from __future__ import annotations

import sqlite3
from typing import Iterable, Mapping

FX_CONVERSION_DOC_TYPES = frozenset(
    {"fx_conversion", "brokerage_fx_conversion"}
)


def fx_conversion_documents(
    connection: sqlite3.Connection,
    *,
    as_of: str | None = None,
) -> dict[int, dict]:
    """Return documents that explicitly evidence internal brokerage FX.

    The exact normalized document type is the durable signal.  The vault folder
    remains a compatibility signal for older ingests whose type was less precise.
    """

    rows = connection.execute(
        """SELECT id,doc_date,doc_type,drive_path
             FROM documents
            WHERE (? IS NULL OR doc_date<=?)""",
        (as_of, as_of),
    ).fetchall()
    result: dict[int, dict] = {}
    for row in rows:
        doc_type = str(row["doc_type"] or "").strip().casefold().replace("-", "_")
        drive_path = str(row["drive_path"] or "")
        if doc_type in FX_CONVERSION_DOC_TYPES or drive_path.startswith(
            "fx-conversions/"
        ):
            result[int(row["id"])] = dict(row)
    return result


def is_internal_fx_transaction(
    transaction: Mapping,
    fx_document_ids: Iterable[int],
) -> bool:
    """Whether a cash row is one leg of an internal brokerage conversion."""

    source_doc_id = transaction.get("source_doc_id")
    if source_doc_id is not None and int(source_doc_id) in fx_document_ids:
        return True
    category = str(transaction.get("category") or "").strip().casefold()
    if category == "fx":
        # Within a brokerage, an FX-labelled cash leg is categorically an
        # internal reallocation. A contradictory legacy flow_type must not turn
        # it into external capital and corrupt performance statistics.
        return True
    return False


__all__ = [
    "FX_CONVERSION_DOC_TYPES",
    "fx_conversion_documents",
    "is_internal_fx_transaction",
]
