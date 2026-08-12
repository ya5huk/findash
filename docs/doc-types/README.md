# Doc types

How to recognize each kind of source file, what data it contains, and which tables it should populate. This file is **informational, not procedural** — the sync skill uses judgment to handle edge cases.

## Principles

1. **Folder = first-pass archetype.** A file's location suggests which archetype it is; the content confirms it.
2. **Content is the source of truth.** Filenames are privacy-safe routing hints;
   never derive facts from them.
3. **Dedup on `documents.drive_id`.** Don't re-ingest a file you've already seen, even if it appears under a different path.
4. **Use judgment for categorization.** A transfer between *your* accounts is not an expense, even if the bank statement makes it look like one. See [Classification](./classification.md).
5. **Closing balance comes from the statement, not from `SUM(transactions)`.** Bank statements only cover a window; the account may have had a balance before the window began. Read the running-balance column on the last row (e.g. Hapoalim XLSX's יתרה בש"ח), or the "final balance" cell. If neither is present, leave the `balances` row out and flag it in `documents.notes` rather than inventing one. (Anti-pattern: for a closed sub-account, summing only in-window transactions can show a false negative balance — read the statement's true closing balance instead.)
6. **Israeli locale: dates are DD/MM/YYYY.** Hand-typed and Excel-exported Israeli dates are day-first. A cell whose day is ≤ 12 is *ambiguous* — it could pass as MM/DD, and Excel may have stored it under that reading (e.g. `07/04` = 7 Apr saved as 4 Jul). Resolve every ambiguous date before trusting it: cells with day > 12 are self-resolving; for the rest, cross-check against independent signals — a recorded FX rate vs `fx_rates` on each candidate date, chronological / running-balance monotonicity, or cross-document references. `xlsx_to_rows.py` lists each ambiguous cell in its `ambiguous_dates` output as `{cell, value, swapped}`.

## Archetypes

findash has **five modeled archetypes plus an `other/` review folder.** There is
no closed registry of document labels: read the private content, choose a modeled
archetype when it genuinely fits, and otherwise retain it as `other`. The pages
below are worked examples that teach judgment, not filename rules.

| Archetype (folder) | What generally lands here | Tables it feeds |
|---|---|---|
| **`payslips/`** | Israeli payslip PDFs; employment agreements | `payslips`, `payslip_line_items` |
| **`full-statements/`** | bank checking & credit-card activity — XLSX/PDF exports and app screenshots (the `fetch-bank-data` skill's API dump + notes pairs share this archetype but stay local — see [Full statements](./full-statements.md)) | `transactions`, `balances` |
| **`investments/`** | brokerage exports, screenshots, manual logs, trade confirmations, periodic statements | raw trades/transactions, canonical events, snapshot evidence, securities, FX |
| **`long-term-savings/`** | pension, training fund, and bank deposit/savings products | `accounts`, `balances`, `transactions` |
| **`fx-conversions/`** | internal ILS↔USD conversions inside the brokerage | `transactions`, `fx_rates` |
| **`other/`** | unmatched private sources awaiting a deliberately reviewed model/catalogue change | `documents`; facts only when their meaning is supported |

`documents.doc_type` is a free-text label you set to whatever describes the file (e.g. `pension-periodic-statement`) — a human-readable note, **not** a value drawn from a closed set.

**Routing a `dump/` file:** read it, decide which modeled archetype fits, and
move it with a privacy-safe name (the Drive-side move remains best-effort).
`drive_id`, never the filename, deduplicates it. If none fits, use `other/<YYYY>/`
and keep the review note private. A normal finance run never edits this public
catalogue from private input; add a reusable type only as a separate, sanitized
repository change. That catalogue change does not automatically revisit known
`drive_id` values. No reclassification command currently exists; changing
existing private data would require a separately designed and reviewed migration.

## Catalogue

Each archetype has its own page describing the shapes it commonly holds, what they contain, and which tables they populate:

- [Payslips](./payslips.md) — Israeli תלוש משכורת PDFs → `payslips`, `payslip_line_items`.
- [Investments](./investments.md) — brokerage exports, screenshots, manual logs, sell events → `trades`, `securities`, `positions`, `balances`, `transactions`, `fx_rates`.
- [Long-term savings](./long-term-savings.md) — pension, training fund, and bank deposit/savings products → `accounts`, `balances`, `transactions`.
- [Full statements](./full-statements.md) — checking & credit-card statements, screenshots, and the bank/Cal API fetch pairs → `transactions`, `balances`.
- [FX conversions](./fx-conversions.md) — internal brokerage ILS↔USD conversions → `transactions`, `fx_rates`.
- [Classification](./classification.md) — the expense-vs-transfer buckets and the judgment calls that govern categorization.
