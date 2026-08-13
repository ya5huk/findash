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

- **Provenance is explicit in `account_feeds` + `source_events`.** The feed identifies the provider account;
  `(account_feed_id, source_key)` identifies the provider event. Re-fetching the same provider id is a no-op.
- **Canonical events are counted once.** `event_evidence` can attach an IBKR observation and a wrapper-statement
  observation to the same `canonical_events` row while retaining both raw facts.
- **Snapshots keep evidence too.** Live balance observations attach through `balance_evidence`; they do not
  become a second balance merely because another source reported the same account.

A synthetic `documents` row (no Drive file) was considered as a way to keep #6 literally intact, and
rejected in favor of this simpler model.

## Yahoo market reference data

`scripts/refresh_prices.py` sends listed security symbols and ISO currencies to Yahoo Finance and caches
closing prices, FX rates, and per-share distribution events. It does **not** send account identifiers,
quantities, balances, transaction text, or credentials. The renderer itself performs no network access.
Each selected security also gets an aggregate refresh outcome (`success`, `empty`, or `failed`) so a
confirmed non-dividend security is distinguishable from missing coverage. Failed refreshes keep the last
known-good events and remain visibly incomplete; successful refreshes remove provider events that vanished.

Distribution events are reference data, not proof of a brokerage cash payment. The dashboard multiplies
them by reconstructed shares held before the event date to show gross modeled entitlement, then projects
recent cadence against current holdings for an explicitly estimated upcoming/next-year view. Modeled rows
never enter `transactions`, recorded-cash history, or net worth. Until account-activity coverage is reliable,
the cash-flow dividend component—and its corresponding income/net contribution—uses modeled entitlement;
the model retains recorded dividend cash separately for a future confirmation view. Only a statement or
account-activity feed can establish recorded dividend cash or tax.

## Interactive Brokers (IBKR)

Pulled by the [`fetch-investments`](../skills/fetch-investments/SKILL.md) skill through the **official
Interactive Brokers connector** — Anthropic's certified IBKR connector, added by the user via Claude's
connector directory (a claude.ai connector, *not* declared in `plugin.json`; its tool names are
connector-specific, discovered at run time). **Interactive auth:** it authenticates solely in a hands-on
Claude session. `sync-finance-data` attempts `fetch-investments` best-effort (gated on IBKR having been onboarded) —
interactive runs pull fresh data; unattended runs skip with a `⚠️` summary warning (relayed through Telegram when configured).
Manual runs work any time. Read-only. IBKR is attached as an **account feed** to one logical findash account
(see *Account mapping* below). Its raw trade observations remain in `trades`; canonical events prevent the same
trade from being counted again when a wrapper statement also reports it. Positions snapshots are anchors and
reconciliation checkpoints when the available event history does not reach the account's opening date.

### Tools → tables

Use only the connector's **read** tools (match by function — exact names vary per connector):

| read tool (by function) | what it returns | populates |
|---|---|---|
| account summary | net-liquidation value, account id(s), base currency | the **probe**; account-feed identity; reconciliation |
| account trades | per trade: id, symbol, side, size, price, commission, time | `source_events` + raw `trades` + one matched/created `canonical_events` row and `event_evidence`; `securities` on first sighting |
| account balances / cash | cash by currency | canonical `balances` anchor + `balance_evidence` for the IBKR observation |
| positions / holdings | holdings: symbol, qty, avg cost, market price, market value, currency | `positions` plus `position_evidence` (one row per holding) — for **reconciliation + bootstrap**, not primary valuation; `securities` on first sighting |
| price snapshot / history | quotes (esp. LSE/GBP symbols Yahoo prices poorly) | optional `prices` enrichment (`INSERT OR IGNORE`) |

**Never** call a tool that places/modifies/cancels an order or moves funds — this source is read-only.
*(An allocation / performance breakdown panel is a possible future enrichment — not yet built.)*

The accounting layer never chooses an entire account-wide “trade-fed” or “snapshot-fed” mode. For each
security/cash component it starts from the latest trustworthy snapshot at or before the requested date and
applies confirmed canonical events after that anchor. A later snapshot reconciles the derived state and starts
a new segment when history is incomplete. The snapshot and events are therefore complementary evidence,
never two holdings to add.

Omission closes a holding only after a confirmed **complete** positions read. On a complete account-wide
response, write deterministic zero-quantity position/evidence anchors for previously held securities that are
absent. Mark `account_snapshot_complete=1` on the resulting `position_evidence` and `balance_evidence` rows
only when the same successful read also proves complete cash coverage, with explicit zero cash anchors for
omitted prior currencies. A complete empty portfolio therefore records the marker on its zero cash evidence;
it never invents a security sentinel. Complete holdings without complete cash still leave the flag at `0`. If
pagination, connector failure, filtering, or tool semantics make either side partial, write only returned facts
and never interpret absence as zero.

### Account mapping

IBKR rarely deserves its own logical account: a broker already tracked from statements may itself be an
IBKR wrapper. A second account would double-count the portfolio. The mapping is a row in `account_feeds`
with `provider='ibkr'`; it points either to the existing wrapper account or to a newly-created account for an
IBKR-only user. Resolve it in that order: existing feed mapping, legacy marker/config, clear holdings/trade
overlap, create only when no brokerage evidence exists, and ask when genuinely ambiguous. Keep separate
IBKR provider account ids as separate feed rows if the connector exposes several subaccounts, while mapping
them to the same logical account only when the evidence says they are one economic portfolio.

