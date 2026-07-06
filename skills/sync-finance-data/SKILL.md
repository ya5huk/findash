---
name: sync-finance-data
description: Use when the user says "sync finance", "sync my finance", "ingest new docs", or any morning-update equivalent. Gathers new Drive drops into local staging (best-effort), ingests everything staged — bank fetch pairs + manual drops — into the SQLite database, sorts `dump/` drops into their archetype folders on Drive, and backs the database up to Drive weekly.
---

# sync-finance-data

You ingest new financial data into the local SQLite database: files staged locally by `fetch-bank-data` plus documents the user dropped in the Google Drive vault. **You use judgment, not pattern-matching.** Categorization, deduplication, and reconciliation across docs are your job — there are no rules in this file beyond the flow.

**Drive is best-effort throughout; local ingest is the reliable core.** Any Drive failure (listing, download, move, backup) becomes a `⚠️` bullet in the summary — never an abort. The run only fails if local ingestion itself crashes.

## Where things live

- Drive folder ID: the `[drive]` section of `.secrets/findash` (key `root_folder_id=…`, chmod 600). Folder structure: [`docs/drive-layout.md`](../../docs/drive-layout.md).
- SQLite schema + conventions: [`docs/sqlite-schema.md`](../../docs/sqlite-schema.md)
- How each archetype maps to tables: [`docs/doc-types/`](../../docs/doc-types/README.md)
- Password for protected payslip PDFs: the `[pdf-passwords]` section of `.secrets/findash` (one `pattern=password` line per file pattern)
- Drive access: `scripts/drive.sh` (`ls [-R]` / `pull` / `push` / `move` / `delete`) — it owns `./rclone.conf` and the root-ID from `.secrets/findash`, fast-fails on network trouble, and any non-zero exit means "Drive degraded" (warn + continue)
- Local DB: `data/finance.db`
- Staging — the ingest queue: `inbox/staging/fetched/` (pairs staged by `fetch-bank-data`) and `inbox/staging/drive/` (Drive pulls from step 1, filenames prefixed `<driveId>__` so identity survives a crashed session)

## Flow

### 1. Gather new Drive drops into staging (best-effort)

Pull anything new from Drive into local staging. Every Drive call here is best-effort: on failure, add a `⚠️` bullet (step 8) and continue — never abort.

- List the inbox: `scripts/drive.sh ls dump/`
- List the vault: for each top folder in `<DRIVE_ROOT>` (the five archetype folders — `payslips`, `investments`, `long-term-savings`, `full-statements`, `fx-conversions` — plus any new-domain folder), `scripts/drive.sh ls -R <folder>/`. Collect `(ID, Path, ModTime, Size)`.
- **If listing fails** (auth lapse, network down): one warning bullet — `⚠️ Drive unreachable — manual drops skipped this run` — then go straight to step 2 and ingest whatever is already staged.
- **Dedup before download:** query `documents.drive_id`; skip anything already present. Exception: a `dump/`-listed file whose `drive_id` is already in `documents` was ingested on a previous run but its Drive-side move failed — put it on the **deferred-move list** for step 5 (its destination is stored in `documents.drive_path`); don't re-download.
- Download each genuinely new file: `scripts/drive.sh pull <path> inbox/staging/drive/<ID>__<filename>` — the `<ID>__` prefix keeps the drive_id attached to the file even if this session dies before ingest. Sanity-check the downloaded size against the listing. A per-file failure → warning bullet, skip that file, keep going.

### 2. Ingest everything in staging (local, reliable)

The core of the skill — no Drive dependency. Enumerate **both** staging dirs, `inbox/staging/fetched/` and `inbox/staging/drive/`, including files left over from a previous crashed run.

Per file, first the **already-ingested check**: derive its document id — for Drive files, the `<ID>__` filename prefix; for fetched pairs, `local:fetch:<json-filename>:<sha256-first-12>` recomputed from the `.json` bytes. If that id is already in `documents`, delete the staged file (a previous run committed it but died before cleanup) and move on.

Otherwise process it:

