# Drive layout

## Root

The vault is a single Google Drive folder. Its **folder ID** is per-user and lives under the `[drive]` section of `.secrets/findash` (gitignored, chmod 600):

```
[drive]
root_folder_id=<paste your vault folder ID here>
```

Get the ID from your Drive folder's URL: `drive.google.com/drive/folders/<ID>`. All skill Drive access goes through `scripts/drive.sh` (`ls` / `pull` / `push` / `move` / `delete`), which reads the ID from `.secrets/findash` and passes it to rclone via `--drive-root-folder-id=<ID>`. **Do not hardcode it anywhere else.**

Suggested human-readable folder name: `finance-vault`. The folder ID is the stable identifier; the name can change. The rclone remote should be named `gdrive`, because `scripts/drive.sh` uses paths like `gdrive:dump/`.

## Folder structure

```
<DRIVE_ROOT>/
├── dump/                           # drop new files here unsorted; sync triages on next run
├── payslips/<YYYY>/                # PDF, password-protected
├── investments/<YYYY>/             # XLSX exports + JPG screenshots from brokerage and bank capital-market view
├── long-term-savings/<YYYY>/       # PDF statements from Harel (pension + training fund) + JPG savings screenshots
├── full-statements/<YYYY-MM>/      # bank Hapoalim checking statements (XLSX/PDF) + Mastercard XLSX
├── fx-conversions/<YYYY>/          # Hapoalim FX conversion screenshots (ILS ⇄ USD between own sub-accounts)
├── finance.db                      # SQLite snapshot, overwritten by the weekly backup
└── backups/                        # finance-<timestamp>.db weekly backups (pruned to the newest ~8 by sync)
```

Files at the top of categories (not inside `<YYYY>/`) are also valid — the sync scans recursively.

### `dump/` — inbox for unsorted files (human drops only)

Drop any new finance document (payslip, statement, screenshot, anything) into `dump/` without thinking about where it belongs. Only humans write here — the automatic bank fetch stages its files locally, never in Drive. The sync skill triages each file on its next run: it downloads it to local staging, reads the content, decides the right destination folder and a descriptive name (recorded in `documents.drive_path`), ingests it, and then moves it via `rclone moveto`. Drive preserves the file's `drive_id` across the move, so the document audit trail stays intact. The move is **best-effort**: if Drive drops mid-run, an already-ingested file simply lingers in `dump/` (harmless — `drive_id` dedup stops re-ingestion) and the move retries on the next run.

If the sync encounters a file in `dump/` whose content doesn't fit any existing doc type, it creates a new doc type:

1. Proposes a snake_case name (e.g. `insurance_statement`, `tax_certificate`), a destination folder under the vault, and a filename pattern.
2. Documents the new type in [`docs/doc-types/`](./doc-types/README.md) (a routing-table row + a prose section on the matching catalogue page).
3. Moves the file. Mentions the new type in the sync report so you can rename or correct it.
4. **Re-checks already-classified files** against the updated catalogue and moves any that fit the new folder better (keeps the vault organized as the doc-type taxonomy grows).

## Filename convention

You've been using:

```
YYYY-MM-DD-<institution>-<doc-type>-<balance-or-amount>.<ext>
```

Examples:

- `2026-04-27-bank-hapoalim-<acct-suffix>-<balance>.xlsx` — Hapoalim checking account, with the account suffix and balance encoded for human triage
- `2026-04-26-harel-pension-pension-statement.pdf` — Harel pension statement
- `2026-05-06-stock-trade-confirmation-<amount>.jpg` — single trade confirmation

The underscore in numbers (`<major>_<minor>`) is the decimal point.

**The filename is a hint, not the source of truth.** The sync skill confirms by reading content. The amount in the filename is useful as a sanity check after parsing.

Payslips have a different convention:

```
<month-name>-<YY>-payroll.pdf   e.g. apr-26-payroll.pdf
```

## Sync semantics

- Files in `dump/` **are** moved and renamed by the sync skill — that's the whole point of the folder. Once a file is outside `dump/`, the sync only re-files it when a new doc type/folder is introduced (see above). `documents.drive_path` records the *intended* destination at ingest time, so it may briefly lead the actual move when Drive is down.
- Dedup happens on `documents.drive_id` (stable across moves), so renaming or moving a file later doesn't cause a re-ingest.
- `finance.db` at the root is overwritten by the **weekly** backup (sync skips the upload when the last successful backup is under 7 days old). Old copies live in `backups/`. Day-to-day, the local `data/finance.db` is the source of truth.
- If you re-organize folders in Drive manually, that's fine — `drive_id` is stable across moves; the next sync's stale-path check will update `documents.drive_path`.
- Every flow-driven Drive operation (listing, download, move, backup) is best-effort: a Drive outage becomes a `⚠️` warning in the Telegram summary, never a failed run.
