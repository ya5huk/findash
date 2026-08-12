# IBKR Account Auto-Resolution Implementation Plan

> **HISTORICAL — DO NOT IMPLEMENT.** This plan was superseded by the normalized
> ledger architecture: logical accounts can have multiple `account_feeds`, raw
> observations link to canonical events through evidence tables, and schema
> changes run through `scripts/migrate_db.py`. Its single
> `accounts.live_source` marker and direct-SQL migration steps are retained only
> as design history. Use `docs/live-sources.md`, `docs/sqlite-schema.md`, and the
> current `skills/fetch-investments/SKILL.md` instead.

> Historical note: this was originally formatted as an executable task plan.
> Its unchecked boxes no longer describe work to perform.

**Goal:** Replace the hand-pasted `[ibkr] account_name` config with automatic, evidence-based account resolution persisted as a DB marker.

**Architecture:** One nullable `accounts.live_source` column marks the account the IBKR live source feeds. `fetch-investments` resolves it in priority order (marker → legacy secrets override → first-run evidence) and persists the outcome; docs and `setup` stop instructing a manual paste. This repo is a prompt-driven plugin — the "code" is mostly skill/doc markdown plus one SQL schema file, so verification is cross-file consistency and live queries, not unit tests.

**Tech Stack:** SQLite (`scripts/init-db.sql`), skill markdown under `skills/`, docs under `docs/`.

**Spec:** `docs/superpowers/specs/2026-07-02-ibkr-auto-mapping-design.md`

## Global Constraints

- Public-project hygiene: no real account names, ids, balances, or Drive IDs in any committed file.
- One topic per file: resolution order lives in `skills/fetch-investments/SKILL.md`; the mapping concept lives in `docs/live-sources.md`; other files only point at them.
- Instruct, don't hardcode: evidence matching is described as judgment, never as regex/SQL recipes.
- Commits are handled at the end of the whole plan by the user's `/commit-multiple-if-needed` skill — individual tasks do NOT commit.

---

### Task 1: Schema — `accounts.live_source`

**Files:**
- Modify: `scripts/init-db.sql:43` (accounts block)

**Interfaces:**
- Produces: `accounts.live_source TEXT` — `'ibkr'` on the (single) account the IBKR live source feeds; NULL otherwise. All later tasks reference this exact column name and value.

- [ ] **Step 1: Add the column to the accounts block**

In `scripts/init-db.sql`, change the last line of the `accounts` column list:

```sql
  liquidity_date    TEXT,                                  -- when funds become accessible (training fund, deposits)
  notes             TEXT,
  live_source       TEXT                                   -- 'ibkr' on the one account a live API source feeds (see docs/live-sources.md); NULL otherwise
);
```

- [ ] **Step 2: Verify the file parses**

Run: `sqlite3 :memory: ".read scripts/init-db.sql" "SELECT 1 FROM pragma_table_info('accounts') WHERE name='live_source';"`
Expected: `1`

---

### Task 2: `skills/fetch-investments/SKILL.md` — resolution order

**Files:**
- Modify: `skills/fetch-investments/SKILL.md` (§1 "Preconditions + resolve the mapped account", the `[ibkr]` config bullet in "Where things live", the reconcile step §4, and the Principles list)

**Interfaces:**
- Consumes: `accounts.live_source` from Task 1.
- Produces: the resolution-order prose that Tasks 3–6 point at.

- [ ] **Step 1: Replace the `account_name` config bullet in "Where things live"**

Replace the `**account_name**` sub-bullet under the `[ibkr]` config bullet with:

```markdown
  - `account_name` — **legacy override only.** The mapped account now lives in the DB
    (`accounts.live_source='ibkr'`); if this key is set it wins once and is backfilled into the
    marker (see §1). New installs never set it.
```

- [ ] **Step 2: Replace the "Resolve the mapped account" bullet in §1**

Replace the entire `**Resolve the mapped account** from [ibkr] account_name:` bullet (all four sub-bullets) with:

