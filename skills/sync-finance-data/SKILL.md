---
name: sync-finance-data
description: Use when the user says "sync finance", "sync my finance", "ingest new docs", or any morning-update equivalent. Triages files dropped in `dump/`, scans the Google Drive vault for new documents, parses each into structured rows in the SQLite database, and uploads the updated database back to Drive.
---

# sync-finance-data

You ingest new documents from the user's Google Drive finance vault into the local SQLite database and back up the database to Drive. **You use judgment, not pattern-matching.** Categorization, deduplication, and reconciliation across docs are your job — there are no rules in this file beyond the flow.

## Where things live

- Drive folder ID: the `[drive]` section of `.secrets/findash` (key `root_folder_id=…`, chmod 600). Folder structure: [`docs/drive-layout.md`](../../docs/drive-layout.md).
- SQLite schema + conventions: [`docs/sqlite-schema.md`](../../docs/sqlite-schema.md)
- How each doc type maps to tables + the folder-routing table: [`docs/doc-types/`](../../docs/doc-types/README.md)
- Password for protected payslip PDFs: the `[pdf-passwords]` section of `.secrets/findash` (one `pattern=password` line per file pattern)
- rclone config: `./rclone.conf` (always pass `--config ./rclone.conf` to rclone)
- Local DB: `data/finance.db`
- Work dir for transient downloads: `inbox/` (clean it up after each run)

## Flow

### 1. Triage `dump/`

Before scanning the rest of the vault, sort anything the user dropped in `<DRIVE_ROOT>/dump/`:

- `rclone --config ./rclone.conf lsjson --drive-root-folder-id=<ROOT_ID> gdrive:dump/`
- For each entry: download to `inbox/dump/<filename>`, inspect contents to determine doc_type (using your judgment + the catalogue in [`docs/doc-types/`](../../docs/doc-types/README.md)).
- Match doc_type against the "Folder routing" table. Choose a destination folder + new filename per the convention.
- Move on Drive: `rclone --config ./rclone.conf moveto gdrive:dump/<orig> gdrive:<dest-folder>/<new-name>`. Drive preserves the file's `drive_id`, so any existing `documents` row stays valid.
- If a file's `drive_id` already exists in `documents`, update `documents.drive_path` to the new path.
- Delete the local `inbox/dump/<filename>` after the move succeeds.

**Unfamiliar files create a new doc_type:**

1. Propose a snake_case name (e.g. `insurance_statement`, `tax_certificate`), a destination folder under the vault, and a filename pattern.
2. Append a row to the "Folder routing" table in [`docs/doc-types/README.md`](../../docs/doc-types/README.md), and a short prose section on the matching catalogue page describing what the type holds, how to read it, and which SQLite tables it populates.
3. Move + rename the file per the new convention.
4. Mention every newly-created doc_type in the sync report: `Created doc_type 'X' → folder Y. See docs/doc-types/.`

**After creating a new folder, re-check existing files.** A new doc_type means the catalogue grew; some already-classified files may now belong in the new folder.

- Re-list the whole vault (`rclone lsjson --recursive`).
- For each file outside `dump/`, re-evaluate: does it fit the new doc_type better than its current folder?
- For each match: `rclone moveto` to the new path; `UPDATE documents SET drive_path=? WHERE drive_id=?` for the affected row(s); list every move in the sync report under "Reorganized".
- Skip this step entirely if no new doc_type was introduced this run — keeps normal syncs fast.

### 2. Enumerate the rest of the vault

For each top folder in `<DRIVE_ROOT>` (`payslips`, `investments`, `long-term-savings`, `full-statements`, and any folders introduced by `dump/` triage), recursively list files with `rclone --config ./rclone.conf lsjson --recursive --drive-root-folder-id=<ROOT_ID> gdrive:<folder>/`. Collect `(ID, Path, ModTime, Size)`.

### 3. Dedup

Query `documents.drive_id`. Skip anything already present.

### 4. Process each new file

- Download to `inbox/` with `rclone copy --config ./rclone.conf --drive-root-folder-id=<ROOT_ID> gdrive:<path> ./inbox/`.
- Classify by folder + filename (first pass), confirm by content (second pass). See [`docs/doc-types/`](../../docs/doc-types/README.md) for shapes.
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

### 5. Refresh price + FX cache

Run `python3 scripts/refresh_prices.py --range 1mo`. This calls Yahoo for the last month of daily closes for every currently-held security (plus SPY, USD/ILS, GBP/ILS) and writes any missing rows. It's idempotent — already-present rows are skipped via `INSERT OR IGNORE` (prices) or a `WHERE source != 'document'` guard (FX), so doc-derived rates from step 4 are preserved. Any new ticker with zero prices in the DB is automatically promoted to a 3y fetch.

