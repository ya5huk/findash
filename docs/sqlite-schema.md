# SQLite schema

Source of truth: [`scripts/init-db.sql`](../scripts/init-db.sql). This file explains the *intent* — the SQL file is the spec.

## Conventions

- **Money:** stored as `INTEGER amount_minor` in the currency's minor unit (agorot for ILS, cents for USD). Multiply input by 100, divide for display. Never use `REAL` for money.
- **Dates:** stored as `TEXT` in ISO 8601 (`YYYY-MM-DD`). Timestamps `YYYY-MM-DD HH:MM:SS`.
- **Currencies:** stored as ISO 4217 strings ('ILS', 'USD'). Each monetary table has its own `currency` column — never assume.
- **Hebrew text:** stored as-is in UTF-8. SQLite handles this natively.
- **Sign convention for `transactions.amount_minor`:** positive = credit (money in), negative = debit (money out). Credit-card charges are negative; payments to the card are positive.
- **The `component` column** (on `balances` and `transactions`) is the only non-obvious thing:
  - `NULL` for plain accounts (bank, brokerage, cash).
  - `tagmul_employee | tagmul_employer | pitsuyim` for pension.
  - `tagmul_employee | tagmul_employer` for study fund (no severance).
  - `cash_usd | cash_gbp | cash_ils | cash_<ccy>` for multi-currency brokerage cash (your brokerage, IBKR).
- **Live-API sources** (e.g. IBKR via the `fetch-investments` skill) write fact rows with `source_doc_id = NULL` — the deliberate exception to the audit-trail rule. Idempotency then comes from each table's UNIQUE key, not a `documents` row. See [live-sources.md](./live-sources.md).
- **Trade-fed vs snapshot-fed accounts.** An account with `trades` rows is *trade-fed* (positions derived from the ledger — true cost basis + the stocks-vs-S&P benchmark); an account with only `positions` rows is *snapshot-fed* (valued from the snapshot). The renderer decides this **by data**, never by a hardcoded account id — so the two paths never value the same holding twice.

## Tables, at a glance

| Table | One row per | Notes |
|---|---|---|
| `documents` | source file ingested | natural key: `drive_id`. every fact links back via `source_doc_id` — except live-API sources (see [live-sources.md](./live-sources.md)) |
| `accounts` | account / fund / card | `kind` is open vocab; new types don't need a new table |
| `balances` | (account, as_of, component) snapshot | pension has 3 rows per snapshot |
| `transactions` | money flow | use judgment for `category` and `counterparty` — see [doc-types/classification.md](./doc-types/classification.md) |
| `securities` | tradable ticker | includes benchmarks (e.g. `SPY`) |
| `trades` | buy/sell event | positions are derived, never stored. `external_id` = live-source trade id (e.g. IBKR), `NULL` for document/screenshot trades; partial unique index `(account_id, external_id)` dedups connector re-runs. Connector trades carry `source_doc_id` NULL |
| `positions` | (account, security, as_of) snapshot | as-reported holdings from a live API (e.g. IBKR); the snapshot counterpart to `trades`. For a trade-fed account it's a reconcile/bootstrap cross-check; it's the primary value source only for a snapshot-fed account (one with no `trades`). `source_doc_id` is NULL — see [live-sources.md](./live-sources.md) |
| `prices` | (security, date) close | for valuation and benchmark comparison |
| `fx_rates` | (date, base→quote) | reporting currency is ILS. `source` column: `'document'` = extracted from a Drive doc (the rate the user actually transacted at — authoritative, never overwritten by the Yahoo refresh); `'yahoo'` = filled by `scripts/refresh_prices.py` |
| `payslips` | one payslip | structured columns for common bits |
| `payslip_line_items` | one row from a payslip | catch-all for the long tail (bonuses, allowances, custom deductions) |

## Worked example: a pension statement

A Harel pension PDF generally produces:

- 0 new rows in `accounts` (account already exists; created once).
- **3 new rows in `balances`** for the snapshot date: one per component (`tagmul_employee`, `tagmul_employer`, `pitsuyim`).
- **N×3 new rows in `transactions`** where N is the number of monthly deposits shown — one transaction per (month, component). Categorize as `pension_deposit`. The employer is the `counterparty`. The salary month goes in `description` or `reference`.
- 1 row in `documents`.

## Worked example: a bank statement (XLSX)

Each row in the bank's transactions table becomes one row in `transactions`. Use judgment for `category`:

- `salary` when description says משכורת / payroll
- `transfer` when both sides are accounts you own (Hapoalim → the brokerage account is a transfer, *not* an expense)
- `card_charge` for "כאל" credit card consolidation entries
- `dividend` for ני"ע-דיבידנד
- Otherwise pick a sensible category from open vocab — recurring categories will emerge naturally.

The bank's running balance can optionally be inserted into `balances` once per statement (as_of = last row's date).

## Useful queries

```sql
-- Net worth in ILS as of today (most recent balance per account)
WITH latest AS (
  SELECT account_id, MAX(as_of) AS as_of
  FROM balances GROUP BY account_id
)
SELECT a.name, b.amount_minor, b.currency
FROM accounts a
JOIN latest USING (account_id)
JOIN balances b ON b.account_id = latest.account_id AND b.as_of = latest.as_of;

-- Current positions per security
SELECT s.ticker,
       SUM(CASE WHEN t.side='buy' THEN t.shares ELSE -t.shares END) AS shares,
       SUM(CASE WHEN t.side='buy' THEN t.shares*t.price_minor ELSE -t.shares*t.price_minor END) / 100.0 AS cost_basis
FROM trades t JOIN securities s ON s.id = t.security_id
GROUP BY s.ticker HAVING ABS(shares) > 1e-9;

-- Cumulative deposits to brokerage
SELECT date, SUM(amount_minor) OVER (ORDER BY date) / 100.0 AS cumulative_minor
FROM transactions
WHERE category = 'transfer'
  AND counterparty LIKE '%<your brokerage>%'  -- match your brokerage's counterparty string
  AND amount_minor < 0  -- outflows from checking = deposits to brokerage
ORDER BY date;
```
