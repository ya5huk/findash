# SQLite schema

Source of truth: [`scripts/init-db.sql`](../scripts/init-db.sql). This file explains the *intent* — the SQL file is the spec. Existing databases are upgraded transactionally with `python3 scripts/migrate_db.py`; never copy ad-hoc `ALTER TABLE` guards into skills.

## Conventions

- **Money:** stored as `INTEGER amount_minor` in the ISO currency's minor unit (agorot for ILS, cents for USD, whole yen for JPY). Convert through `scripts/findash_core/currencies.py`; ISO exponents vary, so never assume a factor of 100 or use `REAL` for money.
- **Per-share distributions:** stored exactly as the rational number `amount_per_share_minor_numerator / amount_per_share_minor_denominator` in the row currency's ISO minor unit. The denominator is a positive integer. This permits sub-minor-unit per-share market data without storing money as `REAL`; round only the final cash total through the shared currency helpers.
- **Dates:** stored as `TEXT` in ISO 8601 (`YYYY-MM-DD`). Timestamps `YYYY-MM-DD HH:MM:SS`.
- **Currencies:** stored as ISO 4217 strings ('ILS', 'USD'). Each monetary table has its own `currency` column — never assume.
- **Hebrew text:** stored as-is in UTF-8. SQLite handles this natively.
- **Sign convention for `transactions.amount_minor`:** positive = credit (money in), negative = debit (money out). Credit-card charges are negative; payments to the card are positive.
- **Accounting class:** `transactions.flow_type` is the small, stable calculation dimension: `expense | income | transfer | investment | tax | refund | other`. `category` stays open-vocabulary for human detail. New ingestion sets both with judgment; legacy rows may have `flow_type=NULL` and use the compatibility mapping in `scripts/findash_core/categories.py`.
- **The `component` column** (on `balances`, `transactions`, and canonical cash events) is the only non-obvious thing:
  - `NULL` for plain accounts (bank, brokerage, cash).
  - `tagmul_employee | tagmul_employer | pitsuyim` for pension.
  - `tagmul_employee | tagmul_employer` for study fund (no severance).
  - `cash_usd | cash_gbp | cash_ils | cash_<ccy>` for multi-currency brokerage cash (your brokerage, IBKR).