```markdown
- **Resolve the mapped account** — in this priority order, and persist the outcome so the choice
  is sticky (the `(account_id, external_id)` dedup key double-counts the portfolio if the
  account ever flips between runs):
  1. **Marker:** `SELECT id FROM accounts WHERE live_source='ibkr'`. One row → use it, no
     questions. Two rows, or a marked row with `closed_on` set → stop with a clear message
     (`findash-doctor` flags this too); never guess between them.
  2. **Legacy override:** `[ibkr] account_name` set → resolve by name (`SELECT id FROM accounts
     WHERE name=?`), use it, and backfill the marker (`UPDATE accounts SET live_source='ibkr'
     WHERE id=?`) so the secrets line becomes redundant. Name doesn't resolve → treat as unset
     and fall through.
  3. **First-run evidence (judgment, not a script):**
     - An account already holding trades with `external_id` set was connector-fed before —
       conclusive. Mark it and continue.
     - Otherwise compare the connector's trade history and positions against each existing
       brokerage account's ledger (tickers / quantities / dates). A clear match means that
       broker *is* the IBKR account underneath (the wrapper case) — attach to it.
     - No brokerage accounts, or no overlap anywhere → create one brokerage row
       (`kind='brokerage'`, `name='Interactive Brokers'`, `institution` from
       `get_account_summary` or `'Interactive Brokers'`, `currency=<base ccy>`). This is the
       IBKR-only-user path: zero questions.
     - Genuinely ambiguous (partial overlap, multiple plausible candidates) → ask once,
       interactively. If no answer arrives, skip the run (`IBKR: skipped (account ambiguous)`)
       rather than guess — a wrong sticky choice is worse than a missed day.
     - Whatever the outcome: `UPDATE accounts SET live_source='ibkr' WHERE id=?`.
  - Everything below writes to this **one** resolved account. Never create a *second* IBKR
    account.
```

- [ ] **Step 3: Add the `live_source` schema guard to the idempotent-guards bullet**

In the "Idempotent schema guards" bullet, after the `external_id` guard block, add:

```markdown
  - `accounts.live_source` — same `pragma_table_info` guard pattern:
    ```bash
    sqlite3 data/finance.db "SELECT 1 FROM pragma_table_info('accounts') WHERE name='live_source';" | grep -q 1 \
      || sqlite3 data/finance.db "ALTER TABLE accounts ADD COLUMN live_source TEXT;"
    ```
```

- [ ] **Step 4: Extend §4 reconcile with the duplication guard**

At the end of the §4 "Reconcile" bullet, add:

```markdown
  Also guard against **double-feeding**: if another account's holdings substantially overlap the
  IBKR symbols/quantities (the same portfolio arriving twice — e.g. the wrapper broker's
  statements are also being synced as documents), flag it in the summary and `accounts.notes`.
  Warn, never silently continue.
```

- [ ] **Step 5: Update the Principles bullet**

Replace the "Trade-fed, one mapped account." principle's first sentence with:

```markdown
- **Trade-fed, one resolved account.** The mapped account is resolved automatically (marker →
  legacy `account_name` → first-run evidence) and persisted as `accounts.live_source='ibkr'`;
  never create a second IBKR account (it double-counts net worth).
```

- [ ] **Step 6: Consistency check**

Run: `grep -n "asks once and prints" skills/fetch-investments/SKILL.md`
Expected: no matches (the print-the-paste-line flow is gone).

---

### Task 3: `docs/live-sources.md` — Account mapping section

**Files:**
- Modify: `docs/live-sources.md` ("Account mapping" section; one line in "The `source_doc_id = NULL` convention")

