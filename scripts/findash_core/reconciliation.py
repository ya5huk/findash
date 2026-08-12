"""Fixed, audited reconciliation of duplicate logical accounts.

This module intentionally exposes one operation rather than general destructive SQL.
The caller supplies private ids in a staged JSON request after reviewing the economic
identity and every explicit resolution.  The transaction never infers a match.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date
from pathlib import Path
import sqlite3
from typing import Any

from .db import connect_db
from .migrations import relationship_violation_counts


MAX_RESOLUTIONS = 10_000
OPERATION = "merge_duplicate_logical_account"
IDENTITY_GUARDS = frozenset(
    {
        "account_feeds_identity_update",
        "source_events_identity_update",
        "canonical_events_identity_update",
        "balances_identity_update",
        "positions_identity_update",
    }
)


class ReconciliationError(ValueError):
    """A reviewed merge request is incomplete, ambiguous, or unsafe."""


@dataclass(frozen=True)
class ReconciliationRequest:
    source_account_id: int
    target_account_id: int
    event_resolutions: dict[int, tuple[int, ...]]
    balance_resolutions: dict[int, int]
    position_resolutions: dict[int, int]


def _exact_object(value: Any, keys: frozenset[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ReconciliationError(f"{label} has an invalid shape")
    return value


def _positive_id(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ReconciliationError(f"{label} must be a positive integer")
    return value


def _resolution_list(value: Any, label: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > MAX_RESOLUTIONS:
        raise ReconciliationError(f"{label} must be a bounded list")
    if not all(isinstance(item, dict) for item in value):
        raise ReconciliationError(f"{label} contains an invalid entry")
    return value


def parse_reconciliation_request(payload: Any) -> ReconciliationRequest:
    """Validate the closed request shape without echoing any private value."""

    request = _exact_object(
        payload,
        frozenset(
            {
                "operation",
                "source_account_id",
                "target_account_id",
                "confirmed_same_economic_account",
                "confirmed_reviewed_resolutions",
                "confirmed_unmapped_events_are_distinct",
                "event_resolutions",
                "balance_resolutions",
                "position_resolutions",
            }
        ),
        "reconciliation request",
    )
    if request["operation"] != OPERATION:
        raise ReconciliationError("reconciliation operation is not allowlisted")
    confirmations = (
        request["confirmed_same_economic_account"],
        request["confirmed_reviewed_resolutions"],
        request["confirmed_unmapped_events_are_distinct"],
    )
    if any(value is not True for value in confirmations):
        raise ReconciliationError("all reconciliation confirmations are required")

    source_account_id = _positive_id(
        request["source_account_id"], "source account id"
    )
    target_account_id = _positive_id(
        request["target_account_id"], "target account id"
    )
    if source_account_id == target_account_id:
        raise ReconciliationError("source and target accounts must be different")

    event_resolutions: dict[int, tuple[int, ...]] = {}
    for item in _resolution_list(request["event_resolutions"], "event resolutions"):
        row = _exact_object(
            item,
            frozenset(
                {"obsolete_canonical_event_id", "target_canonical_event_ids"}
            ),
            "event resolution",
        )
        source_id = _positive_id(
            row["obsolete_canonical_event_id"], "obsolete canonical event id"
        )
        targets_value = row["target_canonical_event_ids"]
        if not isinstance(targets_value, list) or not targets_value:
            raise ReconciliationError("event resolution requires target events")
        targets = tuple(
            _positive_id(value, "target canonical event id")
            for value in targets_value
        )
        if len(targets) != len(set(targets)):
            raise ReconciliationError("event resolution contains duplicate targets")
        if source_id in event_resolutions:
            raise ReconciliationError("event resolutions contain duplicate sources")
        event_resolutions[source_id] = targets

    def anchor_resolutions(
        value: Any, label: str, source_key: str, target_key: str
    ) -> dict[int, int]:
        result: dict[int, int] = {}
        for item in _resolution_list(value, label):
            row = _exact_object(
                item, frozenset({source_key, target_key}), f"{label} entry"
            )
            source_id = _positive_id(row[source_key], f"{label} source id")
            target_id = _positive_id(row[target_key], f"{label} target id")
            if source_id in result:
                raise ReconciliationError(f"{label} contain duplicate sources")
            result[source_id] = target_id
        return result

    return ReconciliationRequest(
        source_account_id=source_account_id,
        target_account_id=target_account_id,
        event_resolutions=event_resolutions,
        balance_resolutions=anchor_resolutions(
            request["balance_resolutions"],
            "balance resolutions",
            "source_balance_id",
            "target_balance_id",
        ),
        position_resolutions=anchor_resolutions(
            request["position_resolutions"],
            "position resolutions",
            "source_position_id",
            "target_position_id",
        ),
    )


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        is not None
    )


def _require_identity_guards(connection: sqlite3.Connection) -> None:
    required = set(IDENTITY_GUARDS)
    if _table_exists(connection, "trade_cash_links"):
        required.update(
            {
                "trade_cash_links_valid_insert",
                "trade_cash_links_valid_update",
                "canonical_events_trade_cash_identity_update",
            }
        )
    required.update(
        {
            "balance_evidence_require_provenance_insert",
            "balance_evidence_require_provenance_update",
            "position_evidence_require_provenance_insert",
            "position_evidence_require_provenance_update",
        }
    )
    found = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger'"
        )
        if str(row[0]) in required
    }
    missing = len(required - found)
    if missing:
        raise ReconciliationError(
            f"reconciliation identity guards are incomplete (missing={missing})"
        )


def _relationship_error(
    connection: sqlite3.Connection, *, phase: str
) -> None:
    invalid = relationship_violation_counts(connection)
    if invalid:
        summary = ", ".join(
            f"{table}={count}" for table, count in sorted(invalid.items())
        )
        raise ReconciliationError(
            f"{phase} account/type relationships are invalid ({summary})"
        )


def _account_rows(
    connection: sqlite3.Connection, request: ReconciliationRequest
) -> tuple[sqlite3.Row, sqlite3.Row]:
    rows = connection.execute(
        """SELECT id,kind,currency,opened_on,closed_on
           FROM accounts WHERE id IN (?,?)""",
        (request.source_account_id, request.target_account_id),
    ).fetchall()
    by_id = {int(row["id"]): row for row in rows}
    if len(by_id) != 2:
        raise ReconciliationError("source/target account selection is invalid")
    source = by_id[request.source_account_id]
    target = by_id[request.target_account_id]
    if target["closed_on"] is not None:
        raise ReconciliationError("target account must be open")
    if source["kind"] != target["kind"]:
        raise ReconciliationError("source/target account kinds differ")
    return source, target


def _collision_pairs(
    connection: sqlite3.Connection,
    request: ReconciliationRequest,
    *,
    table: str,
    identity: str,
) -> set[tuple[int, int]]:
    rows = connection.execute(
        f"""SELECT source.id,target.id
            FROM {table} AS source
            JOIN {table} AS target
              ON target.account_id=? AND {identity}
            WHERE source.account_id=?""",
        (request.target_account_id, request.source_account_id),
    )
    return {(int(row[0]), int(row[1])) for row in rows}


def _validate_collisions(
    connection: sqlite3.Connection, request: ReconciliationRequest
) -> None:
    unresolved = {
        "account_feeds": int(
            connection.execute(
                """SELECT COUNT(*) FROM account_feeds AS source
                   JOIN account_feeds AS target
                     ON target.account_id=?
                    AND target.provider=source.provider
                    AND target.external_account_ref IS NULL
                   WHERE source.account_id=?
                     AND source.external_account_ref IS NULL""",
                (request.target_account_id, request.source_account_id),
            ).fetchone()[0]
        ),
        "trades": int(
            connection.execute(
                """SELECT COUNT(*) FROM trades AS source
                   JOIN trades AS target
                     ON target.account_id=?
                    AND target.external_id=source.external_id
                    AND target.source_event_id IS NULL
                   WHERE source.account_id=?
                     AND source.external_id IS NOT NULL
                     AND source.source_event_id IS NULL""",
                (request.target_account_id, request.source_account_id),
            ).fetchone()[0]
        ),
    }
    unresolved = {table: count for table, count in unresolved.items() if count}
    if unresolved:
        summary = ", ".join(
            f"{table}={count}" for table, count in sorted(unresolved.items())
        )
        raise ReconciliationError(f"unresolved natural-key conflicts ({summary})")

    balance_pairs = _collision_pairs(
        connection,
        request,
        table="balances",
        identity="target.as_of=source.as_of AND target.component IS source.component",
    )
    supplied_balances = set(request.balance_resolutions.items())
    if balance_pairs != supplied_balances:
        raise ReconciliationError(
            "balance collision resolutions do not match "
            f"(collisions={len(balance_pairs)}, resolutions={len(supplied_balances)})"
        )
    position_pairs = _collision_pairs(
        connection,
        request,
        table="positions",
        identity=(
            "target.security_id=source.security_id AND target.as_of=source.as_of"
        ),
    )
    supplied_positions = set(request.position_resolutions.items())
    if position_pairs != supplied_positions:
        raise ReconciliationError(
            "position collision resolutions do not match "
            f"(collisions={len(position_pairs)}, resolutions={len(supplied_positions)})"
        )


def _validate_anchor_resolutions(
    connection: sqlite3.Connection,
    request: ReconciliationRequest,
    *,
    table: str,
    evidence_table: str,
    evidence_column: str,
    resolutions: dict[int, int],
) -> None:
    missing_evidence = 0
    missing_target_evidence = 0
    invalid = 0
    for source_id, target_id in resolutions.items():
        if table == "balances":
            row = connection.execute(
                """SELECT 1 FROM balances AS source
                   JOIN balances AS target ON target.id=?
                   WHERE source.id=? AND source.account_id=? AND target.account_id=?
                     AND source.as_of=target.as_of
                     AND source.component IS target.component
                     AND source.currency=target.currency""",
                (
                    target_id,
                    source_id,
                    request.source_account_id,
                    request.target_account_id,
                ),
            ).fetchone()
        else:
            row = connection.execute(
                """SELECT 1 FROM positions AS source
                   JOIN positions AS target ON target.id=?
                   WHERE source.id=? AND source.account_id=? AND target.account_id=?
                     AND source.security_id=target.security_id
                     AND source.as_of=target.as_of
                     AND source.currency=target.currency""",
                (
                    target_id,
                    source_id,
                    request.source_account_id,
                    request.target_account_id,
                ),
            ).fetchone()
        if row is None:
            invalid += 1
        evidence_count = int(
            connection.execute(
                f"SELECT COUNT(*) FROM {evidence_table} WHERE {evidence_column}=?",
                (source_id,),
            ).fetchone()[0]
        )
        if not evidence_count:
            missing_evidence += 1
        target_evidence_count = int(
            connection.execute(
                f"SELECT COUNT(*) FROM {evidence_table} WHERE {evidence_column}=?",
                (target_id,),
            ).fetchone()[0]
        )
        if not target_evidence_count:
            missing_target_evidence += 1
    if invalid or missing_evidence or missing_target_evidence:
        raise ReconciliationError(
            f"{table} resolutions failed validation "
            f"(invalid={invalid}, without_source_evidence={missing_evidence}, "
            f"without_target_evidence={missing_target_evidence})"
        )


def _validate_event_resolutions(
    connection: sqlite3.Connection, request: ReconciliationRequest
) -> None:
    invalid = 0
    without_evidence = 0
    targets_without_evidence = 0
    linked = 0
    for obsolete_id, target_ids in request.event_resolutions.items():
        source = connection.execute(
            """SELECT event_type,status FROM canonical_events
               WHERE id=? AND account_id=?""",
            (obsolete_id, request.source_account_id),
        ).fetchone()
        if source is None or source["status"] == "excluded":
            invalid += 1
            continue
        for target_id in target_ids:
            target = connection.execute(
                """SELECT event_type,status FROM canonical_events
                   WHERE id=? AND account_id=?""",
                (target_id, request.target_account_id),
            ).fetchone()
            if (
                target is None
                or target["status"] != "confirmed"
                or target["event_type"] != source["event_type"]
            ):
                invalid += 1
            elif not connection.execute(
                "SELECT 1 FROM event_evidence WHERE canonical_event_id=? LIMIT 1",
                (target_id,),
            ).fetchone():
                targets_without_evidence += 1
        evidence_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM event_evidence WHERE canonical_event_id=?",
                (obsolete_id,),
            ).fetchone()[0]
        )
        if not evidence_count:
            without_evidence += 1
        if _table_exists(connection, "trade_cash_links"):
            linked += int(
                connection.execute(
                    """SELECT COUNT(*) FROM trade_cash_links
                       WHERE cash_event_id=? OR trade_event_id=?""",
                    (obsolete_id, obsolete_id),
                ).fetchone()[0]
            )
    if invalid or without_evidence or targets_without_evidence or linked:
        raise ReconciliationError(
            "event resolutions failed ownership/type validation "
            f"(invalid={invalid}, without_source_evidence={without_evidence}, "
            f"without_target_evidence={targets_without_evidence}, linked={linked})"
        )


def _event_evidence_rows(
    connection: sqlite3.Connection, source_account_id: int
) -> list[sqlite3.Row]:
    return connection.execute(
        """SELECT evidence.canonical_event_id,evidence.source_event_id,
                  evidence.evidence_role,evidence.match_method,evidence.confidence,
                  evidence.notes,evidence.linked_at
           FROM event_evidence AS evidence
           JOIN canonical_events AS canonical
             ON canonical.id=evidence.canonical_event_id
           WHERE canonical.account_id=?
           ORDER BY evidence.canonical_event_id,evidence.source_event_id""",
        (source_account_id,),
    ).fetchall()


def _resolved_event_evidence(
    rows: list[sqlite3.Row], resolutions: dict[int, tuple[int, ...]]
) -> list[tuple[Any, ...]]:
    result: list[tuple[Any, ...]] = []
    for row in rows:
        old_canonical = int(row["canonical_event_id"])
        targets = resolutions.get(old_canonical)
        if targets is None:
            targets = (old_canonical,)
            role = row["evidence_role"]
        else:
            role = "supporting" if len(targets) == 1 else "aggregate"
        for target in targets:
            result.append(
                (
                    target,
                    row["source_event_id"],
                    role,
                    row["match_method"],
                    row["confidence"],
                    row["notes"],
                    row["linked_at"],
                )
            )

    duplicate_keys = sum(
        count - 1
        for count in Counter((row[0], row[1]) for row in result).values()
        if count > 1
    )
    duplicate_nonaggregate_sources = sum(
        count - 1
        for count in Counter(row[1] for row in result if row[2] != "aggregate").values()
        if count > 1
    )
    duplicate_primaries = sum(
        count - 1
        for count in Counter(row[0] for row in result if row[2] == "primary").values()
        if count > 1
    )
    if duplicate_keys or duplicate_nonaggregate_sources or duplicate_primaries:
        raise ReconciliationError(
            "event resolutions create evidence conflicts "
            f"(keys={duplicate_keys}, sources={duplicate_nonaggregate_sources}, "
            f"primaries={duplicate_primaries})"
        )
    return result


def _source_graph_counts(
    connection: sqlite3.Connection, account_id: int
) -> dict[str, int]:
    return {
        table: int(
            connection.execute(
                f"SELECT COUNT(*) FROM {table} WHERE account_id=?", (account_id,)
            ).fetchone()[0]
        )
        for table in (
            "account_feeds",
            "balances",
            "transactions",
            "trades",
            "canonical_events",
            "positions",
        )
    }


def _stage_rows(
    connection: sqlite3.Connection, request: ReconciliationRequest
) -> dict[str, list[sqlite3.Row]]:
    source = request.source_account_id
    return {
        "source_events": connection.execute(
            """SELECT source.* FROM source_events AS source
               JOIN account_feeds AS feed ON feed.id=source.account_feed_id
               WHERE feed.account_id=? ORDER BY source.id""",
            (source,),
        ).fetchall(),
        "transactions": connection.execute(
            """SELECT id,source_event_id,canonical_event_id
               FROM transactions WHERE account_id=? ORDER BY id""",
            (source,),
        ).fetchall(),
        "trades": connection.execute(
            """SELECT id,source_event_id,canonical_event_id,external_id
               FROM trades WHERE account_id=? ORDER BY id""",
            (source,),
        ).fetchall(),
        "event_evidence": _event_evidence_rows(connection, source),
        "balance_evidence": connection.execute(
            """SELECT evidence.* FROM balance_evidence AS evidence
               JOIN balances AS anchor ON anchor.id=evidence.canonical_balance_id
               WHERE anchor.account_id=? ORDER BY evidence.id""",
            (source,),
        ).fetchall(),
        "position_evidence": connection.execute(
            """SELECT evidence.* FROM position_evidence AS evidence
               JOIN positions AS anchor ON anchor.id=evidence.canonical_position_id
               WHERE anchor.account_id=? ORDER BY evidence.id""",
            (source,),
        ).fetchall(),
        "trade_cash_links": (
            connection.execute(
                """SELECT link.* FROM trade_cash_links AS link
                   JOIN canonical_events AS cash ON cash.id=link.cash_event_id
                   WHERE cash.account_id=? ORDER BY link.cash_event_id,link.trade_event_id""",
                (source,),
            ).fetchall()
            if _table_exists(connection, "trade_cash_links")
            else []
        ),
    }


def _delete_staged_evidence(
    connection: sqlite3.Connection, staged: dict[str, list[sqlite3.Row]]
) -> None:
    for row in staged["trade_cash_links"]:
        connection.execute(
            "DELETE FROM trade_cash_links WHERE cash_event_id=? AND trade_event_id=?",
            (row["cash_event_id"], row["trade_event_id"]),
        )
    for row in staged["event_evidence"]:
        connection.execute(
            "DELETE FROM event_evidence WHERE canonical_event_id=? AND source_event_id=?",
            (row["canonical_event_id"], row["source_event_id"]),
        )
    for table in ("balance_evidence", "position_evidence"):
        for row in staged[table]:
            connection.execute(f"DELETE FROM {table} WHERE id=?", (row["id"],))


def _reinsert_source_events(
    connection: sqlite3.Connection, rows: list[sqlite3.Row]
) -> None:
    columns = (
        "id",
        "account_feed_id",
        "source_key",
        "event_type",
        "occurred_on",
        "source_doc_id",
        "payload_hash",
        "observed_at",
    )
    for row in rows:
        connection.execute(
            """INSERT INTO source_events
               (id,account_feed_id,source_key,event_type,occurred_on,
                source_doc_id,payload_hash,observed_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            tuple(row[column] for column in columns),
        )


