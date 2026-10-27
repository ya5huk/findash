# Database operations

Skills never invoke the SQLite CLI or assemble shell-escaped SQL. All reads and
writes go through `scripts/findash_db.py`, which fixes the database path, enables
foreign keys, applies migrations before writes, bounds query output, and commits a
write request atomically.

**Claude interprets; the ledger engine does the bookkeeping.** Every write is a
typed *intent* that holds only the facts Claude read. The engine derives everything
derivable (missing amounts, rates, market rates, cash components, counterpart legs,
links, dedup keys, zero anchors) and writes every row the fact implies.

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

```bash
python3 scripts/findash_db.py query inbox/staging/operations/<request>.json
```

A write request is one list of intents, applied in order in one transaction:

```json
{
  "intents": [
    {"type": "document", "drive_id": "<stable-source-id>",
     "drive_path": "<source-path>", "filename": "<filename>"},
    {"type": "cash", "account": "<account name or id>", "date": "YYYY-MM-DD",
     "amount": "-42.50", "currency": "ILS", "flow_type": "expense",
     "category": "groceries",
     "evidence": {"document": "<stable-source-id>", "row": 7}}
  ]
}
```

```bash
python3 scripts/findash_db.py apply inbox/staging/operations/<request>.json
```

The output reports per-intent outcomes (`created`, `linked`, `confirmed`,
`updated`, `unchanged`, ...). Re-applying a request is a no-op; an intent that
contradicts recorded facts fails the whole request. Put the `document` intent
first so later intents can cite it by `drive_id`. There is no plain-SQL write
path; the logged `raw_sql` intent is the only escape.

## Reference intents

| Intent | Required | Optional | Behaviour |
|---|---|---|---|
| `document` | `drive_id drive_path filename` | `doc_type doc_date raw_hash notes` | created once; a later run may refine path, type, date or notes, never the bytes (`raw_hash`) |
| `account` | `name kind institution currency` | `external_id opened_on closed_on investment_track liquidity_date notes` | keyed by name; `kind`, `institution`, `currency` never change |
| `feed` | `account provider feed_type` | `external_account_ref label active` | a provider account attaches to one logical account for good; moving it needs `reconcile-account` |
| `security` | `ticker currency` | `name asset_class` | other intents create a missing security implicitly |
| `price` | `ticker date close currency` | — | a stored close is never overwritten |
| `meta` | `key` | `value` or `now: true` | run-journal stamps |
| `balance` | `account as_of amount currency evidence` | `component complete prefer` | one snapshot observation (bank closing balance, pension component, ...) |
| `position` | `account as_of ticker quantity currency evidence` | `avg_cost price value complete prefer` | one holding observation |
| `snapshot` | `account as_of evidence` | `cash positions complete` | a whole brokerage read: `cash` = `[{currency, amount}]`, `positions` = `[{ticker, quantity, currency, avg_cost?, price?, value?}]` |
| `payslip` | `employer period_start period_end gross net` | `paid_on currency document line_items` + the deduction/contribution amounts | keyed by period and employer |

Snapshot evidence is `{"feed": "<provider>", "key": "<key>"}` or
`{"document": "<drive_id>"}` (optional `row`, and `feed` when the document came
through one). A `snapshot` derives each observation's key from that prefix:
`<key>:cash:<CCY>` and `<key>:pos:<ticker>`.

- **One anchor per identity.** A second source that agrees becomes corroborating
  evidence (`linked`); one that disagrees is rejected. Decide which source is right
  and re-send the one to keep with `prefer: true` (`replaced`). Never sum two
  snapshots.
- **`complete: true`** asserts the read covered every holding and every cash
  currency. On a `snapshot`, the engine also writes zero anchors for previously
  held securities and previously non-zero cash currencies the read omitted —
  limited to what the same feed has reported before, so an API never zeroes a
  wallet only the wrapper's statements can see. Partial reads never close anything.
- **Payslip amounts** are `gross`, `net`, `income_tax`, `social_security`,
  `health_insurance`, `pension_employee`, `pension_employer`,
  `study_fund_employee`, `study_fund_employer` (major units; currency default
  ILS). `line_items` = `[{label, amount, kind}]` with kind `earning`, `deduction`
  or `info`. Re-sending may only add `paid_on` or the document; any other
  difference is rejected.

## Money intents

Every intent names accounts by id or exact name, writes amounts as major-unit
decimals in the stated currency (extra precision is an error), and cites evidence.

**Evidence** is one of:

| Form | Meaning |
|---|---|
| `{"document": "<drive_id>", "row": "<stable row key>"}` | a document row; optional `"feed"` names the provider feed (default `document-ledger`) |
| `{"feed": "<provider>", "key": "<provider id>"}` | a live feed observation; optional `"document"` adds the staged document |
| `{"transaction": <id>}` | adopt an existing raw transaction that has no event yet |
| `{"event": <id>}` | an already-normalized canonical event |

The evidence key is the idempotency key: the same evidence never creates a second
event. When the user tells you a fact directly (no document), use
`"confirmed_by": "<plain reason>"` where an intent allows it.