- **Accounts are economic identities; feeds are evidence sources.** A wrapper statement and an underlying API can describe one portfolio. They are two `account_feeds` rows attached to one `accounts` row, never two values to add. See [Account identity and event evidence](#account-identity-and-event-evidence).
- **Live-API sources** have `source_doc_id = NULL`, but their `source_events` rows still identify the provider feed and stable provider key. See [live-sources.md](./live-sources.md).

## Tables, at a glance

| Table | One row per | Notes |
|---|---|---|
| `documents` | source file ingested | natural key: `drive_id`. Locally staged bank pairs use `local:fetch:<filename>:<sha256-first-12>`; `raw_hash` + `notes` survive after staging cleanup. Every document fact links here; live APIs use explicit feed/source-event provenance instead |
| `accounts` | logical economic account / fund / card | `kind` is open vocab. `live_source` is retained only for migration compatibility; new mappings belong in `account_feeds` |
| `account_feeds` | provider/source view of an account | many feeds can point to one account; a provider account cannot point to two logical accounts |
| `balances` | canonical (account, as_of, component) snapshot | the expression unique index treats plain-account `component=NULL` as one value, closing SQLite's NULL uniqueness hole |
| `balance_evidence` | source observation supporting a balance | preserves every feed/document observation; conflicting observations are reconciled into one balance rather than summed; like position evidence, it can carry the explicit complete-account marker |
| `transactions` | raw money-flow observation | `flow_type` owns accounting meaning; `source_event_id` and the optional one-to-one `canonical_event_id` link new observations without erasing legacy rows |
| `securities` | tradable ticker | includes benchmarks (e.g. `SPY`) |
| `trades` | raw buy/sell observation | `external_id` deduplicates only unlinked legacy rows; feed-backed rows use the feed-scoped `source_event_id`, while an optional `canonical_event_id` must agree with that source's one-to-one evidence |
| `canonical_events` | economic cash movement or trade | calculations count each confirmed row once; cash preserves its normalized `component`, while trade components must be null |
| `source_events` | stable feed observation | unique by `(account_feed_id, source_key)`; `source_key` is a provider id or document + row identity |
| `event_evidence` | source↔canonical link | source of truth for normalization: two feeds can support one event without deleting either raw row; `aggregate` supports one-to-many statement/fill matching and therefore has no raw canonical pointer |
| `trade_cash_links` | explicit cash↔trade relationship | says one confirmed canonical cash event exactly records settlement for one or more confirmed canonical trades; each trade has at most one settlement-cash event, avoiding ambiguous partial replacement |
| `transfer_links` | own-account transfer | joins the outflow leg to the equal inflow leg on another account; either leg may be an expected `needs_review` leg until its cash is observed |
| `fx_conversions` | currency conversion inside one account | joins the sold and bought cash legs and stores the user's `actual_rate` beside the day's `market_rate`; conversions change cash composition, not external capital |
| `dividend_events` | dividend payment | ties the income cash event to its security and ex-date; carries gross/withholding when a source shows them and the expected amount when predicted |
| `ledger_log` | ledger write | one row per intents request, reconcile run, or raw-SQL escape (reason only, never values) |
| `positions` | (account, security, as_of) observation | as-reported checkpoint used as an anchor/reconciliation source when event history is incomplete—not a second portfolio to add |
| `position_evidence` | source observation supporting a position | preserves overlapping wrapper/API snapshots while one `positions` row remains the chosen anchor; snapshot evidence can set `account_snapshot_complete=1` only when the same read covered every holding **and** every cash component for the account |
| `prices` | (security, date) close | for valuation and benchmark comparison |
| `security_distributions` | one provider distribution event for a security | cached market reference data keyed by `(security_id, source, source_key)`; the exact rational per-share amount can model entitlement but is not evidence that the account received cash |
| `security_distribution_refreshes` | latest provider refresh outcome for one security | distinguishes a confirmed empty result from failed or never-fetched coverage; it never represents a cash event |
| `fx_rates` | (date, base→quote) | reporting currency is ILS. Market reference rates (`'yahoo'`, filled by `scripts/refresh_prices.py`). A conversion's own rate lives on `fx_conversions`, never here. Older `'document'` rows (card/bank statement rates) are still honoured by valuation lookups and never overwritten by the refresh |
| `security_price_observations` | append-only provider quote observation | integer `price_minor`, timestamp, currency, and source; used only for the localhost live dashboard |
| `fx_rate_observations` | append-only provider FX observation | timestamped positive rate used only for live valuation; it never replaces an authoritative transaction rate |
| `payslips` | one payslip | structured columns for common bits |
| `payslip_line_items` | one row from a payslip | catch-all for the long tail (bonuses, allowances, custom deductions) |
| `meta` | flow-state key | `last_db_backup_at` gates the weekly Drive backup; run-journal keys (`last_sync_at`, `last_ingest_<source>_at`, `last_ibkr_at`) feed the doctor's staleness warnings. Versioned migration creates it on databases that predate the table |
| `schema_migrations` | applied schema migration | managed only by `scripts/migrate_db.py` / `findash_core.db.connect_db` |

## Account identity and event evidence

An `accounts` row answers **what economic account is this?** An `account_feeds` row answers **where did this observation come from?** For example:

```text
accounts: Wrapper brokerage
  ├─ account_feeds: wrapper statements
  └─ account_feeds: IBKR API
```

Both feeds may report the same trade. Keep both raw `trades` rows, give each a stable `source_events` identity, then link both through `event_evidence` to one `canonical_events` trade. The accounting layer counts the canonical row once. Never merge solely because amount and date match: two genuine fills can be identical. Stable provider ids win; otherwise compare security, side, quantity, price, currency, dates, fees and surrounding account state with judgment. Ambiguous matches stay `needs_review` rather than being guessed.

The ledger engine (`scripts/findash_core/ledger/`) writes each normalized observation in one transaction, in dependency order: the `source_events` identity, the canonical event(s), `event_evidence`, then the raw `transactions`/`trades` row. Skills describe the observation as an intent ([database-operations.md](./database-operations.md)) and never write these tables directly. A non-null raw `canonical_event_id` is only a one-to-one convenience pointer: its source has exactly one evidence link, which is non-aggregate and targets that same canonical row; aggregate evidence leaves the pointer null.

A trade changes both holdings and brokerage cash. When no posted cash observation exists, accounting derives that cash leg from the confirmed canonical trade. When a feed separately reports the posted settlement cash, retain it as a confirmed canonical cash event and add `trade_cash_links` rows to every canonical fill it covers. One cash event can deliberately cover several fills, but one trade cannot be split across several canonical cash events: aggregate split source rows into one reviewed canonical settlement or leave them unlinked until their economics are unambiguous.

Movements that span several canonical events are joined by one explicit link.
`transfer_links` pairs an own-account outflow with its equal inflow; the side not
yet observed is an *expected leg* (`needs_review`) and does not count until
confirmed by evidence or by the daily reconcile. `fx_conversions` pairs the sold
and bought legs inside one account and keeps the user's rate beside the market
rate, so the broker's markup stays measurable. External capital counts only the
brokerage transfer legs; conversions change which currency is held.
`dividend_events` ties a dividend's cash to its security and ex-date, which lets
reconcile predict a payment and confirm it when the cash appears. Linked economics
are immutable; an expected leg may still be re-dated and confirmed.

The trade-cash link is an exact reviewed assertion. Cash and trades must share account and currency, be confirmed, and the cash date must be on or after every covered trade date. Its signed amount must equal the linked fills' economics—buy `= -(quantity × price_minor) - fees_minor`, sell `= quantity × price_minor - fees_minor`—within `max(1, number of fills)` minor units for fractional-fill rounding. Amount/date similarity alone never creates a link. Once linked, the events' dates, amounts, security, side, quantity, price, fees, currency, component, status, and other accounting identity are immutable; notes may still be edited. A correction requires explicit review rather than changing facts underneath the link. An unlinked cash event remains a separate movement. A linked trade stays derived before the posted cash date; from that date onward the explicit cash replaces it, so later evidence never rewrites earlier history.

The same principle applies to snapshots. `balances` and `positions` hold chosen anchors; `balance_evidence` and `position_evidence` retain every source observation. A second feed is corroboration or a reconciliation conflict, not another asset.

Resolve account and event identity before attaching evidence. Once dependent evidence/raw facts exist, the stable identity is immutable: feed account/provider/reference; source feed/key/type; canonical account/type; balance account/date/component; and position account/security/date. Raw canonical pointers and evidence links must remain mutually consistent, and linked settlement economics cannot be edited in place. Correct a bad mapping through the reviewed reconciliation operation; never repoint a parent row and leave its evidence contradictory.

For a reviewed legacy duplicate, use the fixed `findash_db.py reconcile-account`
operation described in [database-operations.md](./database-operations.md). Normal
write batches cannot delete evidence or bypass identity guards. Snapshot evidence
must always cite a document, an account feed, or both.

**Snapshot completeness must be explicit.** Omission means zero only when the source is known to cover the entire account and the whole read succeeded. For such an import, add a zero-quantity `positions` anchor (plus deterministic `position_evidence`) for each previously held security omitted by the source and explicit cash anchors, including zero, for every previously observed currency. Set `account_snapshot_complete=1` on the `balance_evidence` and `position_evidence` rows from that read only when both holdings and cash coverage are complete. The marker may live on balance evidence when the complete account has no positions. Complete holdings with missing/partial cash is still an incomplete account snapshot and leaves the flag at its default `0`. A screenshot, cropped table, failed/paginated response, or otherwise partial source writes only what it actually shows and never synthesizes zeros from absence. Existing evidence migrates to `0`; the system does not guess historical completeness. This explicit assertion is what permits detailed anchors to replace an older whole-account brokerage total.

Locked-account component snapshots are coherent sets too: pension requires all
three documented components on one date, and a study fund requires both. A
complete component set replaces an older plain total; a newer plain total or an
incomplete later breakdown does not get silently displaced by stale/partial detail.
Between statements, valuation adds later exact fund movements and, when exactly
one active account of the matching kind/currency exists, employee + employer
contributions from later `payslips`. Payroll roll-forward is calculated rather than
materialized as a transaction, is marked estimated because market return is
unknown, and is reset by the next observed fund snapshot. Exact post-snapshot fund
movement coverage takes precedence so the same payroll contribution is not counted
twice. Ambiguous multiple-policy routing produces a warning and no estimate.

Legacy `transactions` and `trades` remain valid with null event links. An isolated explicit investment-cash row still applies. Where legacy investment cash overlaps eligible raw trade history, calculations preserve the old trade-derived treatment and warn because those rows do not prove which cash and trade belong together; migration never invents `trade_cash_links`. New ingestion creates the source identity, canonical event, evidence link, and consistent raw fact atomically. Copy a cash observation's normalized `component` onto its canonical event; this keeps pension/cash sub-ledgers intact across feeds. This makes migration incremental instead of rewriting historical financial facts automatically.

## Worked example: a pension statement

A Harel pension PDF generally produces:

- 0 new rows in `accounts` (account already exists; created once).
- **3 new rows in `balances`** for the snapshot date: one per component (`tagmul_employee`, `tagmul_employer`, `pitsuyim`).
- **N×3 new rows in `transactions`** where N is the number of monthly deposits shown — one transaction per (month, component). Categorize as `pension_deposit`. The employer is the `counterparty`. The salary month goes in `description` or `reference`.
- Those deposit rows use `flow_type='transfer'`: they move owned value into retirement savings; they are not consumer expense or salary income a second time.
- 1 row in `documents`.

## Worked example: a bank statement (XLSX)

Each row in the bank's transactions table becomes one intent (usually `cash`; a movement between owned accounts is a `transfer`). Use judgment for `category`:

- `salary` when description says משכורת / payroll
- `transfer` when both sides are accounts you own (Hapoalim → the brokerage account is a transfer, *not* an expense)
- `card_payment` + `flow_type='transfer'` for a checking-side consolidated card payment; merchant-level card rows remain expenses
- `dividend` when the row semantically records a cash distribution, regardless
  of the provider's label or language
- Otherwise pick a sensible category from open vocab — recurring categories will emerge naturally.

The bank's running balance can optionally be inserted into `balances` once per statement (as_of = last row's date), with the source observation in `balance_evidence`.