- Recognize the archetype from its origin folder (Drive files) or its `*-api-fetch*` naming (fetched pairs), then confirm by reading the content. See [`docs/doc-types/`](../../docs/doc-types/README.md) for the shapes you'll see.
- **Dump-sourced files:** judge which archetype folder the file belongs to and give it a descriptive destination name (no required format). Record the decision in the document row: `drive_path = <dest-folder>/<new-name>` — step 5 executes the actual Drive-side move from that value. Set `documents.doc_type` to a short free-text label (e.g. `harel-pension-statement`) — a human-readable note, not a value from a closed set.

  **Unfamiliar files:** map to the nearest archetype by reading the content — most things fit one of the five folders. If a file genuinely belongs to a new financial domain (e.g. insurance), use judgment: give it a folder, add a short prose note to the matching catalogue page (what it holds, how to read it, which tables it feeds), and mention it in the sync report. There's **no routing table to grow and no vault re-scan** — the archetypes are stable.
- **Fetched pairs** (`*-api-fetch*.json` + `.notes.md`): read both halves together — the notes are hints; verify against the JSON. Create **one** documents row for the pair: `drive_id = 'local:fetch:<json-filename>:<sha256-first-12>'`, `drive_path = 'local/staging/fetched'`, `filename` = the json filename, `raw_hash` = the full sha256, and fold the sidecar's bullets into `documents.notes` — the reasoning survives after the files are deleted. The hash in the id is deliberate: a same-day re-fetch with identical bytes dedups to a no-op, while changed bytes re-ingest (content-key judgment absorbs the overlapping txns).
- Extract the data. The *how* depends on format:
  - PDF (unlocked) → use the Read tool with the local file path.
  - PDF (password-protected payslip) → `qpdf --password=<pw> --decrypt <file> <tmp>`, read `tmp`, delete `tmp`. If `qpdf` is missing tell the user to install it (`sudo apt install -y qpdf`).
  - XLSX → `python3 scripts/xlsx_to_rows.py <file>` returns JSON `{sheet, rows}` with Excel serial dates already converted to ISO.
  - JPG → use the Read tool with the image path; transcribe what you see.
