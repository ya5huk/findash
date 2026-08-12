# IBKR account auto-resolution — design

**Date:** 2026-07-02
**Status:** historical; superseded — do not implement

> This document describes an abandoned single-`accounts.live_source` design.
> The current architecture models one logical account with multiple
> `account_feeds`, preserves raw observations as evidence for canonical events,
> and applies versioned migrations through `scripts/migrate_db.py`. See
> `docs/live-sources.md`, `docs/sqlite-schema.md`, and
> `skills/fetch-investments/SKILL.md` for the maintained design.

## Problem

IBKR data must land on exactly one `accounts` row (schema requirement), but today the choice
lives in `.secrets/findash` `[ibkr] account_name` — a file skills may never write. So:

- `setup` makes the user hand-edit a chmod-600 credentials file for a value that isn't a secret.
- If unset, `fetch-investments` re-asks **every run** and the answer never persists.
- An unanswered prompt forces the agent to guess, and the guess evaporates with the session.
- The value references a DB row by name from outside the DB — renaming the account silently
  dangles it.

Goal: zero manual config. An IBKR-only user should never see a mapping question; a
wrapper-broker user (their tracked broker *is* IBKR underneath) should get the attach decision
made by evidence; and the decision must be sticky, because the trade dedup key
`(account_id, external_id)` breaks — double-counting the portfolio — if the resolved account
flips between runs (first-principle #7, idempotency).

## Design

### 1. Schema: `accounts.live_source TEXT` (nullable)

`'ibkr'` marks the one account the IBKR live source feeds. Added to `scripts/init-db.sql`;
existing DBs get it via an idempotent guard in `fetch-investments` (same
`pragma_table_info` pattern as `trades.external_id`). Generalizes to future live sources.

### 2. Resolution order in `fetch-investments` (replaces today's §1 mapping step)

1. **Marker:** `SELECT id FROM accounts WHERE live_source='ibkr'` → use it. Steady state; no
   prompt. Two marked rows, or a marked row with `closed_on` set → stop with a clear message
   (doctor territory), never guess between them.
2. **Legacy override:** `[ibkr] account_name` set in `.secrets/findash` → resolve by name, use
   it, and **backfill the marker** so the secrets line becomes redundant. Existing installs
   converge automatically.
3. **First-run evidence resolution** (neither set):
   - Trades already carrying `external_id` on some account are conclusive — that account was
     connector-fed before; mark it and continue.
   - Otherwise compare the connector's trade history + positions against each existing
     brokerage account's ledger (tickers / quantities / dates). A clear match → attach (the
     wrapper case). This is judgment, not a script (first-principle #2).
   - No brokerage accounts, or no overlap anywhere → **auto-create** an account
     (`kind='brokerage'`, `institution` from the connector summary, base currency) — the
     IBKR-only user path, zero questions.
   - Genuinely ambiguous (partial overlap, multiple candidates) → ask once, interactively. If
     no answer arrives, **skip the run** (`IBKR: skipped (account ambiguous)`) rather than
     guess — a wrong sticky choice is worse than a missed day.
   - Persist whichever outcome: `UPDATE accounts SET live_source='ibkr' WHERE id=?`.

### 3. Per-run duplication guard (extends the existing reconcile step)

Every run, after ingest: if another account's holdings substantially overlap the IBKR
symbols/quantities (the same portfolio arriving twice — e.g. broker statements for the wrapper
account also being synced as documents), flag it in the run summary and `accounts.notes`.
Warn, never silently continue, never insert corrective rows.

### 4. Documentation updates (one topic per file)

- `skills/fetch-investments/SKILL.md` — §1 rewritten to the resolution order above.
- `docs/live-sources.md` — *Account mapping* section rewritten (marker is the source of truth;
  `account_name` is a legacy override).
- `skills/setup/SKILL.md` — IBKR step no longer instructs a hand-paste; it says to run
  `/findash:fetch-investments`, which resolves the account itself. `[ibkr]` stays in the
  secrets template only for the optional `account_ids` / `base_currency` keys, with
  `account_name` documented as optional legacy.
- `CLAUDE.md` — `[ibkr]` setup bullet updated to match.
- `skills/findash-doctor/SKILL.md` — one added check: at most one `live_source='ibkr'` row and
  it isn't closed; report ⚠️ otherwise.

## Not in scope

- Multi-IBKR-account support beyond the existing `account_ids` override.
- Other live sources (the column generalizes; nothing else changes now).
- Removing `account_name` support entirely — kept as a read-only override; owner may choose to
  drop it later.

## Verification

- Fresh DB + connector: run `fetch-investments` → account auto-created, marker set, second run
  resolves silently from the marker.
- DB with a matching ledger (wrapper case): first run attaches to the existing account by
  evidence, marker set; second run silent.
- `[ibkr] account_name` set, no marker: run resolves by name and backfills the marker.
- Marker on a closed account / two markers: run stops with the clear message; doctor flags it.