## Useful queries

```sql
-- Most recent balance rows per account, still in each row's own currency
WITH latest AS (
  SELECT account_id, MAX(as_of) AS as_of
  FROM balances GROUP BY account_id
)
SELECT a.name, b.amount_minor, b.currency
FROM latest l
JOIN accounts a ON a.id = l.account_id
JOIN balances b ON b.account_id = l.account_id AND b.as_of = l.as_of;

-- Confirmed canonical position deltas per account/security
SELECT s.ticker,
       SUM(CASE WHEN e.side='buy' THEN e.quantity ELSE -e.quantity END) AS shares
FROM canonical_events e JOIN securities s ON s.id=e.security_id
WHERE e.event_type='trade' AND e.status='confirmed'
GROUP BY e.account_id, e.security_id HAVING ABS(shares) > 1e-9;

-- Cumulative deposits to brokerage, kept in integer minor units per currency
SELECT occurred_on,
       currency,
       SUM(amount_minor) OVER (
         PARTITION BY currency ORDER BY occurred_on
       ) AS cumulative_amount_minor
FROM canonical_events
WHERE account_id=<brokerage_account_id>
  AND event_type='cash' AND flow_type='transfer' AND status='confirmed'
ORDER BY occurred_on;
```

The final example is intentionally account-local. The cross-account relationship
lives in `transfer_links`; application code uses normalized signed external flows
for performance calculations.

