# Live sources

Most findash data is **document-centric**: a file lands in the Drive vault, `sync-finance-data` ingests
it, and every row cites a `documents.source_doc_id` (first-principle #6). This file owns the exception —
**live API sources** that write SQLite directly, with no Drive file and no `documents` row.

Document sources are catalogued in [`doc-types/`](./doc-types/README.md); this file is their live-API
counterpart. Schema lives in [`sqlite-schema.md`](./sqlite-schema.md).

## The `source_doc_id = NULL` convention

A live source has no document to cite, so its fact rows carry `source_doc_id = NULL`. This is a
**deliberate, documented relaxation** of first-principle #6, made per the project owner's decision — not
an accident, and not a license to skip provenance for *document* sources (those must always cite a doc).

What replaces the audit trail:

- **Idempotency comes from UNIQUE dedup keys, not a `documents` row.** Re-running a live fetch upserts the
  same rows (no duplicates) via each table's natural key.
- **Provenance is implicit in the account.** Rows on an `Interactive Brokers` account (`institution`) are,
  by construction, from the IBKR live source. Keep a one-line note in `accounts.notes` when something is
  worth recording (e.g. a reconciliation discrepancy).

A synthetic `documents` row (no Drive file) was considered as a way to keep #6 literally intact, and
rejected in favor of this simpler model.

## Interactive Brokers (IBKR)

Pulled by the [`fetch-investments`](../skills/fetch-investments/SKILL.md) skill through the **official
Interactive Brokers connector** — Anthropic's certified IBKR connector, added by the user via Claude's
connector directory (a claude.ai connector, *not* declared in `plugin.json`; its tool names are
connector-specific, discovered at run time). **Interactive-only:** it authenticates solely in a hands-on
Claude session, so `fetch-investments` is a manual step and is **not** part of the unattended `daily-run`
cron flow. Read-only. IBKR is **mapped onto a findash account you choose** (see *Account mapping* below)
and is **trade-fed**: the connector's trade history flows into the `trades` table — the same ledger a
screenshot-fed brokerage feeds from screenshots. A positions snapshot is written too, but only as a
reconciliation cross-check and a bootstrap for a brand-new account whose trades don't yet cover its
holdings.

### Tools → tables

Use only the connector's **read** tools (match by function — exact names vary per connector):

| read tool (by function) | what it returns | populates |
|---|---|---|
| account summary | net-liquidation value, account id(s), base currency | the **probe** that the connector is live; reconciliation; `accounts` metadata |
| account trades | per trade: id, symbol, side, size, price, commission, time | `trades` (one row per trade; `external_id` = IBKR trade id; `fees_minor` = commission; `currency` **derived from the matched security** — the trade payload carries none; `source_doc_id` NULL) + `securities` (on first sighting) |
| account balances / cash | cash by currency | `balances` (one row per currency, `component='cash_<ccy>'`, `source_doc_id` NULL) |
| positions / holdings | holdings: symbol, qty, avg cost, market price, market value, currency | `positions` (one row per holding, `source_doc_id` NULL) — for **reconciliation + bootstrap**, not primary valuation; `securities` on first sighting |
| price snapshot / history | quotes (esp. LSE/GBP symbols Yahoo prices poorly) | optional `prices` enrichment (`INSERT OR IGNORE`) |

**Never** call a tool that places/modifies/cancels an order or moves funds — this source is read-only.
*(An allocation / performance breakdown panel is a possible future enrichment — not yet built.)*

The `trades` vs `positions` split is **data-driven**, never hardcoded to an account id: an account with
`trades` rows is **trade-fed** (positions derived from the ledger — true cost basis and the stocks-vs-S&P
benchmark); an account with only `positions` rows is **snapshot-fed**, valued straight from
`market_value_minor`. IBKR maps onto a trade-fed account, so its positions snapshot is a reconciliation
cross-check (and a bootstrap until trades cover the holdings), **never** double-counted against the
trade-derived value.

### Account mapping

IBKR rarely deserves its own findash account: a broker you already track may itself be an IBKR wrapper
(some Israeli brokers are IBKR wrappers underneath), so a separate IBKR account would double-count net worth.
The [`setup`](../skills/setup/SKILL.md) skill asks once which existing account IBKR attaches to (or to
create a new one) and records the choice as `[ibkr] account_name=<findash account name>` in
`.secrets/findash`. `fetch-investments` reads it; if it's unset, it asks in-session and proceeds. No code
hardcodes an account name or id.

### Judgment calls

- **Trade handoff, by judgment — not a blind cutoff.** Ingest connector trades *after* the latest trade
  already on the mapped account, and around that boundary **cross-check** against existing trades (date /
  ticker / side / quantity) so one you already captured (e.g. an old screenshot) isn't ingested twice.
  `external_id` (the IBKR trade id) is the dedup key for connector re-runs; document/screenshot trades keep
  `external_id = NULL` and dedup via their source doc.
- **Trade currency comes from the security, not the trade.** `get_account_trades` carries no currency
  field, so set `trades.currency` from the matched `securities.currency` and normalize minor units the same
  way the renderer does (USD → cents; GBP → pence/`GBp` already-minor; ILA → agorot). A pence price stored
  as pounds is off by 100×.
- **Funding an IBKR account is a transfer, not an expense.** Money moving from a bank into IBKR is
  `category='transfer'` (same rule as Hapoalim → your brokerage; principle #2). Never categorize it as spend.
- **Currency / minor units.** Store `amount_minor` in the currency's minor unit. Multiply major amounts by
  100 — **except** values already in a minor unit: `GBp` / `GBX` (pence) and `ILA` (agorot) are integers
  already; do **not** ×100 again. `GBP` (pounds) and `USD` (dollars) do get ×100. Track cash per currency
  via `component` (`cash_usd` / `cash_gbp` / `cash_ils` / …), reusing your brokerage's convention.
- **Reconciliation, never fabrication.** Cross-check `get_account_summary` net-liquidation against
  Σ(position market values) + Σ(cash). On a material divergence (≳1%), note it (summary + `accounts.notes`).
  **Never** insert a balancing/plug row to force the totals to agree.
- **Don't duplicate a security you already hold.** If an IBKR symbol matches a security already tracked via
  your brokerage under a slightly different ticker, reuse the existing `securities` row (match on name/ISIN when
  obvious) rather than creating a near-duplicate.

### Failure is a skip, not an error

The IBKR connector is interactive-only, so it simply isn't there in a headless run, and a user may not have
added it at all. `fetch-investments` is **best-effort, like a bank source**: any failure (connector not
connected, not authenticated, unavailable in this session, timeout) writes nothing, pushes nothing, and
reports `IBKR: skipped (<reason>)` — nothing aborts. The user adds/connects it via Claude's connector
directory (and confirms with `/mcp`) when convenient, then reruns the skill by hand.
