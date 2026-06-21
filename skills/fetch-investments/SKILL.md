---
name: fetch-investments
description: Use when the user says "fetch investments", "fetch ibkr", "fetch interactive brokers", "pull portfolio", "fetch brokerage", "snapshot ibkr", or any portfolio-snapshot equivalent. Interactive-only — runs from a hands-on Claude session that has the official Interactive Brokers connector connected; it is NOT part of the unattended `daily-run` cron flow. Ingests the IBKR connector's trade history into the `trades` ledger on a user-mapped account (so it stops needing trade screenshots), plus a positions snapshot used for reconciliation + new-user bootstrap. Skips gracefully — best-effort, like a failed bank source — when the connector isn't connected.
---

# fetch-investments

You pull a live portfolio from Interactive Brokers (IBKR) via the official IBKR connector and write it **directly into SQLite**. The authoritative thing you ingest is the **trade history** — buys/sells go into the `trades` ledger on a **user-mapped account**, which is what powers holdings, the stocks-vs-S&P benchmark, and date-accurate cost basis. You *also* write a **positions snapshot** for reconciliation and new-user bootstrap. Unlike `fetch-bank-data` (which drops files in Drive `dump/` for sync to ingest), there is **no document and no Drive file** — IBKR is a live API source, written straight to the DB with `source_doc_id = NULL` (the deliberate audit-trail exception; see [`docs/live-sources.md`](../../docs/live-sources.md)). So you own both the DB write *and* the Drive push, the same way sync owns its backup step.

**This replaces the old screenshot workflow for new activity.** Historically the user fed trades by dropping screenshots into Drive `dump/`; the connector's trade feed makes that unnecessary going forward. Existing screenshot trades are left untouched — you only ingest connector trades *after* the latest trade already on the mapped account (see [Trades](#3-trades-the-core-judgment-not-mechanics)).

**No separate IBKR account.** Do **not** create a standalone "Interactive Brokers" account — that double-counts net worth (an IBKR brokerage *is* the user's portfolio, and for many users it's already tracked under another broker, e.g. an Israeli wrapper). IBKR data attaches to **one mapped findash account**, chosen by the user in `setup` and recorded as `[ibkr] account_name`.