## Schema lifecycle

- Fresh database: `python3 scripts/migrate_db.py --init`.
- Existing database: `python3 scripts/migrate_db.py`.
- Health check: `python3 scripts/migrate_db.py --check`.
- Application scripts open SQLite through `findash_core.db.connect_db`, which enables `PRAGMA foreign_keys=ON`, rejects pre-existing orphan rows by table/count only, and applies pending migrations before writes.

Migration refuses to guess when an old database contains different balances for the same `(account, date, component)`. Identical duplicates are collapsed with all evidence retained; conflicting values require a human source-priority decision.

Migration v3 also audits existing rows before enabling write guards. It reports only affected table names and counts—never private values—and stops until malformed dates/currencies, non-integer money, invalid quantities/prices, empty components, or non-positive FX rates are corrected. The same rules then apply to inserts and updates.

Migration v4 adds `canonical_events.component` so normalized cash events retain their sub-ledger identity; canonical trades require it to remain null.

Migration v5 rejects cross-account snapshot evidence and prevents deleting a feed while source, balance, or position evidence still depends on it.

Migration v6 audits parent/child account and event-type consistency, then prevents linked feed, source-event, canonical-event, balance, and position identities from being reassigned behind existing evidence.

Migration v7 completes those stable-identity guards and scopes legacy provider trade ids so two feeds on one logical account cannot collide.

