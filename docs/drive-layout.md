# Drive layout

## Root

The vault is a single Google Drive folder. Its **folder ID** is per-user and lives under the `[drive]` section of `.secrets/findash` (gitignored, chmod 600):

```
[drive]
root_folder_id=<paste your vault folder ID here>
```

Get the ID from your Drive folder's URL: `drive.google.com/drive/folders/<ID>`. All skill Drive access goes through the official **Google Drive connector** (added in Claude — see [setup](./setup.md#3-optional-connect-google-drive)). Skills surface the ID with `python3 scripts/drive_root.py` and scope every connector call to that folder tree by `parentId`. **Do not hardcode it anywhere else**, and never search, list, or touch anything outside the vault: the connector can see the whole Drive, and only this folder is findash's business.

Suggested human-readable folder name: `finance-vault`. The folder ID is the stable identifier; the name can change. Category folders are located by exact title directly under the root, so keep the names below verbatim.

## Folder structure

```
<DRIVE_ROOT>/
├── dump/                           # drop new files here unsorted; sync triages on next run
├── payslips/<YYYY>/                # PDF, password-protected
├── investments/<YYYY>/             # XLSX exports + JPG screenshots from brokerage and bank capital-market view
├── long-term-savings/<YYYY>/       # PDF statements from Harel (pension + training fund) + JPG savings screenshots
├── full-statements/<YYYY-MM>/      # bank Hapoalim checking statements (XLSX/PDF) + Mastercard XLSX
├── fx-conversions/<YYYY>/          # Hapoalim FX conversion screenshots (ILS ⇄ USD between own sub-accounts)
└── other/<YYYY>/                   # unrecognized/private source types pending a reviewed catalogue addition
```

Files at the top of categories (not inside `<YYYY>/`) are also valid — the sync scans recursively.

Setup, doctor, and sync prepare the layout idempotently: any fixed folder missing directly under the root is created through the connector, and existing folders are never renamed or moved. Database backups live locally in `data/backups/`; a vault from before that change may still hold a `finance.db` file and a `backups/` folder, which are no longer maintained and can be deleted by hand.

### `dump/` — inbox for unsorted files (human drops only)

Drop any new finance document (payslip, statement, screenshot, anything) into `dump/` without thinking about where it belongs. Only humans write here — the automatic bank fetch stages its files locally, never in Drive. The sync skill triages each file on its next run: it downloads it through the connector into local staging, reads the content, decides the right destination folder and a descriptive name (recorded in `documents.drive_path`), ingests it, and then moves that one direct `dump/` file with the connector's update call (new parent + new title). Drive preserves the file's `drive_id` across the move, so the document audit trail stays intact. Sync never overwrites a destination: it checks the destination folder for a same-named file first, and any collision leaves both Drive files and identities untouched for review. The move is **best-effort**: if Drive drops mid-run, an already-ingested file simply lingers in `dump/` (harmless — `drive_id` dedup stops re-ingestion) and the move retries on the next run.

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
- Database backups are local: sync archives a consistent weekly snapshot under `data/backups/` (gated by `meta.last_db_backup_at`, pruned to the newest eight). Nothing is uploaded to the vault; day-to-day, the local `data/finance.db` is the source of truth.
- Do not manually reorganize an ingested file if its current path must remain reflected in `documents.drive_path`: ordinary sync deliberately does not maintain paths for known IDs. A future maintenance tool would need to update Drive and the database together; none is currently implemented.
- Every flow-driven Drive operation (layout preparation, listing, download, move) is best-effort: failure becomes a `⚠️` summary warning and never fails the local run.