**Interactive-only.** The IBKR connector is Anthropic's certified Interactive Brokers connector, added by the user through Claude's connector directory (it's a claude.ai connector, *not* a findash-declared MCP server). It authenticates only in an **interactive** Claude session, so this skill is a **manual** step — it is *not* chained into the unattended `daily-run` / `run_daily.sh` cron flow. Run it by hand (then re-render) when you want fresh holdings.

**Best-effort.** If the connector isn't connected (the user hasn't added it, or this is a headless session where it isn't available), **skip and report** — never abort. A user who doesn't use IBKR is just the normal skip case.

## Where things live

- IBKR connector: the official **Interactive Brokers (IBKR)** connector, added once by the user via Claude's connector directory (`+` → Connectors → Add connector → Browse connectors → search "ibkr"). It's a **claude.ai connector**, so it is *not* declared in `plugin.json`, and its tool names are connector-specific (not `mcp__plugin_findash_ibkr__*`) — **discover them at run time**, don't hardcode. Confirm it's live with `/mcp` (should read `Interactive Brokers (IBKR) · connected`).
- Config: the `[ibkr]` section of `.secrets/findash`, read via `scripts/lib/findash_secrets.py` → `read_section('ibkr')`:
  - **`account_name`** — the findash account IBKR maps onto (set by `setup`). Resolve it to an `accounts.id`. If absent, ask once and tell the user the line to add (see [Resolve the mapped account](#1-preconditions--resolve-the-mapped-account)).
  - `account_ids` / `base_currency` — optional (multi-account subset; non-default base ccy, default `ILS`).
- Local DB: `data/finance.db` (the live working copy; run after a sync so it's current — typically `/findash:sync-finance-data` (or `/findash:daily-run`) first, then this skill, then re-render).
- SQLite schema + conventions: [`docs/sqlite-schema.md`](../../docs/sqlite-schema.md) — the `trades` table (incl. `external_id`), the `positions` table, and the `source_doc_id = NULL` live-source convention.
- IBKR source shape + judgment calls: [`docs/live-sources.md`](../../docs/live-sources.md).
- rclone config: `./rclone.conf` (always pass `--config ./rclone.conf`). Drive root ID: `root_folder_id=…` in `.secrets/findash` `[drive]`.

## The IBKR connector tools

The connector exposes ~21 tools under the stable namespace `mcp__claude_ai_Interactive_Brokers_IBKR__*` (a mix of read/snapshot and trading tools). The namespace derives from the connector's display name, so it's the same for every user. They're deferred — `ToolSearch` for the read ones, e.g.:

```
select:mcp__claude_ai_Interactive_Brokers_IBKR__get_account_summary,mcp__claude_ai_Interactive_Brokers_IBKR__get_account_trades,mcp__claude_ai_Interactive_Brokers_IBKR__get_account_positions,mcp__claude_ai_Interactive_Brokers_IBKR__get_account_balances
```

then call them. If a name has drifted, re-discover by function from `/mcp`.

**Use only these read tools** (names below are under the `mcp__claude_ai_Interactive_Brokers_IBKR__` prefix):

| tool | what it gives you |
|------|-------------------|
| `get_account_summary` | net-liquidation value + account-level metrics, base `currency` — **your probe** that the connector is live |
| `get_account_trades` | **the core feed** — trade history for a period: trade id, symbol, side, size, price, commission, trade time (note: **no currency field** — derive it, see §3) |
| `get_account_positions` | holdings: symbol, quantity, avg cost, market price, market value, currency — for the reconcile/bootstrap snapshot |
| `get_account_balances` | cash by currency |
| `get_price_snapshot` / `get_price_history` | quotes (optional price enrichment, esp. LSE/GBP symbols Yahoo covers poorly) |

`get_account_trades` takes a `period` enum: `TODAY`, `DAYS_7/30/60/90`, `MONTH_TO_DATE`, `YEAR_TO_DATE`, and the completed-quarter buckets `LAST_QUARTER`…`FOUR_QUARTERS_AGO` (≈ 15 months of reach total). All boundaries are UTC.

**Never call the acting tools** — `create_order_instruction`, `delete_order_instruction`, `get_order_instructions` (it *drafts* a trade), or `provide_customer_feedback`. This skill is strictly read-only: it never drafts, places, modifies, or cancels an order, and never moves money. In an interactive run you're prompted before each tool call — **deny any non-read tool.**

## Flow

### 1. Preconditions + resolve the mapped account

- Read the `[ibkr]` config.
- **Resolve the mapped account** from `[ibkr] account_name`:
  - **Set, and the account exists** (`SELECT id FROM accounts WHERE name=?`, names are UNIQUE) → use that `id`.
  - **Set, but no such account yet** (the user chose "create new" in `setup`) → create one brokerage row: `kind='brokerage'`, `name=<account_name>`, `institution` from `get_account_summary` (or `'Interactive Brokers'`), `currency=<base ccy>`.
  - **Unset** → this is an interactive session, so ask once: show the existing accounts (`SELECT id,name,kind,institution FROM accounts ORDER BY id`), explain that IBKR holdings may already be tracked under another broker (e.g. some Israeli brokers are IBKR wrappers), and let the user pick attach-to-existing vs create-new. Use the choice for this run, and **print the exact line** `account_name=<name>` for them to paste under `[ibkr]` so future runs skip the prompt. (Don't write `.secrets/findash` yourself — same principle as `setup`.)
  - Everything below writes to this **one** mapped account. Never create a separate IBKR account.
- **Idempotent schema guards** (for DBs predating these — `scripts/init-db.sql` is the source of truth):
  - `positions` table: `CREATE TABLE IF NOT EXISTS positions (...)` mirroring the schema's `positions` block.
  - `trades.external_id` + its partial unique index — but a plain `ADD COLUMN` errors if it already exists, so guard with `PRAGMA table_info`:
    ```bash
    sqlite3 data/finance.db "SELECT 1 FROM pragma_table_info('trades') WHERE name='external_id';" | grep -q 1 \
      || sqlite3 data/finance.db "ALTER TABLE trades ADD COLUMN external_id TEXT;"
    sqlite3 data/finance.db "CREATE UNIQUE INDEX IF NOT EXISTS idx_trades_external ON trades(account_id, external_id) WHERE external_id IS NOT NULL;"
    ```
- Confirm `data/finance.db` exists. Run standalone with no DB? Tell the user to run sync first — don't fabricate one.

### 2. Probe (best-effort)

Find the connector's read tools, then call the **account-summary tool first** as the probe (it also gives the base `currency`). On **any** error — connector not connected, not authenticated, unavailable in this (e.g. headless) session, connection error, or timeout — **stop cleanly**: write nothing, push nothing, report `IBKR: skipped (<short reason>)`. A missing connector must never abort anything.

### 3. Trades — the core (judgment, not mechanics)

This is the point of the skill: bring the IBKR trade ledger into `trades` on the mapped account, deduped, so the benchmark and cost basis stay accurate without screenshots.

- **Cutoff = the latest trade already on the mapped account:** `SELECT MAX(date) FROM trades WHERE account_id=<mapped>`. You will ingest connector trades from the cutoff day onward (inclusive — re-scanning the boundary day is safe because dedup absorbs overlap; an *exclusive* `> cutoff` would silently drop a same-day trade). If the account has **no trades yet** (new user), there's no cutoff → pull the widest history available (bootstrap).
- **Window:** call `get_account_trades` with the *smallest* period that still covers `today − cutoff` (`DAYS_7/30/60/90` → `YEAR_TO_DATE` → chain the quarter buckets back to the ~15-month max). Periods overlap and that's fine — **`external_id` dedup is the only thing preventing double-count, so windows may overlap freely.** If the gap exceeds the connector's ~15-month reach (e.g. the user stopped screenshotting over a year ago), ingest what the window allows and **report the uncovered span** in the summary so they know a manual gap remains. No silent truncation.
- **Cross-check the handoff (judgment, principle #2):** for connector trades on/around the cutoff day, look at the trades already in the DB (date / ticker / side / quantity). If a connector trade clearly matches one already recorded (e.g. an old screenshot of the same fill), **don't duplicate it** — skip it, or backfill its `external_id` onto the existing row so future runs dedup cleanly. This is a reasoning step, not a blind date filter.
- **Map each connector trade → a `trades` row** on the mapped account:
  - `side` (`buy`/`sell`), `shares` (size), `price_minor` (per-share, minor units), `fees_minor` = commission, `date` (trade time → ISO date), `external_id` = the IBKR **trade id**, `source_doc_id` = NULL.
  - **Currency comes from the security, not the trade** — `get_account_trades` carries no currency field. Resolve it from the matched `securities` row (or the symbol's listing / a positions/price lookup), and store both the trade and its security in that currency.
  - **Minor-unit normalization** (mirror the renderer's convention so valuation agrees): USD → cents (×100), GBP **pounds** → pence (×100), but `GBp`/`GBX` and `ILA` are **already** minor units — do **not** ×100 again. An LSE trade mishandled here is off by 100×. See `docs/live-sources.md`.
  - **`securities` upsert by `ticker` with judgment:** reuse an existing security when an IBKR symbol matches one you already hold under a slightly different ticker (match on name/ISIN — e.g. `RR. @LSE` ↔ `RR.LSE`); only create a new row on a genuine first sighting.
  - **Dedup:** upsert on `(account_id, external_id)` (the partial unique index). Re-running the same period is a no-op.

### 4. Positions snapshot — reconcile + bootstrap (judgment, not a row)

Write a positions snapshot to the **mapped account** from `get_account_positions` + `get_account_balances`. Its job is now secondary: **reconciliation** (does the as-reported snapshot match the trade-derived holdings?) and **bootstrap** (a brand-new user with no trades yet still sees current holdings — the renderer values an account from trades when it has them, else from its latest snapshot).

- **`positions`** — one row per holding (`quantity`, `avg_cost_minor`, `market_price_minor`, `market_value_minor`, `currency`, `source_doc_id` NULL). Minor-unit rules as in §3 (watch `GBp`). Upsert on `UNIQUE(account_id, security_id, as_of)`.
- **`balances`** (cash) — one row per non-zero currency: `component='cash_<ccy>'` (reuse the per-currency convention), `source_doc_id` NULL, upsert on `UNIQUE(account_id, as_of, component)`.
- **Reconcile:** compare (a) the snapshot's per-symbol quantities against the trade-derived holdings on the mapped account, and (b) `get_account_summary` net-liquidation against Σ(position market values) + Σ(cash), in one currency. Material drift (≳1% — a missed trade, a corporate action, or history beyond the connector window) → **note it** in the summary and one line in `accounts.notes`. **Never** insert a balancing/plug row.

### 5. Prices (optional enrichment)

For held symbols Yahoo covers poorly (esp. LSE/GBP), pull `get_price_snapshot` / `get_price_history` and write `INSERT OR IGNORE INTO prices (security_id, date, close_minor, currency)` (the `OR IGNORE` keeps a document-sourced rate from being clobbered). Yahoo remains the fallback and the benchmark source. Prefer writing IBKR prices for *historical* dates the render-time Yahoo refresh won't `INSERT OR REPLACE` over.

### 6. Persist the DB to Drive (mirror sync step 6)

Only if you wrote rows. Mirror sync's backup exactly — overwrite the vault DB and drop a timestamped copy:
```bash
rclone --config ./rclone.conf copyto data/finance.db gdrive:finance.db --drive-root-folder-id=<ROOT_ID>
rclone --config ./rclone.conf copyto data/finance.db gdrive:backups/finance-<YYYY-MM-DD-HHMM>.db --drive-root-folder-id=<ROOT_ID>
```
On the skip path (step 2 failed), **do not push** — nothing changed.

### 7. Report + summary

- Append IBKR bullets to `data/last_sync_summary.md` under `## Ingested` (the same channel sync uses; if you re-render afterward, it rides along as the second Telegram message). One short sentence, counts only: `IBKR — 4 new trades ingested, 12 positions reconciled, cash in USD + ILS`. If a pre-window gap exists, say so: `… (trade history before <date> is screenshot-sourced; connector window doesn't reach it)`.
- Print one stdout status line:
  - success → `IBKR: ok (<N> new trades, <M> positions, <K> cash currencies)`
  - skip → `IBKR: skipped (<reason>)`

## Principles

- **Trade-fed, one mapped account.** Ingest the connector trade ledger onto the `[ibkr] account_name` account; never create a separate IBKR account (it double-counts net worth). Positions are reconcile + bootstrap, not the primary valuation.
- **Read-only at IBKR.** Never draft, place, modify, or cancel an order, and never transfer or move funds. Use only the connector's reporting tools; deny any other tool when prompted.
- **Interactive-only.** The IBKR connector authenticates only in a hands-on Claude session, so this skill is never chained into the unattended `daily-run` / cron flow. Run it manually, then re-render.
- **No document, by design.** IBKR rows carry `source_doc_id = NULL` — the documented exception to first-principle #6 (see `CLAUDE.md` + `docs/live-sources.md`). Idempotency comes from the UNIQUE dedup keys (`(account_id, external_id)` for trades; the snapshot keys for positions/balances), not a `documents` row.
- **Best-effort.** Connector missing / unauthenticated / unavailable → skip + report, never abort.
- **Judgment over mechanics.** The trade-handoff cross-check, deriving trade currency from the security, symbol-matching to existing securities, the transfer-vs-expense call on IBKR funding, the reconciliation note — all yours (principle #2). The only scripts here are `sqlite3` and `rclone`.
- **Privacy / log hygiene.** Never print balances, positions, account ids, prices, or net-liquidation values to stdout or the summary — counts and currencies only (principle #1; this repo is public).
- **Atomicity.** Wrap the mapped account's writes in one transaction; push the DB only after they commit.
- **Money as integers.** `price_minor` / `fees_minor` / `amount_minor` in the currency's minor unit; watch the already-minor `GBp`/`GBX`/`ILA` so you don't ×100 twice.