| Intent | Required | Optional | Engine derives |
|---|---|---|---|
| `cash` | `account date amount currency flow_type evidence` | `category component description counterparty reference value_date status notes same_as_event` | raw row, source identity, canonical event; brokerage `cash_<ccy>` component |
| `transfer` | `date amount currency from to` | `description counterparty reference` | both legs and their `transfer_links` row |
| `fx_conversion` | `account date sell buy` | `settled_on rate evidence confirmed_by description` | the missing third value, both legs, market rate, `fx_conversions` row |
| `trade` | `account date ticker side shares price currency evidence` | `fees external_id same_as_event status` | security (if new), raw trade, canonical trade |
| `trade_settlement` | `cash_event trade_events` | — | validated `trade_cash_links` |
| `dividend` | `account ticker date currency evidence` | `ex_date gross withholding net description` | net amount; settles a matching expected dividend |
| `confirm` | `event` | `date amount evidence note` | status `confirmed`, evidence row |
| `exclude` | `event reason` | — | excludes the event and anything that only existed because of its link |
| `raw_sql` | `reason statements` | — | logged escape hatch (below) |

Notes per intent:

- **`cash`** — `same_as_event` attaches a second feed's observation to an existing
  event (a list means one aggregate statement row covering several events).
  `status: "needs_review"` keeps an uncertain row out of calculations.
- **`transfer`** — `from`/`to` each take `account` plus optional `evidence`, `date`
  and `category`. A side with no evidence becomes an **expected leg**
  (`needs_review`) that reconcile or a later intent confirms. Give
  `confirmed_by` on the unobserved side only when other evidence already proves
  the destination. Re-sending the transfer with new destination evidence fills the
  expected leg instead of creating another.
- **`fx_conversion`** — `sell`/`buy` take `currency` plus optional `amount` and
  `evidence`; give two of sell amount, buy amount and `rate`, or all three if they
  agree. `rate` is quoted the way people read it: reporting currency per foreign
  unit (ILS per USD) in either direction. `date` is the conversion date (market
  rate date); `settled_on` is when the bought cash lands. The user's rate is stored
  beside the market rate; neither goes into `fx_rates`.
- **`trade`** — commission may carry either sign; it is stored as a cost.
  Currency comes from the security, not the provider's trade row.
- **`dividend`** — give what the source shows: `net`, or `gross` with
  `withholding`. With an open expected dividend for the same security, document or
  feed evidence settles it at the observed amount.

## Escape hatch

When no intent fits, use a `raw_sql` intent with a written reason:

```json
{"type": "raw_sql", "reason": "plain-language reason, no financial values",
 "statements": [{"sql": "UPDATE ... WHERE ...", "params": []}]}
```

Statements must be a single `INSERT`, `INSERT OR IGNORE` or bounded
`UPDATE ... WHERE` each (no `REPLACE`, schema changes or deletes), may touch any
table, and are recorded in `ledger_log`. Reconcile and the doctor report every use from the last 14 days.
Prefer adding a typed intent over repeating an escape.

## Daily reconcile

```bash
python3 scripts/findash_db.py reconcile [--today YYYY-MM-DD] [--dry-run]
```

1. Fills missing market rates on recorded conversions.
2. Expects dividends: for each Yahoo ex-date in the last 45 days where a brokerage
   held shares, net = shares × per-share × (1 − withholding learned from that
   account's history, default 25%).
3. Measures **unexplained brokerage cash**: per cash currency, the change between
   consecutive snapshots minus every recorded movement between them.
4. Auto-confirms a residual only when exactly one explanation fits: an expected
   transfer in the same currency; an expected transfer in another currency whose
   implied rate is within the FX band (1% better to 2.5% worse than market), which
   also records the conversion; or expected dividend(s) within 2%.
5. Prints the remaining open items as JSON.

| Open item | Meaning | Close it with |
|---|---|---|
| `expected_leg` | transfer counterpart not yet observed | `transfer` with the side's evidence, or `exclude` |
| `unexplained_cash` | snapshot change no event explains | the intent for what it was (`dividend`, `cash` interest/fee, `transfer`, ...) |
| `stale_review` | `needs_review` event older than 14 days | `confirm` or `exclude` |
| `dividend_overdue` | expected dividend 45 days past ex-date | `dividend`, or `exclude` if not paid |
| `fx_rate_outside_band` | user's rate far from market | check the date/amounts; correct with `exclude` + a new intent |
| `missing_market_rate` | no market close near the conversion date | backfill prices; reconcile fills it |
| `raw_sql_used` | escape hatch used recently | add a typed intent for that case |

Items with `escalate: true` (older than 7 days, or any raw SQL) are what the user
should hear about.

## Reviewed duplicate-account reconciliation

This is a fixed, reviewed operation. It is not free-form SQL and
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

Always delete the request after the command finishes, whether it succeeded or failed:

```bash
python3 scripts/staging_files.py delete inbox/staging/operations/<request>.json
```

Query only the columns needed for the decision, and never echo raw rows, SQL parameters,
account identifiers, or financial values into a public report or unattended log.

For a health check, use `python3 scripts/findash_db.py integrity`.
