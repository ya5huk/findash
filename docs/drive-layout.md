# Drive layout

## Root

The vault is a single Google Drive folder. Its **folder ID** is per-user and lives in [`.secrets/drive`](../.secrets/drive) (gitignored, chmod 600):

```
root_folder_id=<paste your vault folder ID here>
```

Get the ID from your Drive folder's URL: `drive.google.com/drive/folders/<ID>`. Skills read it from `.secrets/drive` and pass it to rclone via `--drive-root-folder-id=<ID>`. **Do not hardcode it anywhere else.**

Suggested human-readable folder name: `finance-vault`. The folder ID is the stable identifier; the name can change.

## Folder structure

```
<DRIVE_ROOT>/
├── dump/                           # drop new files here unsorted; sync triages on next run
├── payslips/<YYYY>/                # PDF, password-protected
├── investments/<YYYY>/             # XLSX exports + JPG screenshots from brokerage and bank capital-market view
├── long-term-savings/<YYYY>/       # PDF statements from Harel (pension + training fund) + JPG savings screenshots
├── full-statements/<YYYY-MM>/      # bank Hapoalim checking statements (XLSX/PDF) + Mastercard XLSX
├── fx-conversions/<YYYY>/          # Hapoalim FX conversion screenshots (ILS ⇄ USD between own sub-accounts)
├── finance.db                      # current SQLite snapshot, overwritten on each sync
└── backups/                        # finance-<timestamp>.db backups (retention TBD)
```

Files at the top of categories (not inside `<YYYY>/`) are also valid — the sync scans recursively.

### `dump/` — inbox for unsorted files

Drop any new finance document (payslip, statement, screenshot, anything) into `dump/` without thinking about where it belongs. The sync skill triages each file on its next run: it reads the content, decides the right destination folder, renames it to match the filename convention below, and moves it via `rclone moveto`. Drive preserves the file's `drive_id` across the move, so the document audit trail stays intact (`documents.drive_path` gets updated to the new path if the file had already been ingested).

If the sync encounters a file in `dump/` whose content doesn't fit any existing doc type, it creates a new doc type:

1. Proposes a snake_case name (e.g. `insurance_statement`, `tax_certificate`), a destination folder under the vault, and a filename pattern.
2. Documents the new type in [`docs/doc-types.md`](./doc-types.md).
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

- Files in `dump/` **are** moved and renamed by the sync skill — that's the whole point of the folder. Once a file is outside `dump/`, the sync only re-files it when a new doc type/folder is introduced (see above).
- Dedup happens on `documents.drive_id` (stable across moves), so renaming or moving a file later doesn't cause a re-ingest.
- `finance.db` at the root is overwritten on each sync. Old copies live in `backups/`.
- If you re-organize folders in Drive manually, that's fine — `drive_id` is stable across moves; the next sync's stale-path check will update `documents.drive_path`.