Migration v8 adds `trade_cash_links`. It does not infer any relationship from old amounts or dates: legacy rows keep their prior treatment until a human normalizes them from source evidence.

Migration v9 requires every balance/position evidence row to cite either a document or an account feed. Existing uncited snapshot evidence is attached to a generated per-account legacy manual feed without changing the canonical snapshot value.

Migration v10 makes complete-account snapshots explicit and prevents one trade from being replaced by several canonical settlement-cash events.

Migration v11 audits raw-pointer/evidence consistency and linked settlement chronology/economics before upgrading. It then enforces those relationships on every write, makes linked economics immutable, and restores ordinary lookup indexes that may be absent from very old databases. Failures report only relationship names and counts, never financial rows.

Migration v12 adds the cached `security_distributions` market-data table and its per-security `security_distribution_refreshes` coverage state. Its positive rational integer representation preserves exact sub-minor per-share amounts; source identities are stable and provider-scoped. These rows support modeled dividend entitlement and projections, not claims about cash actually received.

Migration v13 adds append-only security-price and FX-rate observations while
retaining deterministic daily cache rows for valuation.

Migration v14 adds guarded `inferred_transfer_links`. It permits a positive
brokerage transfer only when confirmed bank evidence and two complete cash
snapshots from one active feed reconcile the full interval. Linked event economics
and snapshot identities become immutable.

Migration v15 adds guarded `funding_fx_links` for a reviewed bank transfer followed
by a complete documented brokerage conversion. It keeps external funding separate
from the two internal FX cash legs and makes all linked economics immutable.

Migration v16 introduces the ledger-engine relationships: `transfer_links`,
`fx_conversions`, `dividend_events` and `ledger_log`. Existing v14/v15 links move
into `transfer_links` and `fx_conversions` (a conversion document's rate becomes the
conversion's `actual_rate` and leaves `fx_rates`), then the v14/v15 tables are
dropped. Older databases still replay v14 and v15 first.