def _restore_raw_links(
    connection: sqlite3.Connection,
    table: str,
    rows: list[sqlite3.Row],
    event_resolutions: dict[int, tuple[int, ...]],
) -> None:
    for row in rows:
        canonical_id = row["canonical_event_id"]
        if canonical_id is not None and int(canonical_id) in event_resolutions:
            targets = event_resolutions[int(canonical_id)]
            canonical_id = targets[0] if len(targets) == 1 else None
        if table == "trades":
            connection.execute(
                """UPDATE trades SET source_event_id=?,canonical_event_id=?,external_id=?
                   WHERE id=?""",
                (
                    row["source_event_id"],
                    canonical_id,
                    row["external_id"],
                    row["id"],
                ),
            )
        else:
            connection.execute(
                """UPDATE transactions
                   SET source_event_id=?,canonical_event_id=? WHERE id=?""",
                (row["source_event_id"], canonical_id, row["id"]),
            )


def _reinsert_snapshot_evidence(
    connection: sqlite3.Connection,
    *,
    table: str,
    rows: list[sqlite3.Row],
    anchor_column: str,
    resolutions: dict[int, int],
) -> None:
    if table == "balance_evidence":
        columns = (
            "id",
            "canonical_balance_id",
            "account_feed_id",
            "source_doc_id",
            "source_key",
            "observed_amount_minor",
            "observed_currency",
            "account_snapshot_complete",
            "observed_at",
        )
    else:
        columns = (
            "id",
            "canonical_position_id",
            "account_feed_id",
            "source_doc_id",
            "source_key",
            "observed_quantity",
            "observed_avg_cost_minor",
            "observed_price_minor",
            "observed_value_minor",
            "observed_currency",
            "account_snapshot_complete",
            "observed_at",
        )
    placeholders = ",".join("?" for _ in columns)
    for row in rows:
        values = [row[column] for column in columns]
        anchor_index = columns.index(anchor_column)
        values[anchor_index] = resolutions.get(int(values[anchor_index]), values[anchor_index])
        connection.execute(
            f"INSERT INTO {table}({','.join(columns)}) VALUES ({placeholders})",
            values,
        )


