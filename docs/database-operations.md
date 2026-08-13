# Database operations

Skills never invoke the SQLite CLI or assemble shell-escaped SQL. All normal reads and
writes go through `scripts/findash_db.py`, which fixes the database path, enables foreign
keys, applies migrations before writes, bounds query output, and commits a write batch
atomically.

## Request files

Write a private JSON request under `inbox/staging/operations/`. Use a fresh, simple
filename ending in `.json`; never place a request elsewhere.

A read request has one parameterized `SELECT` (or read-only `WITH`):

```json
{
  "sql": "SELECT id, name FROM accounts WHERE kind=? ORDER BY id",
  "params": ["brokerage"]
}
```

Run it with:

```bash
python3 scripts/findash_db.py query inbox/staging/operations/<request>.json
```

A write request contains an ordered list of parameterized statements:

```json
{
  "statements": [
    {
      "sql": "INSERT INTO documents(drive_id,drive_path,filename) VALUES(?,?,?) ON CONFLICT(drive_id) DO NOTHING",
      "params": ["<stable-source-id>", "<source-path>", "<filename>"]
    }
  ]
}
```

Run it with:

```bash
python3 scripts/findash_db.py apply inbox/staging/operations/<request>.json
```

The whole list succeeds or rolls back. Put the document/source identity before facts
that cite it, and resolve generated ids inside later SQL with a stable-key subquery.
The gateway accepts `INSERT`, `INSERT OR IGNORE`, and bounded `UPDATE ... WHERE`;
use `ON CONFLICT ... DO UPDATE` instead of `REPLACE`. Schema changes and general
destructive deletes belong only in reviewed versioned migrations. The fixed reviewed
operations below are the only exceptions.

## Reviewed duplicate-account reconciliation

The first exception is a fixed reconciliation operation. It is not free-form SQL and
does not enable `DELETE` in normal write batches. Use it only after a human has
confirmed that the source and target rows are the same economic account and every
overlap has been reviewed.

```json
{
  "operation": "merge_duplicate_logical_account",
  "source_account_id": 0,
  "target_account_id": 0,
  "confirmed_same_economic_account": true,
  "confirmed_reviewed_resolutions": true,
  "confirmed_unmapped_events_are_distinct": true,
  "event_resolutions": [
    {
      "obsolete_canonical_event_id": 0,
      "target_canonical_event_ids": [0]
    }
  ],
  "balance_resolutions": [
    {"source_balance_id": 0, "target_balance_id": 0}
  ],
  "position_resolutions": [
    {"source_position_id": 0, "target_position_id": 0}
  ]
}
```

Replace every `0` inside the private request with a reviewed local id. List every
balance/position natural-key collision and select its already-supported target
anchor. For a duplicate canonical event, list one target for a one-to-one match or
several targets when one source observation aggregates several economic events.
Omit a source event from `event_resolutions` only after confirming it is a distinct
economic event that should move unchanged.

Run the fixed operation with:

```bash
python3 scripts/findash_db.py reconcile-account inbox/staging/operations/<request>.json
```

The command validates ownership, event type, source and target evidence, every
natural-key collision, foreign keys, and all cross-table account/type relationships.
It preserves source-event and evidence ids; a one-to-one event resolution becomes
supporting evidence, a one-to-many resolution becomes aggregate evidence, and the
obsolete canonical row remains on the target as `excluded`. Redundant snapshot
anchors are removed only after their evidence is moved to the reviewed target. The
source account must end empty and is then closed. Any ambiguity rolls the transaction
back. The target keeps its human-facing metadata while its opening date expands to
preserve the full known history of either duplicate. Output contains aggregate counts
only.

## Reviewed incorrect-balance removal

Use the second fixed operation only after a human has confirmed that one interpreted
balance is wrong. Repeat the row's full identity, evidence count, and complete source
document set so any intervening change makes the operation roll back. The source
documents remain in `documents` and receive a correction note; removing the balance
cascades only to its normalized `balance_evidence` rows.

```json
{
  "operation": "remove_incorrect_balance",
  "balance_id": 0,
  "expected_account_id": 0,
  "expected_as_of": "YYYY-MM-DD",
  "expected_component": null,
  "expected_amount_minor": 0,
  "expected_currency": "ILS",
  "expected_source_doc_ids": [0],
  "expected_evidence_count": 0,
  "confirmed_incorrect_historical_interpretation": true,
  "confirmed_source_documents_preserved": true,
  "reason": "plain-language reviewed reason"
}
```

Replace the placeholders inside the private request with values from one narrowly
scoped read. Run:

```bash
python3 scripts/findash_db.py remove-incorrect-balance inbox/staging/operations/<request>.json
```

The command verifies every guard inside one immediate transaction, preserves the
source documents, validates the resulting schema and relationships, and emits only
aggregate counts. It does not enable `DELETE` in normal write batches.

## Reviewed inferred brokerage funding

Use the third fixed operation only after two complete per-currency brokerage cash
snapshots from the same active feed reveal a positive residual, and confirmed
bank-side transfers uniquely explain that entire residual. Private recipient and
reference evidence must identify the brokerage. Amount/date similarity alone is not
enough.

```json
{
  "operation": "infer_brokerage_funding",
  "outflow_event_ids": [0],
  "brokerage_account_id": 0,
  "before_balance_id": 0,
  "after_balance_id": 0,
  "confirmed_bank_destination": true,
  "confirmed_no_competing_cash_explanation": true,
  "confirmed_complete_snapshot_pair": true
}
```

Replace every `0` with a privately reviewed local id. Several bank outflows may
share one snapshot interval; list all of them only when their sum is the full
residual. Run:

```bash
python3 scripts/findash_db.py infer-brokerage-funding inbox/staging/operations/<request>.json
```

The operation requires confirmed negative transfer events with source evidence,
same-currency complete brokerage cash anchors from one active feed, chronological
containment, and an exact residual after confirmed cash events and trade settlements
(apart from the existing fractional-trade minor-unit tolerance). It atomically
creates equal positive brokerage transfers and durable `inferred_transfer_links`.
Re-running the same request is a no-op. A partial snapshot, unexplained remainder,
missing bank evidence, competing destination, or already-linked transfer fails the
whole operation.

Always delete the request after the command finishes, whether it succeeded or failed:

```bash
python3 scripts/staging_files.py delete inbox/staging/operations/<request>.json
```

Query only the columns needed for the decision, and never echo raw rows, SQL parameters,
account identifiers, or financial values into a public report or unattended log.

For a health check, use `python3 scripts/findash_db.py integrity`.