### Judgment calls

- **Source identity first; economic matching second.** The IBKR trade id is the `source_events.source_key`
  and deduplicates connector reruns. Then compare it with canonical events already supported by wrapper
  statements/screenshots. Security, side, quantity, price, currency, dates and fees all matter. Equal date and
  amount alone are not enough—two genuine fills can be identical. Link high-confidence matches as supporting
  evidence; leave ambiguity for review instead of deleting either raw row.
- **Write normalized events atomically.** In one transaction, upsert the source identity, find/create the
  canonical event(s), insert `event_evidence`, then write the raw fact. A non-null raw `canonical_event_id`
  requires exactly one evidence link, which must be non-aggregate and target that same canonical row.
  Aggregate evidence keeps the raw pointer null. Never commit a pointer first and repair its evidence later.
- **Trade currency comes from the security, not the trade.** `get_account_trades` carries no currency
  field, so set `trades.currency` from the matched `securities.currency`. Normalize through
  `findash_core.currencies`: provider quote-unit aliases become canonical ISO currencies and the correct
  currency exponent is applied once.
- **Posted trade cash is explicit, never inferred as a duplicate.** A canonical trade supplies derived
  brokerage cash only while no confirmed canonical cash event is linked to it. If account activity also
  reports the actual settlement cash, retain that cash observation and add `trade_cash_links` only after
  matching account, currency, trade/fill ids, full economics, dates and fees. The posted date cannot precede
  a covered fill, and signed settlement must match the fill sum within `max(1, fill count)` minor units for
  fractional rounding. One posted cash row may link to several fills. Linked economics are immutable; equal
  amount/date alone is never enough, and ambiguity stays unlinked and `needs_review`.
- **Funding an IBKR account is a transfer, not an expense.** Money moving from a bank into IBKR is
  `category='transfer', flow_type='transfer'`. The bank and brokerage legs are separate canonical account
  events; consolidated spending counts neither.
- **Complete cash snapshots may prove a missing brokerage leg.** Reconstruct one
  currency between two complete snapshots from the same active feed. When the full
  unexplained positive residual is uniquely matched to confirmed bank outflows whose
  recipient/reference evidence names this brokerage, create the brokerage inflow
  through the guarded `infer-brokerage-funding` operation. The resulting
  `inferred_transfer_links` retain the bank event and both snapshot anchors. A
  portfolio-value bump, partial cash read, or amount/date match alone is never proof.
- **Cash-snapshot residuals can nominate dividends, not prove them.** Between two complete per-currency
  cash snapshots, advance the earlier anchor through confirmed cash events and trade settlements. Market
  price changes never affect cash. A remaining positive residual that aligns with modeled distributions,
  eligible holdings, currency, timing, and possible withholding is a reconciliation candidate only;
  interest, fees, tax, FX, transfers, corporate actions, and incomplete history remain alternatives. Keep
  the candidate private and count-only in reports. Do not write a confirmed dividend transaction without
  an account-activity or statement observation.
- **Currency / minor units.** Store `amount_minor` in the canonical ISO currency's minor unit. Use
  `major_to_minor` for major-unit amounts and `yahoo_price_to_minor` for provider quotes that may already be
  minor units; never hand-apply ×100. Track cash per currency via `component`
  (`cash_usd` / `cash_gbp` / `cash_ils` / …), reusing your brokerage's convention.
- **Short-position signs.** Keep quantity and market value signed: a short has
  negative `quantity` and negative `market_value_minor`, while its per-unit
  market price and average cost remain non-negative. This preserves net worth
  and P/L direction; never store an absolute short market value as an asset.
- **Commission sign.** `fees_minor` is the non-negative magnitude of the trading cost. Some provider fields
  use cash-ledger sign and return a commission debit as negative; normalize that to its absolute magnitude
  once before conversion so buys add the fee and sells subtract it exactly once.
- **Reconciliation, never fabrication.** Cross-check `get_account_summary` net-liquidation against
  Σ(position market values) + Σ(cash). On a material divergence (≳1%), note it (summary + `accounts.notes`).
  **Never** insert a balancing/plug row to force the totals to agree.
- **Don't duplicate a security you already hold.** If an IBKR symbol matches a security already tracked via
  your brokerage under a slightly different ticker, reuse the existing `securities` row (match on name/ISIN when
  obvious) rather than creating a near-duplicate.

### Failure is a skip, not an error

The IBKR connector authenticates interactively, so it simply isn't there in a headless run, and a user may
not have added it at all. `fetch-investments` is **best-effort, like a bank source**: any failure (connector
not connected, not authenticated, unavailable in this session, timeout) writes nothing, reports
`IBKR: skipped (<reason>)`, and — when IBKR was previously onboarded — adds a `⚠️ IBKR skipped` warning
bullet to the local summary, which Telegram relays when configured. Nothing aborts. The user adds/connects it via Claude's connector directory
(and confirms with `/mcp`) when convenient, then reruns the skill by hand.