def _restore_trade_cash_links(
    connection: sqlite3.Connection, rows: list[sqlite3.Row]
) -> None:
    for row in rows:
        connection.execute(
            """INSERT INTO trade_cash_links
               (cash_event_id,trade_event_id,match_method,created_at)
               VALUES (?,?,?,?)""",
            (
                row["cash_event_id"],
                row["trade_event_id"],
                row["match_method"],
                row["created_at"],
            ),
        )


def _run_reconciliation(
    connection: sqlite3.Connection,
    request: ReconciliationRequest,
    close_date: str,
) -> dict[str, int]:
    source_account, target_account = _account_rows(connection, request)
    if source_account["opened_on"] is not None and source_account["opened_on"] > close_date:
        raise ReconciliationError("source account cannot be closed on the requested date")
    _require_identity_guards(connection)
    _relationship_error(connection, phase="existing")
    _validate_collisions(connection, request)
    _validate_anchor_resolutions(
        connection,
        request,
        table="balances",
        evidence_table="balance_evidence",
        evidence_column="canonical_balance_id",
        resolutions=request.balance_resolutions,
    )
    _validate_anchor_resolutions(
        connection,
        request,
        table="positions",
        evidence_table="position_evidence",
        evidence_column="canonical_position_id",
        resolutions=request.position_resolutions,
    )
    _validate_event_resolutions(connection, request)

    # An opening date gates historical valuation. Preserve the full known
    # coverage of the economic account instead of letting the chosen target
    # label hide older facts. NULL means unknown, so it remains the conservative
    # unbounded result when either duplicate has no reliable opening date.
    source_opened = source_account["opened_on"]
    target_opened = target_account["opened_on"]
    merged_opened = (
        min(source_opened, target_opened)
        if source_opened is not None and target_opened is not None
        else None
    )

    before = _source_graph_counts(connection, request.source_account_id)
    staged = _stage_rows(connection, request)
    final_event_evidence = _resolved_event_evidence(
        staged["event_evidence"], request.event_resolutions
    )
    counts = {
        **before,
        "source_events": len(staged["source_events"]),
        "event_evidence": len(final_event_evidence),
        "balance_evidence": len(staged["balance_evidence"]),
        "position_evidence": len(staged["position_evidence"]),
        "event_resolutions": len(request.event_resolutions),
        "balance_resolutions": len(request.balance_resolutions),
        "position_resolutions": len(request.position_resolutions),
    }

    _delete_staged_evidence(connection, staged)
    connection.execute(
        """UPDATE transactions
           SET source_event_id=NULL,canonical_event_id=NULL WHERE account_id=?""",
        (request.source_account_id,),
    )
    connection.execute(
        """UPDATE trades
           SET source_event_id=NULL,canonical_event_id=NULL,external_id=NULL
           WHERE account_id=?""",
        (request.source_account_id,),
    )
    for row in staged["source_events"]:
        connection.execute("DELETE FROM source_events WHERE id=?", (row["id"],))

    for source_id in request.balance_resolutions:
        connection.execute("DELETE FROM balances WHERE id=?", (source_id,))
    for source_id in request.position_resolutions:
        connection.execute("DELETE FROM positions WHERE id=?", (source_id,))
    connection.execute(
        "UPDATE balances SET account_id=? WHERE account_id=?",
        (request.target_account_id, request.source_account_id),
    )
    connection.execute(
        "UPDATE positions SET account_id=? WHERE account_id=?",
        (request.target_account_id, request.source_account_id),
    )
    for obsolete_id in request.event_resolutions:
        connection.execute(
            """UPDATE canonical_events
               SET account_id=?,status='excluded' WHERE id=?""",
            (request.target_account_id, obsolete_id),
        )
    connection.execute(
        "UPDATE canonical_events SET account_id=? WHERE account_id=?",
        (request.target_account_id, request.source_account_id),
    )
    connection.execute(
        "UPDATE transactions SET account_id=? WHERE account_id=?",
        (request.target_account_id, request.source_account_id),
    )
    connection.execute(
        "UPDATE trades SET account_id=? WHERE account_id=?",
        (request.target_account_id, request.source_account_id),
    )
    connection.execute(
        "UPDATE account_feeds SET account_id=? WHERE account_id=?",
        (request.target_account_id, request.source_account_id),
    )
    connection.execute(
        "UPDATE accounts SET opened_on=? WHERE id=?",
        (merged_opened, request.target_account_id),
    )

    _reinsert_source_events(connection, staged["source_events"])
    _restore_raw_links(
        connection, "transactions", staged["transactions"], request.event_resolutions
    )
    _restore_raw_links(
        connection, "trades", staged["trades"], request.event_resolutions
    )
    for row in final_event_evidence:
        connection.execute(
            """INSERT INTO event_evidence
               (canonical_event_id,source_event_id,evidence_role,match_method,
                confidence,notes,linked_at)
               VALUES (?,?,?,?,?,?,?)""",
            row,
        )
    _reinsert_snapshot_evidence(
        connection,
        table="balance_evidence",
        rows=staged["balance_evidence"],
        anchor_column="canonical_balance_id",
        resolutions=request.balance_resolutions,
    )
    _reinsert_snapshot_evidence(
        connection,
        table="position_evidence",
        rows=staged["position_evidence"],
        anchor_column="canonical_position_id",
        resolutions=request.position_resolutions,
    )
    if staged["trade_cash_links"]:
        _restore_trade_cash_links(connection, staged["trade_cash_links"])

    connection.execute(
        "UPDATE accounts SET closed_on=COALESCE(closed_on,?) WHERE id=?",
        (close_date, request.source_account_id),
    )
    remaining = _source_graph_counts(connection, request.source_account_id)
    remaining_count = sum(remaining.values())
    if remaining_count:
        raise ReconciliationError(
            f"source account graph is not empty (rows={remaining_count})"
        )
    closed = connection.execute(
        "SELECT closed_on IS NOT NULL FROM accounts WHERE id=?",
        (request.source_account_id,),
    ).fetchone()[0]
    if not closed:
        raise ReconciliationError("source account was not closed")
    _require_identity_guards(connection)
    foreign_keys = len(connection.execute("PRAGMA foreign_key_check").fetchall())
    if foreign_keys:
        raise ReconciliationError(
            f"reconciliation foreign-key validation failed (rows={foreign_keys})"
        )
    integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        raise ReconciliationError("reconciliation integrity validation failed")
    _relationship_error(connection, phase="resulting")
    return counts


def reconcile_logical_accounts(
    path: Path, payload: Any, *, close_date: str | None = None
) -> dict[str, int]:
    """Atomically merge one reviewed duplicate account into its target."""

    request = parse_reconciliation_request(payload)
    effective_date = close_date or date.today().isoformat()
    try:
        date.fromisoformat(effective_date)
    except (TypeError, ValueError) as exc:
        raise ReconciliationError("close date must be an ISO date") from exc

    connection = connect_db(path)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("BEGIN IMMEDIATE")
        try:
            counts = _run_reconciliation(connection, request, effective_date)
            connection.commit()
            return counts
        except ReconciliationError:
            connection.rollback()
            raise
        except sqlite3.Error:
            connection.rollback()
            raise ReconciliationError(
                "logical-account reconciliation failed; no changes committed"
            ) from None
    finally:
        connection.close()
