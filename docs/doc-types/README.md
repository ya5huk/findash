# Doc types

How to recognize each kind of source file, what data it contains, and which tables it should populate. This file is **informational, not procedural** — the sync skill uses judgment to handle edge cases.

## Principles

1. **Folder = first-pass archetype.** A file's location suggests which archetype it is; the content confirms it.
2. **Content is the source of truth.** If the filename's amount disagrees with what's in the file, trust the file.
3. **Dedup on `documents.drive_id`.** Don't re-ingest a file you've already seen, even if it appears under a different path.
4. **Use judgment for categorization.** A transfer between *your* accounts is not an expense, even if the bank statement makes it look like one. See [Classification](./classification.md).
5. **Closing balance comes from the statement, not from `SUM(transactions)`.** Bank statements only cover a window; the account may have had a balance before the window began. Read the running-balance column on the last row (e.g. Hapoalim XLSX's יתרה בש"ח), or the "final balance" cell. If neither is present, leave the `balances` row out and flag it in `documents.notes` rather than inventing one. (Anti-pattern: for a closed sub-account, summing only in-window transactions can show a false negative balance — read the statement's true closing balance instead.)
6. **Israeli locale: dates are DD/MM/YYYY.** Hand-typed and Excel-exported Israeli dates are day-first. A cell whose day is ≤ 12 is *ambiguous* — it could pass as MM/DD, and Excel may have stored it under that reading (e.g. `07/04` = 7 Apr saved as 4 Jul). Resolve every ambiguous date before trusting it: cells with day > 12 are self-resolving; for the rest, cross-check against independent signals — a recorded FX rate vs `fx_rates` on each candidate date, chronological / running-balance monotonicity, or cross-document references. `xlsx_to_rows.py` lists each ambiguous cell in its `ambiguous_dates` output as `{cell, value, swapped}`.

## Archetypes

findash has **five archetypes — one per vault folder.** Every document is an instance of one of them. There is no fixed registry of document "types" to maintain: you read a file, judge which archetype it is, and route it to that folder. The per-archetype pages below describe the shapes you'll *commonly* see and which tables each feeds — as **worked examples that teach the judgment, not a closed list to keep complete.**

| Archetype (folder) | What generally lands here | Tables it feeds |
|---|---|---|
| **`payslips/`** | Israeli payslip PDFs; employment agreements | `payslips`, `payslip_line_items` |
| **`full-statements/`** | bank checking & credit-card activity — XLSX/PDF exports, app screenshots, and the `fetch-bank-data` skill's API dump + notes pairs | `transactions`, `balances` |
| **`investments/`** | brokerage exports, screenshots, manual logs, trade confirmations, periodic statements | `trades`, `securities`, `positions`, `balances`, `transactions`, `fx_rates` |
| **`long-term-savings/`** | pension, training fund, and bank deposit/savings products | `accounts`, `balances`, `transactions` |
| **`fx-conversions/`** | internal ILS↔USD conversions inside the brokerage | `transactions`, `fx_rates` |

`documents.doc_type` is a free-text label you set to whatever describes the file (e.g. `harel-pension-statement`) — a human-readable note, **not** a value drawn from a closed set. (One reserved internal label, `yahoo_dividend_estimate`, is written by sync itself and read by the renderer — see [Investments](./investments.md).)

**Routing a `dump/` file:** read it, decide which archetype fits best, and move it into that folder with a descriptive name. There's no required filename format — `drive_id` is what dedups a file, never its name. Most files map cleanly. If something genuinely belongs to none of the five (a new financial domain — say, insurance), use judgment: give it a folder, and add a short prose note to a catalogue page describing what it holds and which tables it feeds. There is **no routing table to grow and no vault re-scan to run** — the archetypes are stable.

## Catalogue

Each archetype has its own page describing the shapes it commonly holds, what they contain, and which tables they populate:

- [Payslips](./payslips.md) — Israeli תלוש משכורת PDFs → `payslips`, `payslip_line_items`.
- [Investments](./investments.md) — brokerage exports, screenshots, manual logs, sell events → `trades`, `securities`, `positions`, `balances`, `transactions`, `fx_rates`.
- [Long-term savings](./long-term-savings.md) — pension, training fund, and bank deposit/savings products → `accounts`, `balances`, `transactions`.
- [Full statements](./full-statements.md) — checking & credit-card statements, screenshots, and the bank/Cal API fetch pairs → `transactions`, `balances`.
- [FX conversions](./fx-conversions.md) — internal brokerage ILS↔USD conversions → `transactions`, `fx_rates`.
- [Classification](./classification.md) — the expense-vs-transfer buckets and the judgment calls that govern categorization.
