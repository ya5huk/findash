# Drive layout

## Root

The vault is a single Google Drive folder. Its **folder ID** is per-user and lives under the `[drive]` section of `.secrets/findash` (gitignored, chmod 600):

```
[drive]
root_folder_id=<paste your vault folder ID here>
```

Get the ID from your Drive folder's URL: `drive.google.com/drive/folders/<ID>`. All skill Drive access goes through `scripts/drive.sh` (`probe` / `ensure-layout` / `ls` / `pull` / `push` / `move` / `delete`), which reads the ID from `.secrets/findash` and passes it to rclone via `--drive-root-folder-id=<ID>`. **Do not hardcode it anywhere else.**

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
├── other/<YYYY>/                   # unrecognized/private source types pending a reviewed catalogue addition
├── finance.db                      # SQLite snapshot, overwritten by the weekly backup
└── backups/                        # finance-<timestamp>.db weekly backups (pruned to the newest ~8 by sync)
```

Files at the top of categories (not inside `<YYYY>/`) are also valid — the sync scans recursively.

Run `scripts/drive.sh ensure-layout` once after connecting Drive; setup, doctor,
and sync also run it idempotently. It creates any missing fixed folder shown
above (except the database file) and does not list private filenames.

### `dump/` — inbox for unsorted files (human drops only)

Drop any new finance document (payslip, statement, screenshot, anything) into `dump/` without thinking about where it belongs. Only humans write here — the automatic bank fetch stages its files locally, never in Drive. The sync skill triages each file on its next run: it downloads it to local staging, reads the content, decides the right destination folder and a descriptive name (recorded in `documents.drive_path`), ingests it, and then moves that one direct `dump/` file through `scripts/drive.sh move`. Drive preserves the file's `drive_id` across the move, so the document audit trail stays intact. The wrapper never overwrites a destination: any name collision leaves both Drive files and identities untouched for review. The move is **best-effort**: if Drive drops mid-run, an already-ingested file simply lingers in `dump/` (harmless — `drive_id` dedup stops re-ingestion) and the move retries on the next run.

If a file does not fit the committed catalogue, sync records it as `other`,
files it under `other/<YYYY>/` with a privacy-safe name, and notes locally that
the type needs review. A normal finance run never edits committed documentation
from private input. Add a reusable doc type to [`docs/doc-types/`](./doc-types/README.md)
only in a separate, deliberately sanitized repository change. Ordinary sync
will continue to skip that file's known `drive_id`; any later reclassification
would need a new, explicitly reviewed data migration that updates the database
and Drive path together. FinDash does not currently implement that command.

## Filename convention

Use a descriptive but privacy-safe shape:

```
YYYY-MM-DD-<institution>-<doc-type>-<opaque-index>.<ext>
```

Examples:

- `2026-04-27-bank-hapoalim-statement-01.xlsx` — first Hapoalim statement filed for that date
- `2026-04-26-pension-statement-01.pdf` — pension statement
- `2026-05-06-stock-trade-confirmation-01.jpg` — first trade confirmation filed for that date

`<opaque-index>` is only a sequence such as `01`; it must not be derived from an
account/card number, balance, amount, person, employer, security, counterparty,
Drive ID, or a hash of any of those. **The filename is only a routing hint, not
the source of truth.** The sync skill confirms the type and facts by reading the
private content.

Payslips have a different convention:

```
<month-name>-<YY>-payroll.pdf   e.g. apr-26-payroll.pdf
```

## Sync semantics

- Files in `dump/` **are** moved and renamed by the sync skill — that's the whole point of the folder. Ordinary sync does not revisit or re-file an already-known `drive_id`, and no reclassification command currently exists. `documents.drive_path` records the *intended* destination at ingest time, so it may briefly lead the actual move when Drive is unavailable.
- Dedup happens on `documents.drive_id` (stable across moves), so renaming or moving a file later doesn't cause a re-ingest.
- `finance.db` at the root is overwritten by the **weekly** backup (sync skips the upload when the last successful backup is under 7 days old). Old copies live in `backups/`. Day-to-day, the local `data/finance.db` is the source of truth.
- Do not manually reorganize an ingested file if its current path must remain reflected in `documents.drive_path`: ordinary sync deliberately does not maintain paths for known IDs. A future maintenance tool would need to update Drive and the database together; none is currently implemented.
- Every flow-driven Drive operation (layout preparation, listing, download, move, backup) is best-effort: failure becomes a `⚠️` summary warning and never fails the local run. Telegram receives that summary only when configured.