Partial failures (a ticker 429s twice) are logged and recovered by the next run — don't treat them as blocking.

### 5b. Refresh dividend transactions (autonomous, every run)

Dividends are **real cash** — they bump your brokerage's per-currency cash buckets. Sync owns this regardless of whether a brokerage periodic statement happens to be in the vault this run. The goal: between snapshots, `transactions` should reflect every dividend that has actually been paid into the brokerage account.

For each currently-held security (the same set computed in step 5), run:

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

**Upsert when a brokerage periodic statement is processed** (see step 4 + `docs/doc-types/investments.md` "brokerage periodic statement"): for each real `הפ/דיב` row extracted, delete any matching synthetic transaction first, then insert the real one with `source_doc_id` = the statement's doc id. Match by `(account_id=<brokerage_account_id>, counterparty=<ticker>, ABS(julianday(date) - julianday(<row_date>)) <= 5)` filtered to synthetic docs:

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

Reflect what was inserted in the per-file summary (step 8) under **Ingested**: `live dividends — 3 new (JEPI +$24.18, SCHD +$12.40, RR.LSE +£2.25)`.

### 6. Backup database

- Overwrite `<DRIVE_ROOT>/finance.db` with the current `data/finance.db`.
- Drop a timestamped copy at `<DRIVE_ROOT>/backups/finance-<YYYY-MM-DD-HHMM>.db`.

### 7. Clean up

Empty `inbox/` (and `inbox/dump/`).

### 8. Summarize

Two outputs: a per-file summary file picked up by the dashboard render, and a stdout report for the current conversation.

**Per-file summary file** — append bullets to `data/last_sync_summary.md`. This file is read (and deleted) by `render-finance-dashboard` to send a second Telegram message after the dashboard HTML — see [`../render-finance-dashboard/SKILL.md`](../render-finance-dashboard/SKILL.md).

- Two top-level sections: `## Ingested` (files that produced ≥1 row in any fact table) and `## Triaged` (files moved without ingest, new doc_types created, files reorganized into newly-introduced folders).
- If the file already exists (the user ran sync twice before rendering), append under the existing section headers — don't duplicate headers. Create the file with both headers on first write.
- Omit a section header entirely when it has no bullets this run.
- If neither section has any bullets (a no-op resync), don't touch the file at all.
- A file that was both moved from `dump/` AND produced data rows goes under **Ingested** only — no double-counting.

Each bullet is one short sentence in your voice — what was extracted, with the key numbers. Examples by doc type:

```markdown
## Ingested

- added MSFT buy at <price> (5 shares, <fee> fee)
- sold RR.LSE for <price> (3 shares; net of 25% tax + <fee> fee)
- Hapoalim <acct> Feb statement: 18 txns, closing balance <amount> ILS
- April payslip (Acme): <amount> ILS net
- Harel pension Mar statement: 3 monthly deposits, total balance <amount> ILS

## Triaged

- moved screenshot.png → investments/<brokerage>/<YYYY-MM>-<action>-<ticker>.png
- created doc_type 'insurance_statement' → folder insurance/; 1 file routed
- reorganized: harel-pension-old.pdf → long-term-savings/harel-pension/2024-03.pdf
```

**Stdout report** — print:

```
Triaged dump/: <N> files moved
  - <orig> → <new-path>
  - …
Created new doc_types: <list, or "none">
Reorganized (existing files re-filed to new folders): <list, or "none">
Ingested: <M> new docs
  - X payslips, Y trades, Z balances, W transactions
Backups: finance.db + finance-<timestamp>.db uploaded
```

## Principles to apply throughout

- **Transfers between the user's own accounts are not expenses.** Hapoalim → your brokerage = `transfer`, not `expense`.
- **Filename amounts are sanity checks, not truth.** Always trust the content.
- **Pension/study-fund statements produce multi-component balance rows.** See [`docs/sqlite-schema.md`](../../docs/sqlite-schema.md) for the `component` column.
- **OCR uncertainty: refuse over fabricate.** If a JPG number is unreadable, ask the user rather than guess.
- **Idempotency:** running the skill twice should be a no-op (dedup via `documents.drive_id`; `dump/` is empty after the first triage).
- **Atomicity:** if extraction fails halfway through a file, don't leave half its rows in the DB — wrap each file's inserts in a transaction. Insert into `documents` only after the file's rows commit successfully.
- **A wrong doc_type name is recoverable; a wrong DB row is not.** Err toward creating a new doc_type when in doubt about routing; err toward asking the user when in doubt about parsing.

## When in doubt

Ask the user. A wrong row is worse than a missing one.