**Interfaces:**
- Consumes: resolution order from Task 2 (link to it, don't repeat it — one topic per file).

- [ ] **Step 1: Replace the "Account mapping" section body**

```markdown
### Account mapping

IBKR rarely deserves its own findash account: a broker you already track may itself be an IBKR
wrapper (some Israeli brokers are IBKR wrappers underneath), so a separate IBKR account would
double-count net worth. The mapped account is **resolved automatically** by
[`fetch-investments`](../skills/fetch-investments/SKILL.md) — DB marker
(`accounts.live_source='ibkr'`) first, then the legacy `[ibkr] account_name` secrets override
(backfilled into the marker once), then first-run evidence: attach to a ledger-matching broker,
auto-create an account for IBKR-only users, ask only when genuinely ambiguous. The decision is
persisted in the marker so it stays sticky — the `(account_id, external_id)` dedup key breaks if
the account flips between runs. No code hardcodes an account name or id, and nothing needs to be
hand-edited.
```

- [ ] **Step 2: Note the marker in the provenance bullet**

In "The `source_doc_id = NULL` convention" section, extend the "Provenance is implicit in the account" bullet's first sentence to:

```markdown
- **Provenance is implicit in the account.** Rows on the `live_source='ibkr'`-marked account are,
  by construction, from the IBKR live source. Keep a one-line note in `accounts.notes` when something is
  worth recording (e.g. a reconciliation discrepancy).
```

- [ ] **Step 3: Consistency check**

Run: `grep -n "account_name" docs/live-sources.md`
Expected: only the legacy-override mention inside the new Account mapping paragraph.

---

### Task 4: `skills/setup/SKILL.md` — drop the hand-paste step

**Files:**
- Modify: `skills/setup/SKILL.md` (secrets-template `[ibkr]` comment block ~lines 40–47; IBKR onboarding step 2 ~line 70; step 3 sentence about "asks once and prints the line")

- [ ] **Step 1: Update the `[ibkr]` comment block in the secrets template**

```ini
[ibkr]
# IBKR auth is the official Interactive Brokers connector you add in Claude (no
# password here). The account IBKR maps onto is resolved automatically by
# /findash:fetch-investments and stored in the DB — nothing to set here.
# account_name=<legacy override only — pre-existing installs>
# account_ids=<comma-separated IBKR account ids>
# base_currency=ILS
```

- [ ] **Step 2: Replace onboarding step 2 ("Map IBKR onto a findash account")**

```markdown
  2. **Account mapping is automatic.** `fetch-investments` resolves which findash account IBKR
     feeds (attaching to an existing broker when the ledgers clearly match — some brokers are
     IBKR wrappers underneath — creating one for IBKR-only users, asking only when genuinely
     ambiguous) and persists the choice in the DB. There is nothing to configure by hand.
```

- [ ] **Step 3: Fix step 3's trailing sentence**

Replace `If `account_name` is unset it asks once and prints the line to paste.` with:

```markdown
The account it maps onto is resolved automatically (see step 2).
```

- [ ] **Step 4: Consistency check**

Run: `grep -n "prints the line" skills/setup/SKILL.md`
Expected: no matches.

---

### Task 5: `CLAUDE.md` — `[ibkr]` bullet

**Files:**
- Modify: `CLAUDE.md` (the IBKR one-time-setup bullet, item 2)

- [ ] **Step 1: Replace item 2 of the IBKR setup bullet**

Replace the sentence starting `` `[ibkr]` section in `.secrets/findash`: `account_name=` records… `` with:

```markdown
  2. Account mapping is **automatic**: `fetch-investments` resolves which findash account IBKR feeds and stores it in the DB (`accounts.live_source='ibkr'`) — attach-by-evidence for wrapper brokers, auto-create for IBKR-only users. `[ibkr]` in `.secrets/findash` is only needed for the optional `account_ids=` / `base_currency=` keys (`account_name=` remains a legacy override).
```

---

### Task 6: `skills/findash-doctor/SKILL.md` — marker consistency check

**Files:**
- Modify: `skills/findash-doctor/SKILL.md` (checks table + the IBKR note paragraph)

- [ ] **Step 1: Extend check #11's note paragraph**

After the existing IBKR-connector sentence block, add:

```markdown
If `data/finance.db` exists and has the `live_source` column, also sanity-check the marker:
`SELECT COUNT(*) FROM accounts WHERE live_source='ibkr'` should be 0 or 1, and a marked account
must not have `closed_on` set. More than one marked row, or a closed marked row → ⚠️ with a
one-line explanation (fetch-investments refuses to run until it's resolved). Zero rows is fine —
it just means fetch-investments hasn't run yet.
```

---

### Task 7: Converge this install + end-to-end verification

**Files:** none committed (local DB only).

- [ ] **Step 1: Apply the idempotent guard to the local DB**

Run: `sqlite3 data/finance.db "ALTER TABLE accounts ADD COLUMN live_source TEXT;"`

- [ ] **Step 2: Backfill the marker by the conclusive-evidence rule**

The account holding `external_id` trades is the connector-fed one:

Run: `sqlite3 data/finance.db "UPDATE accounts SET live_source='ibkr' WHERE id = (SELECT DISTINCT account_id FROM trades WHERE external_id IS NOT NULL);"`

- [ ] **Step 3: Verify resolution now short-circuits**

Run: `sqlite3 data/finance.db "SELECT COUNT(*) FROM accounts WHERE live_source='ibkr' AND closed_on IS NULL;"`
Expected: `1`

- [ ] **Step 4: Cross-file consistency sweep**

Run: `grep -rn "account_name" CLAUDE.md skills/ docs/live-sources.md`
Expected: every remaining hit says "legacy" or "override" (or is the optional-keys template comment) — none instructs pasting it as the primary flow.