- Insert rows into the right tables (see [`docs/sqlite-schema.md`](../../docs/sqlite-schema.md) for column conventions). Always include `source_doc_id` linking back to `documents`.
- **Use judgment** when categorizing transactions and matching cross-document events. Read [`docs/doc-types/classification.md`](../../docs/doc-types/classification.md) "Judgment calls" before doing any classification work.
- **Closing balance:** for bank statements, read the running-balance column on the last row (Hapoalim XLSX → יתרה בש"ח). Do NOT sum the in-window transactions and call that the balance — that only works if the statement covers the account's entire lifetime. If no running-balance is visible, skip the `balances` insert and add a note to `documents.notes`.
- **Brokerage balance screenshots:** when a brokerage balance screenshot shows cash holdings per currency, insert one `balances` row per non-zero currency with `component='cash_usd' | 'cash_gbp' | 'cash_ils'`. See [`docs/doc-types/investments.md`](../../docs/doc-types/investments.md) "brokerage balance screenshot" for the field details. The renderer combines these snapshots with subsequent flows so cash stays accurate between uploads.
- **Explicit FX rates in docs are authoritative.** When a doc shows the rate the user actually transacted at (e.g. a Hapoalim USD-purchase line "1,000 USD @ 3.30 ILS" on a day Yahoo closed 3.28), insert it into `fx_rates` with `source='document'`. Use `INSERT INTO fx_rates (...) VALUES (..., 'document') ON CONFLICT (date, base_currency, quote_currency) DO UPDATE SET rate=excluded.rate, source='document'` — docs always win over the Yahoo refresh. If the doc shows a converted ILS amount without naming the rate, derive it (`ils_amount / fx_amount`) and write it the same way.
- **Commit, then delete.** Wrap each file's inserts in one transaction; insert into `documents` only after the fact rows commit. Once the commit succeeds, delete the staged file (both halves of a fetched pair). If extraction fails, leave the file in staging — it's the retry queue for the next run — and add a warning bullet.

### 3. Refresh price + FX cache

Run `python3 scripts/refresh_prices.py --range 1mo`. This calls Yahoo for the last month of daily closes for every currently-held security (plus SPY, USD/ILS, GBP/ILS) and writes any missing rows. It's idempotent — already-present rows are skipped via `INSERT OR IGNORE` (prices) or a `WHERE source != 'document'` guard (FX), so doc-derived rates from step 2 are preserved. Any new ticker with zero prices in the DB is automatically promoted to a 3y fetch.

Partial failures (a ticker 429s twice) are logged and recovered by the next run — don't treat them as blocking.

### 4. Refresh dividend transactions (autonomous, every run)

Dividends are **real cash** — they bump your brokerage's per-currency cash buckets. Sync owns this regardless of whether a brokerage periodic statement happens to be in the vault this run. The goal: between snapshots, `transactions` should reflect every dividend that has actually been paid into the brokerage account.

For each currently-held security (the same set computed in step 3), run:

```bash
python3 scripts/yahoo_dividends.py <ticker>
```

It returns JSON `{ticker, yahoo_symbol, yahoo_currency, past:[{pay_date, amount_per_share}, …], next:{pay_date, amount_per_share}|null}`. Past covers ~5 years; `next` is a cadence-extrapolated projection (used by the renderer for the "Upcoming" card — sync does not insert it).

For each past event, decide whether to insert a synthetic transaction:

1. **Compute shares held on the pay_date**:
   ```sql
   SELECT COALESCE(SUM(CASE WHEN side='buy' THEN shares ELSE -shares END), 0)
   FROM trades WHERE security_id = ? AND date <= ?
   ```
   If 0, skip — you didn't own it on the ex-date.

2. **Find the security's last cash snapshot date** on the brokerage account for its currency (USD/GBP/ILS). Resolve `<brokerage_account_id>` from `accounts` before running the query (the brokerage account that has trades):
   ```sql
   SELECT MAX(as_of) FROM balances
   WHERE account_id = <brokerage_account_id> AND component = ?  -- 'cash_usd' | 'cash_gbp' | 'cash_ils'
   ```
   Skip the event if `pay_date <= last_snapshot_date` — the snapshot's cash component already includes it; a synthetic transaction would double-count.

3. **Normalize amount to the security's major unit.** If `yahoo_currency` is `GBp` (LSE listings report in pence), divide `amount_per_share` by 100 before multiplying. For USD/GBP/ILS yahoo_currency, use as-is.

4. **Compute net amount** = `shares_held × amount_per_share_major × 0.75` (25% Israeli dividend withholding). Round to 2 decimals; multiply by 100 for `amount_minor`.

5. **Insert the synthetic document + transaction** (both idempotent):
   ```sql
   INSERT OR IGNORE INTO documents
     (drive_id, drive_path, filename, doc_type, doc_date, notes)
   VALUES
     ('yahoo:div:<ticker>:<pay_date>',
      'synthetic/yahoo/dividends',
      'yahoo-div-<ticker>-<pay_date>',
      'yahoo_dividend_estimate',
      '<pay_date>',
      'Live Yahoo dividend estimate (net of 25% Israeli withholding). Will be superseded if a brokerage periodic statement covering this date is later ingested.');

   -- Look up the doc id (whether just inserted or pre-existing):
   SELECT id FROM documents WHERE drive_id = 'yahoo:div:<ticker>:<pay_date>';

   -- Resolve <brokerage_account_id> from the accounts table first (the brokerage
   -- account that has trades). Only insert the transaction if one doesn't already
   -- reference this doc:
   INSERT INTO transactions
     (account_id, date, amount_minor, currency, category, counterparty,
      description, source_doc_id)
   SELECT <brokerage_account_id>, '<pay_date>', <net_minor>, '<security_ccy>', 'dividend', '<ticker>',
          '<shares> × <per_share> × 0.75 net (Yahoo estimate)', <doc_id>
   WHERE NOT EXISTS (
     SELECT 1 FROM transactions WHERE source_doc_id = <doc_id>
   );
   ```

The `INSERT OR IGNORE` on documents (uniqueness via `drive_id`) plus the `WHERE NOT EXISTS` on transactions makes the whole step a no-op on re-runs.

**Upsert when a brokerage periodic statement is processed** (see step 2 + `docs/doc-types/investments.md` "brokerage periodic statement"): for each real `הפ/דיב` row extracted, delete any matching synthetic transaction first, then insert the real one with `source_doc_id` = the statement's doc id. Match by `(account_id=<brokerage_account_id>, counterparty=<ticker>, ABS(julianday(date) - julianday(<row_date>)) <= 5)` filtered to synthetic docs:

```sql
DELETE FROM transactions
WHERE id IN (
  SELECT t.id FROM transactions t
  JOIN documents d ON d.id = t.source_doc_id
  WHERE t.account_id = <brokerage_account_id>
    AND t.category = 'dividend'
    AND t.counterparty = '<ticker>'
    AND ABS(julianday(t.date) - julianday('<row_date>')) <= 5
    AND d.doc_type = 'yahoo_dividend_estimate'
);
```

**Snapshot supersession.** After inserting any new brokerage balance snapshot at date `D` for any currency component (`cash_usd|cash_gbp|cash_ils`), drop synthetic dividend transactions on the brokerage account dated `<= D` — the snapshot's cash component now reflects them, keeping the synthetics would double-count:

```sql
DELETE FROM transactions
WHERE id IN (
  SELECT t.id FROM transactions t
  JOIN documents d ON d.id = t.source_doc_id
  WHERE t.account_id = <brokerage_account_id>
    AND t.category = 'dividend'
    AND t.date <= '<D>'
    AND d.doc_type = 'yahoo_dividend_estimate'
);
```

Reflect what was inserted in the per-file summary (step 8) under **Ingested**: `💰 Dividends: 3 new (JEPI, SCHD, RR.LSE)`.

### 5. Sort `dump/` drops on Drive (best-effort)

For every dump-sourced file ingested in step 2, plus every entry on the deferred-move list from step 1:

```bash
scripts/drive.sh move dump/<orig> <documents.drive_path>
```

Drive preserves the file's `drive_id` across the move, so the documents row stays valid. A failed move → warning bullet (`⚠️ dump sort deferred — <file> stays in dump/`); the lingering file is harmless (drive_id dedup stops re-ingestion) and the move retries automatically next run.

### 6. Weekly database backup (best-effort, gated)

The DB goes to Drive at most once a week; day-to-day, local `data/finance.db` is the source of truth.

- Schema guard for DBs created before the `meta` table existed:
  ```sql
  CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
  ```
- **Gate:** skip when the last successful backup is fresh —
  ```sql
  SELECT COUNT(*) FROM meta WHERE key='last_db_backup_at' AND value > datetime('now','-7 days');
  ```
  `1` → skip (silent in Telegram; one stdout line). `0` or no row → due (a fresh DB backs up on its first run). If the user explicitly asked to back up now, ignore the gate.
- When due:
  ```bash
  scripts/drive.sh push data/finance.db finance.db
  scripts/drive.sh push data/finance.db backups/finance-<YYYY-MM-DD-HHMM>.db
  ```
- Only after **both** copies succeed:
  ```sql
  INSERT INTO meta(key,value) VALUES('last_db_backup_at', datetime('now'))
  ON CONFLICT(key) DO UPDATE SET value=excluded.value;
  ```
- Failure → warning bullet `⚠️ weekly DB backup failed — retrying next run`; `meta` stays untouched, so the next run tries again.
- **Prune old backups** (only after a successful backup): `scripts/drive.sh ls backups/`, keep the newest 8 `finance-*.db` by ModTime, `scripts/drive.sh delete backups/<name>` for the rest. Housekeeping-grade best-effort: a prune failure is one stdout line, not a Telegram warning.

### 7. Clean up + run journal

Successfully ingested staged files were already deleted per-file in step 2. Remove any other transient artifacts under `inbox/` (including a legacy `inbox/dump/` if one exists) — but **never** delete staging files whose ingest failed; they are the retry queue.

Then stamp the run journal in `meta` (same upsert shape as step 6; the doctor reads these for staleness warnings):

- `last_sync_at = datetime('now')` — every run.
- `last_ingest_<company>_at` — for each company whose fetched pair ingested this run (e.g. `last_ingest_hapoalim_at`, `last_ingest_cal_at`).

### 8. Summarize

Two outputs: a per-file summary file picked up by the dashboard render, and a stdout report for the current conversation.

**Per-file summary file** — append bullets to `data/last_sync_summary.md`. This file is read (and deleted) by `render-finance-dashboard` to send a second Telegram message after the dashboard HTML — see [`../render-finance-dashboard/SKILL.md`](../render-finance-dashboard/SKILL.md).

- Three top-level sections, **in this order**: `## ⚠️ Warnings` (best-effort failures from any step), `## Ingested` (files that produced ≥1 row in any fact table), `## Triaged` (files filed into their archetype folder without producing fact rows).
- If the file already exists (the user ran sync twice before rendering), append under the existing section headers — don't duplicate headers, and don't repeat a warning bullet that's already there. Warnings always go at the top: prepend the section if it's missing.
- Omit a section header entirely when it has no bullets this run.
- If no section has any bullets (a clean no-op resync), don't touch the file at all. Warnings alone DO justify writing the file.
- A file that was both filed AND produced data rows goes under **Ingested** only — no double-counting.

**Bullet style — scannable, not prose.** One emoji prefix + max ~10 words per bullet. Key numbers welcome (this file goes to the user's private Telegram), full reconciliation reasoning does not — that lives in `documents.notes` and the DB. Suggested emoji: 🏦 bank · 💳 card · 📈 IBKR · 💰 dividends · 📄 payslip · 🧾 statement · 📁 filed · 💾 backup · ⚠️ warning.

```markdown
## ⚠️ Warnings

- ⚠️ Drive unreachable — manual drops skipped this run
- ⚠️ weekly DB backup failed — retrying next run

## Ingested

- 🏦 Hapoalim <acct>: 18 txns, closing balance updated
- 💳 Cal <acct>: 9 new txns, next bill −₪1,524
- 📈 added MSFT buy (5 shares)
- 📄 April payslip (Acme): net + pension rows added
- 🧾 Harel pension Mar: 3 deposits, balance updated

## Triaged

- 📁 screenshot.png → investments/ (brokerage sell snapshot)
- 📁 insurance-renewal.pdf → new insurance/ folder
```

**Stdout report** — print (counts only, no amounts):

```
Gather: <N> new Drive files staged (or: Drive unreachable — skipped)
Ingest: <M> staged files → DB (<K> failed, left in staging)
Sort: <J> dump/ files filed (<D> deferred)
Backup: done | skipped (fresh) | failed
Warnings: <W>
```

## Principles to apply throughout

- **Transfers between the user's own accounts are not expenses.** Hapoalim → your brokerage = `transfer`, not `expense`.
- **Filename amounts are sanity checks, not truth.** Always trust the content.
- **Pension/study-fund statements produce multi-component balance rows.** See [`docs/sqlite-schema.md`](../../docs/sqlite-schema.md) for the `component` column.
- **OCR uncertainty: refuse over fabricate.** If a JPG number is unreadable, ask the user rather than guess.
- **Drive degrades, never aborts.** Every Drive touch — gather, sort, backup — is best-effort: failure = one `⚠️` bullet + continue. Only a local ingest crash fails the run.
- **Idempotency:** running the skill twice should be a no-op. Dedup via `documents.drive_id` *before* download for Drive files; via the synthetic `local:fetch:<filename>:<hash>` id for fetched pairs. Staging is re-entrant: leftovers are either ingested or recognized as already-committed and deleted.
- **Atomicity:** if extraction fails halfway through a file, don't leave half its rows in the DB — wrap each file's inserts in a transaction. Insert into `documents` only after the file's rows commit successfully. Delete a staged file only after its commit.
- **A misfiled doc is recoverable; a wrong DB row is not.** Err toward routing to the nearest archetype when in doubt about placement; err toward asking the user when in doubt about parsing.

## When in doubt

Ask the user. A wrong row is worse than a missing one.
