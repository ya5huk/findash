# Ledger engine design

Status: approved and phase 1 implemented 2026-10-26.

## Problem

Money movements are written as raw SQL that Claude composes by hand. A single
economic fact ("₪10,000 funded the brokerage") is secretly a multi-row invariant:
bank leg, brokerage leg, FX legs, evidence rows, link rows. Nothing enforces it,
so it silently fails:

- October 2026: two bank transfers to the brokerage were recorded on the bank side
  only. Net funding never moved; the portfolio line showed the cash as "gains".
- Funding exists in two shapes: older rows are a USD "deposit" with the ILS amount
  and rate only in description text; newer rows are an ILS deposit + FX pair.
- Document FX rates overwrite the market rate for that day, so the broker's markup
  can't be measured.

## Principle

**Claude interprets; code does bookkeeping.**

- Claude decides what a movement *is* and writes a typed **intent** holding only
  the facts it read.
- The engine derives everything derivable (missing amounts, rates, market rates,
  components, counterpart legs, links, dedup keys, status transitions) and writes
  every row atomically.
- Contradictory facts are rejected, never "fixed".

## Write path

```
Claude ──intents.json──▶ findash_db.py apply ──▶ ledger engine ──▶ SQLite (one transaction)
```

- Request: `{"intents": [...]}` under `inbox/staging/operations/`, same lifecycle
  as today.
- **Ledger tables are intent-only**: `transactions`, `trades`, `canonical_events`,
  `source_events`, `event_evidence`, `trade_cash_links`, `transfer_links`,
  `fx_conversions`, `dividend_events`. Plain `statements` requests targeting them
  are rejected.
- **Escape hatch**: a `raw_sql` intent with a written reason. Logged in
  `ledger_log`; the doctor flags recent use.
- Phase 2 (done the same day): reference intents for documents, accounts,
  feeds, securities, prices, `meta`, balances, positions, whole brokerage
  snapshots and payslips. Plain `statements` requests were removed.

## Intent vocabulary

Every intent names its account by id or exact name, uses major-unit decimal
amounts, and cites evidence: a document row, a live feed key, or an existing raw
transaction to adopt.

| Intent | Claude provides | Engine derives |
|---|---|---|
| `cash` | account, date, amount, currency, flow_type, category, evidence | raw row, source identity, canonical event, brokerage cash component; `same_as_event` attaches a second feed as supporting evidence |
| `transfer` | from/to accounts, date, amount, currency, evidence for the side(s) seen | both legs + `transfer_links`; a side without evidence becomes an **expected leg** (`needs_review`) |
| `fx_conversion` | account, date, sell, buy currency, two of {sell amount, buy amount, rate}, evidence | the third value, both legs, market rate for that day, `fx_conversions` row |
| `trade` | account, date, ticker, side, shares, price, fees, evidence | raw trade, canonical trade, derived cash |
| `trade_settlement` | posted cash event, trade events | validated `trade_cash_links` |
| `dividend` | account, ticker, pay date, gross/withholding/net (what was seen), evidence | net amount; confirms a matching expected dividend instead of duplicating |
| `confirm` / `exclude` | event, optional observed amount/date/evidence | status change, evidence link |
| `raw_sql` | reason, statements | logged escape hatch |

## FX conversions: two rates

`fx_conversions` ties the two legs and stores:

- `actual_rate` — what the user got (document, or implied by the two amounts)
- `market_rate`, `market_rate_date` — Yahoo close on the conversion date
- `converted_on` (market-rate date) and `settled_on` (when cash arrives; leg date)

Markup is computed, not stored. `fx_rates` holds market rates only for
conversions: document conversion rates move onto `fx_conversions` and the days
they overwrote are backfilled from Yahoo.

Observed markup is 0.6–1.7% (broker spread; no separate fee). Matching band:
actual rate from **1% better to 2.5% worse** than market.

## Review lifecycle

```
expected (needs_review) ──match──▶ confirmed
                        └─explicit─▶ excluded
```

- `transfer` without destination evidence → expected leg.
- Dividends are **predicted**: for each Yahoo ex-date where the account held
  shares, net = shares × per-share × (1 − withholding), withholding learned from
  history (default 25%).
- Expected legs and dividends don't count in calculations until confirmed.

## Reconcile (daily, agent-only)

`findash_db.py reconcile` runs inside sync:

1. Create expected dividends for recent ex-dates.
2. Find **unexplained brokerage cash**: per cash currency, the change between
   consecutive snapshots minus every recorded event in between.
3. Auto-confirm unambiguous matches (exactly one candidate):
   - expected transfer in the same currency, amount within rounding
   - expected ILS transfer explained by a foreign-cash residual within the FX band
     → confirms the leg and records the implied conversion
   - expected dividend(s) within 2% → confirmed at the observed amount
4. Print the remaining **open items** as JSON for Claude, with age.

| Open item | Trigger |
|---|---|
| `expected_leg` | transfer counterpart still `needs_review` |
| `unexplained_cash` | snapshot change not covered by events |
| `stale_review` | any other `needs_review` event older than 14 days |
| `fx_rate_outside_band` | actual vs market outside the band |
| `missing_market_rate` | no Yahoo rate near the conversion date |
| `dividend_overdue` | expected dividend 45 days past ex-date |
| `raw_sql_used` | escape hatch used in the last 14 days |

Claude closes what it can with intents. Items older than **7 days** become `⚠️`
lines in the chat where sync runs. Nothing goes on the dashboard.

## Migration

Schema v16 `ledger_engine`:

- adds `transfer_links`, `fx_conversions`, `dividend_events`, `ledger_log`
- moves `funding_fx_links` and `inferred_transfer_links` rows into the new tables,
  then drops both tables and their triggers
- moves conversion-document rates onto `fx_conversions`

Data normalization (done through intents, by judgment, not by a script):

- older funding rows become bank `transfer`s (ILS, bank date and amount) plus
  `fx_conversion`s adopting each recorded conversion row
- the two October transfers and conversions are recorded

## Removed

- `findash_db.py infer-brokerage-funding`, `reconcile-funding-fx`
- `funding_reconciliation.py`, `funding_fx_reconciliation.py` and their tests
- SQL-composition guidance for ledger tables in skills and docs

Migrations v14/v15 stay registered as history (old databases replay them before
v16 removes their tables).

## Testing

- One test class per intent: facts in → rows out, contradictions rejected,
  rerun is a no-op.
- Reconcile fixtures replaying October (expected ILS leg + USD residual) and
  dividend confirmation.
- Migration test: v15 database with funding links upgrades losslessly.
- Real-database check: backup first; dashboard totals identical before/after
  except the corrected funding line.
