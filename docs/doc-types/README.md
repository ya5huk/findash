# Doc types

How to recognize each kind of source file, what data it contains, and which tables it should populate. This file is **informational, not procedural** — the sync skill uses judgment to handle edge cases.

## Principles

1. **Filename + folder = first-pass classification.** Confirm by reading content.
2. **Content is the source of truth.** If the filename's amount disagrees with what's in the file, trust the file.
3. **Dedup on `documents.drive_id`.** Don't re-ingest a file you've already seen, even if it appears under a different path.
4. **Use judgment for categorization.** A transfer between *your* accounts is not an expense, even if the bank statement makes it look like one. See [Classification](./classification.md).
5. **Closing balance comes from the statement, not from `SUM(transactions)`.** Bank statements only cover a window; the account may have had a balance before the window began. Read the running-balance column on the last row (e.g. Hapoalim XLSX's יתרה בש"ח), or the "final balance" cell. If neither is present, leave the `balances` row out and flag it in `documents.notes` rather than inventing one. (Anti-pattern: for a closed sub-account, summing only in-window transactions can show a false negative balance — read the statement's true closing balance instead.)
6. **Israeli locale: dates are DD/MM/YYYY.** Hand-typed and Excel-exported Israeli dates are day-first. A cell whose day is ≤ 12 is *ambiguous* — it could pass as MM/DD, and Excel may have stored it under that reading (e.g. `07/04` = 7 Apr saved as 4 Jul). Resolve every ambiguous date before trusting it: cells with day > 12 are self-resolving; for the rest, cross-check against independent signals — a recorded FX rate vs `fx_rates` on each candidate date, chronological / running-balance monotonicity, or cross-document references. `xlsx_to_rows.py` lists each ambiguous cell in its `ambiguous_dates` output as `{cell, value, swapped}`.

## Folder routing

When a file arrives in `dump/`, the sync skill maps its doc_type to a destination folder + filename pattern:

| doc_type                  | Folder                          | Filename                                                                  |
|---------------------------|---------------------------------|---------------------------------------------------------------------------|
| `payslip`                 | `payslips/<YYYY>/`              | `<month>-<YY>-payroll.pdf` (e.g. `apr-26-payroll.pdf`)                    |
| `bank_statement`          | `full-statements/<YYYY-MM>/`    | `<YYYY-MM-DD>-bank-<institution>-<acct>-<balance>.<ext>`                  |
| `mastercard_statement`    | `full-statements/`              | `<YYYY-MM-DD>-bank-<institution>-mastercard-<last4>-<balance>.<ext>`      |
| `pension_statement`       | `long-term-savings/<YYYY>/`     | `<YYYY-MM-DD>-<institution>-pension-pension-statement.pdf`                |
| `training_fund_statement` | `long-term-savings/<YYYY>/`     | `<YYYY-MM-DD>-<institution>-training-fund-training-fund-statement.pdf`    |
| `pension_movements`       | `long-term-savings/<YYYY>/`     | `<YYYY-MM-DD>-<institution>-pension-movements-pension-statement.pdf`      |
| `trade_history`           | `investments/<YYYY>/`           | `<YYYY-MM-DD>-stock-events-transactions-<id>.xlsx`                        |
| `investment_deposits`     | `investments/<YYYY>/`           | `<YYYY-MM-DD>-investment-deposits-<total>.xlsx`                           |
| `trade_confirmation`      | `investments/<YYYY>/`           | `<YYYY-MM-DD>-stock-trade-confirmation-<amount>.<ext>`                    |
| `brokerage_screenshot`    | `investments/<YYYY>/`           | `<YYYY-MM-DD>-<institution>-<context>-<amount>.jpg`                       |
| `brokerage_periodic_statement` | `investments/<YYYY>/`      | `<YYYY-MM-DD>-<institution>-periodic-statement-<total-ils>.pdf`           |
| `savings_screenshot`      | `long-term-savings/<YYYY>/`     | `<YYYY-MM-DD>-<institution>-<product>-<amount>.jpg`                       |
| `employment_agreement`    | `payslips/`                     | preserved filename                                                        |
| `manual_networth_log`     | `investments/<YYYY>/`           | `<YYYY-MM-DD>-net-worth-<id>.xlsx`                                        |
| `manual_trading_journal`  | `investments/<YYYY>/`           | `<YYYY-MM-DD>-stock-market-operations-<id>.xlsx`                          |
| `fx_conversion`           | `fx-conversions/<YYYY>/`        | `<YYYY-MM-DD>-<institution>-fx-<from>-<to>-<src-amount>.<ext>`            |
| `bank_screenshot`         | `full-statements/<YYYY-MM>/`    | `<YYYY-MM-DD>-bank-<institution>-<acct>-screenshot-<balance>.<ext>`       |
| `sprint_statement`        | `long-term-savings/<YYYY>/`     | `<YYYY-MM-DD>-bank-hapoalim-sprint-<deposit-id>-<month-name>-statement.pdf` |
| `bank_deposit_notice`     | `long-term-savings/<YYYY>/`     | `<YYYY-MM-DD>-bank-hapoalim-savings-<deposit-id>-<event>.pdf` |
| `bank_api_dump`           | `full-statements/<YYYY-MM>/`    | `<YYYY-MM-DD>-hapoalim-<acct-suffix>-api-fetch[__tag…].json`                |
| `bank_api_notes`          | `full-statements/<YYYY-MM>/`    | `<YYYY-MM-DD>-hapoalim-<acct-suffix>-api-fetch[__tag…].notes.md`            |
| `cal_api_dump`            | `full-statements/<YYYY-MM>/`    | `<YYYY-MM-DD>-cal-<acct-suffix>-api-fetch[__tag…].json`                     |
| `cal_api_notes`           | `full-statements/<YYYY-MM>/`    | `<YYYY-MM-DD>-cal-<acct-suffix>-api-fetch[__tag…].notes.md`                 |
| `bank_security_notice`    | `investments/<YYYY>/`           | `<YYYY-MM-DD>-bank-hapoalim-stock-<context>-<amount>.pdf`                   |
| `bank_fx_notice`          | `fx-conversions/<YYYY>/`        | `<YYYY-MM-DD>-bank-hapoalim-fx-<from>-<to>-<amount>.pdf`                    |

The amount-in-filename convention uses `_` for the decimal point (e.g. `<major>_<minor>`).

If a `dump/` file doesn't match any row above, the sync proposes a new doc_type, adds a row here, writes a short prose section on the matching catalogue page, and moves the file to the new folder. After adding a new row, the sync re-checks existing files to see if any now fit the new destination better.

---

## Catalogue

Each source folder has its own page describing the doc types it holds, what they contain, and which tables they populate:

- [Payslips](./payslips.md) — Israeli תלוש משכורת PDFs → `payslips`, `payslip_line_items`.
- [Investments](./investments.md) — brokerage exports, screenshots, manual logs, sell events → `trades`, `securities`, `positions`, `balances`, `transactions`, `fx_rates`.
- [Long-term savings](./long-term-savings.md) — pension, training fund, and bank deposit/savings products → `accounts`, `balances`, `transactions`.
- [Full statements](./full-statements.md) — checking & credit-card statements, screenshots, and the bank/Cal API fetch pairs → `transactions`, `balances`.
- [FX conversions](./fx-conversions.md) — internal brokerage ILS↔USD conversions → `transactions`, `fx_rates`.
- [Classification](./classification.md) — the expense-vs-transfer buckets and the judgment calls that govern categorization.
