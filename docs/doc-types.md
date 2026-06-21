# Doc types

How to recognize each kind of source file, what data it contains, and which tables it should populate. This file is **informational, not procedural** — the sync skill uses judgment to handle edge cases.

## Principles

1. **Filename + folder = first-pass classification.** Confirm by reading content.
2. **Content is the source of truth.** If the filename's amount disagrees with what's in the file, trust the file.
3. **Dedup on `documents.drive_id`.** Don't re-ingest a file you've already seen, even if it appears under a different path.
4. **Use judgment for categorization.** A transfer between *your* accounts is not an expense, even if the bank statement makes it look like one. See "Judgment calls" at the bottom.
5. **Closing balance comes from the statement, not from `SUM(transactions)`.** Bank statements only cover a window; the account may have had a balance before the window began. Read the running-balance column on the last row (e.g. Hapoalim XLSX's יתרה בש"ח), or the "final balance" cell. If neither is present, leave the `balances` row out and flag it in `documents.notes` rather than inventing one. (Anti-example: a closed Hapoalim sub-account showed an apparent negative balance in early renders because the code summed in-window transactions instead of reading the statement's true closing balance.)

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

If a `dump/` file doesn't match any row above, the sync proposes a new doc_type, adds a row here, and moves the file to the new folder. After adding a new row, the sync re-checks existing files to see if any now fit the new destination better.

---

## payslips/ — Israeli תלוש משכורת

- **Format:** PDF, password-protected. Passwords are stored locally under `[pdf-passwords]` in `.secrets/findash`.
- **Unlock:** `qpdf --password=$PASS --decrypt <file> <tmp>` then read the temp file. Delete temp when done.
- **Typical content:**
  - Employer name + ID
  - Pay period (start / end / pay date)
  - Gross, net
  - Deductions: מס הכנסה, ביטוח לאומי, ביטוח בריאות, ניכוי פנסיה (תגמולי עובד), קרן השתלמות (תגמולי עובד)
  - Employer contributions: הפרשת פנסיה (תגמולים + פיצויים), הפרשת קרן השתלמות
  - Misc earnings/deductions (bonuses, allowances, refunds)
- **Writes to:**
  - `payslips` — one row, columns for the structured bits.
  - `payslip_line_items` — one row per unusual line (anything not in the explicit columns), with `kind` in {earning, deduction, info}.
  - `documents` — one row.

## investments/ — brokerage exports & screenshots

You currently use **Excellence Investment Management (אקסלנס)** as your brokerage. Files in this folder describe trades, positions, and brokerage account state.

### `*-stock-events-transactions-*.xlsx` — full trade history

- **Columns:** `Symbol, Date, Quantity (signed), Price, Price Currency, Fees Percentage, Fees Amount, Fees Currency`
- **Maps to** `trades`: one row per non-zero Quantity row. `side='buy'` if Quantity > 0 else `'sell'`. `shares = abs(Quantity)`.
- **Securities** need a row in `securities` first — create on first sighting; `asset_class='stock'` unless obviously an ETF.

### `*-investment-deposits-*.xlsx` — cash deposits into brokerage

- **Maps to** `transactions` on the **brokerage account**: `category='deposit'`, positive amount. Store the ILS source + rate in `description` as `from <ILS> ILS @ <rate>`.
- The same money usually shows up as an outflow on a Hapoalim checking statement (counterparty: אקסלנס). Both rows should exist — they're not duplicates, they're the two sides of a transfer. The `reference` from the bank side helps tie them together.
- **Date disambiguation — the DD/MM↔MM/DD gotcha (do this every ingest).** The Date column is user-typed Israeli DD/MM/YYYY, but Excel may have stored an *ambiguous* cell (day ≤ 12, so the day could pass as a month) under a MM/DD reading — e.g. `07/04` (7 Apr) saved as 4 Jul. Cells with day > 12 are self-resolving. `xlsx_to_rows.py` lists every such cell in its `ambiguous_dates` output as `{cell, value, swapped}`. For each, pick `value` vs `swapped` by the same cross-checks as `manual_networth_log` above: (1) **FX match** — compare the row's `$ price` to `fx_rates` on both candidate dates; the user's recorded rate runs a small spread *above* mid, so the closer candidate (after allowing ~0.0–0.06) wins, and a tight match is decisive; (2) **cumulative `Total ($)` / row order** — the sheet is strictly chronological top→bottom, so the chosen dates must keep deposits monotonic in time. (One past sheet had all 7 ambiguous cells flipped; both checks agreed unanimously.)
- **Do not seed `fx_rates` from this sheet.** The `$ price` is the user's transacted rate (carries a spread) and lives only in the transaction `description`. Valuation FX comes from Yahoo; the authoritative in-brokerage conversion rate is captured by the `fx_conversion` screenshot (which *does* write `fx_rates`, `source='document'`), not here. Seeding a spread-laden rate — worse, on a mis-parsed date — silently corrupts USD valuations (this is the exact bug that put a bogus `3.84` USD→ILS into `fx_rates` on a non-existent July date).

### `*-net-worth-*.xlsx` — brokerage account value snapshot

- **Maps to** `balances` on the brokerage account: `component=NULL`, `as_of = doc_date`, `amount_minor = total value`.

### `*-stock-market-operations-*.xlsx` (`manual_trading_journal`)

- User-maintained trading journal: one row per closed buy/sell pair with prices, fees, P/L, and free-form "Loss/Win Explanation" prose. The same trades are already captured in `trades` from the brokerage's `trade_history` export, so this file's structured data is redundant. The prose isn't normalizable. **Skip extraction** unless the user explicitly wants the reflections preserved.

### `*-net-worth-*.xlsx` (`manual_networth_log`)

- User-maintained monthly net-worth journal. Each entry is a **section** delimited by a date in column A — never by row count. **Walk header-to-header**, not row-by-row: locate every Excel row whose column A is either a date serial (numeric cell with a date style, e.g. `46026.0`) or a `DD/MM/YYYY` text cell; the section runs from one date row up to (but not including) the next date row. Don't use the parser's flat row list — use the raw `xl/sharedStrings.xml` + `xl/worksheets/sheet1.xml` if needed.
- Two on-disk layouts coexist:
  - **Modern (post-2025-10):** 3 Excel rows per entry — `[date | detail in C/D/E with `\n`-separated `Label:\nValue` pairs] / [summary B-K] / [blank]`.
  - **Older (pre-2025-10):** 5–8 Excel rows per entry — labels and values are on *separate* rows (col C row N = `'Poalim:'`, col C row N+1 = `'<amount>₪'`); the summary row sits at the bottom of the section. The parser's flat rows-list mangles this; section walking recovers it.
- When a section has no detail breakdown (only a summary `C` total), use the summary value and note the imprecision in `documents.notes` — older entries may include small Paypal amounts folded into the Cash total.
- **Maps to** `balances` (`source_doc_id` = this doc):
  - Cash → primary Hapoalim checking account, `component=NULL`.
  - Locked-Poalim sub-amount → Hapoalim Digital Sprint savings account, `component=NULL`. Only valid for dates ≥ the Sprint open date recorded in the DB.
  - **Skip Pension and Training Fund** rows — the log gives single totals but the DB requires component breakdown (`tagmul_employee` / `tagmul_employer` / `pitsuyim`). Guessing the split would corrupt the chart.
  - **Skip Hafenix equity totals** — the dashboard computes brokerage value from `trades` × historical prices; don't double-source. Hafenix **cash** is now tracked separately, ideally via a Hafenix balance screenshot (see `brokerage_screenshot — Hafenix balance screenshot` below). If a `manual_networth_log` entry explicitly breaks out Hafenix cash by currency, insert those as `balances` rows with `component='cash_usd' | 'cash_gbp' | 'cash_ils'` (screenshot is still the preferred source).
- **Date hygiene — the Excel-locale gotcha.** The user types Israeli DD/MM/YYYY. Cells stored as text (e.g. `14/02/2026`) come through verbatim; cells Excel auto-converted to date serials come through ISO (`2026-02-14`). When the day is >12, the DD/MM interpretation is the only legal one, so the date is recoverable either way. **When day and month are both ≤ 12, the auto-converted ISO output may be wrong**: e.g. `2025-04-12` is really `2025-12-04` and `2025-04-10` is really `2025-10-04`. Resolve ambiguous cases using these checks, in order of decisiveness:
  1. **FX-rate column (most decisive).** Each entry's row has the user's `1$` and `1£` cells (cols 9, 10). Look up the actual USD/ILS rate in `fx_rates` for each candidate date — the one matching the typed rate within a few mils is the right date. Tight match (Δ ≤ 0.01) is conclusive; mismatch (Δ > 0.05) is conclusive in the other direction.
  2. **Cross-doc references in `Income since last entry` / `Notes` columns.** A "+<net salary> job" entry must postdate the matching payslip; a "now <total> USD were ever transferred" cumulative tally pins the date between the deposit that reached that total and the next deposit.
  3. **Sprint balance monotonicity.** Sprint pays ~2.3% nominal annually with no withdrawals, so its balance is strictly increasing. Any candidate date that produces a Sprint balance smaller than a later balance is wrong.
  4. **Sprint open date.** Sprint opened 2025-05-18; any candidate before that is impossible.
- **Dedup:** use `INSERT OR IGNORE` against `UNIQUE(account_id, as_of, component)`. The log's entries should not overlap with official statement closing balances; if they do (e.g. a manual entry on the same day as a Hapoalim XLSX closing), prefer the statement.

### `*-bank-hapoalim-capital-market-*.jpg` — Hapoalim brokerage screenshot

- Read with Read tool's image support. Transcribe positions and/or total value. Treat as a position snapshot if listing securities, or a `balances` row if just a total.

### Hafenix balance screenshot (`brokerage_screenshot` for Hafenix cash)

- Screenshot from Hafenix's app showing the account's cash holdings, typically broken out per currency (USD, GBP, ILS). May also include a positions list — those are informational only; positions are tracked via `trades`.
- **Maps to** `balances` on the Hafenix brokerage account, one row per non-zero currency:
  - `as_of = <screenshot date>`
  - `component = 'cash_usd' | 'cash_gbp' | 'cash_ils'` (lowercase ISO currency)
  - `currency = 'USD' | 'GBP' | 'ILS'`
  - `amount_minor = <amount in minor units of that currency>` (cents/pence/agorot)
  - `source_doc_id = <doc>`
- **Dedup:** `INSERT OR IGNORE` on `UNIQUE(account_id, as_of, component)`. Multiple currencies on the same date insert as separate rows because `component` differs.
- **Do not** write a `component=NULL` row for Hafenix — that slot was used by the deprecated "total brokerage value" pattern and would conflict with the per-currency cash rows.
- Skip a currency component if its amount is zero (keeps the chart clean).
- This is the **canonical** path for tracking Hafenix cash. The renderer combines these snapshots with post-snapshot flows (deposits, FX conversions, sells, and buy cash-legs derived from `trades`) so the displayed balance stays accurate between screenshots.

### Hafenix periodic statement (`brokerage_periodic_statement`)

- Multi-page PDF generated by Hafenix/Excellence with the title **דוח תקופתי**, a brokerage account header, and a heading that says **הננו מתכבדים להציג את מצב חשבונך אצלנו נכון לתאריך: DD/MM/YYYY**. Typically one per calendar month, cutoff = month-end.
- **Two main sections:**
  - **פירוט יתרות** (Holdings detail) — every position the account held on the cutoff date. Columns left-to-right when reading RTL output: `מספר נייר` (security #), `שם נייר` (name), `כמות` (quantity), `שער נוכחי` (current price in *agorot* — divide by 100 for ILS), `עלות הרכישה` (cost basis in ILS), `שווי נייר בשקלים` (market value in ILS), `אחוז מהתיק` (% of portfolio).
  - **פירוט תנועות** (Transactions detail) — every transaction during the report period: trades, dividends (`הפ/דיב` = dividend received, `מס/דיב` = dividend withholding tax), broker commissions (`עמלה`), FX conversions, internal deposits from Bank Hapoalim (`הפקדה לבנק בגין הפועלים`), receipts (`מס שולם` / `דולר ארה"ב קניה`), etc.
- **Maps to:**
  - **`balances` — month-end cash snapshot** (one row per non-zero currency, dedup via `UNIQUE(account_id, as_of, component)`):
    - **USD cash:** quantity of security `99028` (דולר ארה"ב). If a `99218 התחייבות דולרית` (USD liability) row is also present on the same statement, subtract its quantity from 99028's quantity to get net USD cash — the liability is a pending-settlement debit, not separately tracked.
    - **GBP cash:** quantity of security `99069` (לישט). Skip if zero/absent.
    - **ILS cash:** value (in ILS) of the row labeled `יתרה כספית` or `יתרה פח"ק בבנק` (residual cash). Skip if zero. May be negative on rare days (slight overdraft) — store as-is.
  - **`fx_rates`:**
    - `(date=cutoff, base='USD', quote='ILS', rate = 99028.שער_נוכחי / 100)`.
    - `(date=cutoff, base='GBP', quote='ILS', rate = 99069.שער_נוכחי / 100)` if GBP held.
  - **`documents` — one row** per PDF, `doc_type='brokerage_periodic_statement'`, `doc_date = cutoff`. Skip if `drive_id` already ingested.
- **Skip most transaction-level extraction.** The trades, deposits, and FX conversions visible in the report's פירוט תנועות section are already captured from other sources (`trade_history` XLSX, `investment_deposits` XLSX, `fx_conversion` screenshots) — re-ingesting them would create duplicate rows. The periodic statement's primary value to the dashboard is the **month-end cash snapshot** and the **FX rates on those dates**.
- **EXCEPT: `הפ/דיב` (dividend) rows are extracted as confirmations.** Sync (step 5b) autonomously fetches live Yahoo dividend events every run and inserts them as synthetic `transactions` (`source_doc_id` points to a `doc_type='yahoo_dividend_estimate'` document) so Hafenix cash stays accurate between snapshots. When a periodic statement is ingested:
  - For each `הפ/דיב` row in פירוט תנועות, parse `(pay_date, ticker, net amount as the statement shows it — already post-withholding)`.
  - Delete any matching synthetic transaction on the Hafenix brokerage account: `counterparty=ticker, ABS(julianday(date) - julianday(row_date)) <= 5, source doc has doc_type='yahoo_dividend_estimate'`.
  - Insert the real row referencing the statement's `documents.id` — now `source` is a real Hafenix doc, the dashboard tags it "confirmed".
  - Then run the **snapshot-supersession cleanup** described in `skills/sync-finance-data/SKILL.md` step 5b: drop remaining synthetic dividend transactions on the Hafenix brokerage account dated `<= cutoff_date`, since the new snapshot's cash component already includes them.
- `מס/דיב` (withholding tax) rows are informational only — the synthetic + statement amounts are already net of withholding, so don't insert these as separate transactions.
- **The total portfolio value** (סה"כ row) is informational — it equals positions + cash and the dashboard already computes brokerage equity from `trades` × historical prices. **Do not** insert it as a `component=NULL` balance row; per `[[project_hafenix_cash_tracking]]` Hafenix uses per-currency components only.

### `*-stock-trade-confirmation-*.jpg` — single trade

- One row in `trades`. Read with Read tool's image support.

## long-term-savings/ — locked / long-term vehicles

### `*-harel-pension-pension-statement.pdf`

- Cover page lists fund, status, holder, ID, employer, investment track, total accumulated.
- "תנועות אחרונות" table = recent deposits, broken down by (employee tagmul, employer tagmul, severance).
- "ערכי פדיון" row = balance per component.
- **Maps to:**
  - `accounts` — create once with kind='pension', record `investment_track`.
  - `balances` — 3 rows (one per component) for the as-of date.
  - `transactions` — one row per (deposit month, component); `category='pension_deposit'`, `counterparty=employer`.

### `*-harel-training-fund-training-fund-statement.pdf`

- Same shape as pension but with 2 components (no severance), and the table headers are slightly different.
- Note the **liquidity date** ("מועד נזילות") → `accounts.liquidity_date`.
- **Maps to:** same pattern as pension but 2-component.

### `*-harel-pension-movements-pension-statement.pdf`

- Movement-only report (no balance summary). Use for additional transaction rows; don't insert `balances` unless the report includes a snapshot.

### `*-bank-hapoalim-digital-sprint-deposit-*.jpg`

- Screenshot of a Hapoalim short-term savings deposit. Read with Read tool's image support.
- **Maps to:** `accounts` (kind='savings'), `balances`. Note maturity → `liquidity_date`.

### `*-bank-hapoalim-sprint-<deposit-id>-<month-name>-statement.pdf` (`sprint_statement`)

- Monthly PDF statement for a Hapoalim Digital Sprint fixed deposit. Cover shows opening balance, accrued interest, closing principal, projected re-evaluated balance at next monthly stop, and maturity date (`19.05.26 יחול מועד פרעון הפקדון`).
- The "תחנה" row is a snapshot at the monthly stop — the re-evaluated balance equals principal + accumulated interest at that stop.
- **Maps to:**
  - `balances` on the Sprint account: `as_of = stop date`, `amount_minor = re-evaluated balance`.
  - Don't insert separate interest transactions — interest is implicit in the balance growth and is realized only on redemption.
- On maturity, the principal + interest is credited to the linked checking account as `פרעון פקדון`; record that as a `transfer` (or `savings_withdrawal`) on the checking side, and a closing balance of 0 on the Sprint side.

### `*-bank-hapoalim-savings-<deposit-id>-<event>.pdf` (`bank_deposit_notice`)

- Hapoalim deposit/savings notices for non-Sprint or legacy products. Common events: opening/renewal, monthly standing-order notice, and termination.
- **Maps to:** `accounts` (`kind='savings'`) when the product is material enough to affect net worth; `balances` for explicit or inferable point-in-time deposit value; `transactions` for redemption, tax, or transfer legs when they are not already represented by a checking statement.
- Small historical notices that only confirm already-ingested checking rows may create only a `documents` row with explanatory `notes`.
- For termination notices, store the last pre-redemption balance as principal plus accrued interest before tax, then a zero balance on the redemption date. If the notice shows withholding tax, insert a `tax` transaction on the savings account so the redemption drop is explainable.

### Hapoalim securities notices (`bank_security_notice`)

- Hapoalim bank-side securities trade/gift notices, usually for the internal capital-market account linked to checking.
- If the same trade and cash legs are already captured by a statement or a balance screenshot, treat the notice as audit evidence and avoid double-inserting trades. Otherwise, insert a `trade` on the Hapoalim capital-market account and the matching checking cash leg when a reliable account mapping exists.

### Hapoalim bank FX notices (`bank_fx_notice`)

- Bank-side foreign-currency purchase/sale notices for the checking account, distinct from Hafenix internal FX screenshots.
- Insert document-derived `fx_rates` only when the notice states the actual conversion rate. Insert checking-account transactions only when the statement side is not already present and the cash impact is unambiguous.

## full-statements/ — checking & credit card

### `*-bank-<institution>-<acct>-screenshot-<balance>.jpg` (`bank_screenshot`)

- Mobile/web app screenshot of a Hapoalim checking account showing current balance + recent transactions. Use when the user wants to capture mid-month state before the official XLSX/PDF statement is available.
- Read with Read tool's image support; transcribe each transaction row.
- **Maps to:**
  - `transactions` on the checking account — one row per visible row not already in DB. **Always cross-check `(account_id, date, amount, reference)`** against existing rows to avoid duplicating txns from a later monthly statement.
  - `balances` — one row at the snapshot timestamp using the prominent header balance.
- **Note:** screenshots may show only partial transactions (e.g. last 10). Don't assume completeness — when the official statement arrives later, additional txns may surface.


### `*-bank-hapoalim-<acct>-<balance>.xlsx` or `.pdf`

- Hapoalim checking account statement. Columns (Hebrew, RTL): תאריך, הפעולה, פרטים, אסמכתא, חובה, זכות, יתרה בש"ח, תאריך ערך, לטובת, עבור.
- **Maps to:** `transactions` (one row per data row) + optionally `balances` (final balance, as_of = statement date).
- **Sign:** חובה (debit) → negative amount; זכות (credit) → positive.
- **`reference`:** the אסמכתא column.
- **`counterparty`:** prefer the לטובת column when present; otherwise extract from פרטים.

### `*-bank-hapoalim-mastercard-<card-num>-<balance>.xlsx`

- Credit card statement. Each row = one charge. **Sign convention:** charges are negative (money leaving you), payments to the card from your checking account are positive.
- **Maps to:** `transactions` on the credit-card account.
- **Hapoalim "transaction itemization" exports** (filename in Hebrew: `פירוט עסקאות וזיכויים.xlsx`) have the same shape. Columns: `תאריך עסקה`, `שם בית עסק`, `סכום בש"ח`, `מועד חיוב`, `סוג עסקה`, `מזהה כרטיס בארנק דיגיטלי`, `הנחה`, `הערות`. Use `שם בית עסק` for `counterparty`, `תאריך עסקה` for `date`, `מועד חיוב` for `value_date`, and stash `סוג עסקה` + `הערות` (e.g. "תשלומים | 3 installments" or original USD amount) into `description`.
- **Bit (BIT) rows** are P2P transfers via Israel's Bit app. Set `category='p2p_bit'`. Sign rule: if the row is an **outflow** (negative on this card / charge from your account), it's an **expense** — money you sent to someone. If the row is an **inflow** (positive — money coming back into your account via Bit), it's **not an expense**; it's a transfer-in or refund. The dashboard's expense filter relies on `amount_minor < 0`, so sign alone disambiguates — keep `category='p2p_bit'` regardless of direction.

### API-fetched pairs (`bank_api_dump` / `bank_api_notes` / `cal_api_dump` / `cal_api_notes`)

Produced by the `fetch-bank-data` skill (see `skills/fetch-bank-data/SKILL.md`). Each fetch run drops a pair of files into Drive `dump/` per non-empty account: a `.json` raw scrape and a `.notes.md` sidecar with prose observations. Both files describe the same account on the same fetch date — read them together.

**Filename anatomy:**

```
<YYYY-MM-DD>-<company>-<acct-suffix>-api-fetch[__tag__tag…].(json|notes.md)
```

`<YYYY-MM-DD>` is the fetch date (not txn date). `<company>` ∈ {`hapoalim`, `cal`}. `<acct-suffix>` is the last 4 of the institution's `accountNumber`. Tags are compact observation markers (`roundtrip-5000-excellence`, `installments-amazon`, `fx-usd-zara`, `pending-3`, etc.); when none apply (quiet day) the filename has no `__tag` segment.

#### `bank_api_dump` (Hapoalim raw JSON)

- Library: `israeli-bank-scrapers` (`companyId: 'hapoalim'`). Field shape: `{success, accounts:[{accountNumber, balance, txns:[{date, processedDate, originalAmount, chargedAmount, description, memo, identifier, …}]}]}`. The skill slices `accounts[i]` per account before writing — each file holds one account.
- Amounts are JSON floats; multiply by 100 for `amount_minor`.
- Per-transaction running balance is **not** included — only the latest `balance` on the account object. Treat as a snapshot: `balances` row with `as_of = <fetch date>`, `component=NULL`, `amount_minor = round(balance × 100)`.
- **Maps to:** existing `transactions` + `balances` rows on the matching Hapoalim checking `accounts` row. Dedup at row level by `(account_id, date, amount_minor, reference)` — overlapping date ranges with prior XLSX statements are expected and harmless.

#### `bank_api_notes` (Hapoalim sidecar)

- Plain markdown. Header line names the date + company + acct suffix. Body is a bulleted list of observations the skill made at fetch time. Vocabulary: `roundtrip`, `internal transfer`, `first-time counterparty`, `amount anomaly`.
- Treat each bullet as a **hint, not ground truth.** Verify against the JSON and against cross-source documents (Hafenix periodic statements, FX screenshots) before acting on it. A "first-time counterparty" might just be a known counterparty whose name changed on the bank side.

#### `cal_api_dump` (Cal raw JSON)

- Same outer shape as `bank_api_dump`; per-transaction fields differ:
  - `type` ∈ {`'normal'`, `'installments'`}
  - `identifier` (int) — groups all installment rows of one physical purchase
  - `date` — original purchase date
  - `processedDate` — when **this particular installment** hits the bank (the bill-charge date). Sync maps this to `transactions.value_date`.
  - `originalAmount` + `originalCurrency` — present always; equal `chargedAmount`/`ILS` for domestic purchases. Non-ILS values indicate a foreign-currency charge.
  - `chargedAmount` — ILS amount billed; this becomes `amount_minor` (signed per the schema convention: charges negative, refunds/payments positive).
  - `installments: {number, total}` — present when `type === 'installments'`.
  - `status` ∈ {`'completed'`, `'pending'`}. `pending` rows are not yet on a closed bill.
- Per account, the library returns a single `balance` value. For Cal this is the next-bill amount (סכום לחיוב). **Maps to:** a `balances` snapshot row with `component='next_bill'`, `amount_minor` signed **negative** (liability), `currency='ILS'`, `as_of = <fetch date>`.
- **Maps to** `transactions` on a `kind='credit_card'` `accounts` row (institution `'Cal'`). First-ever Cal fetch: no `accounts` row exists for the returned `accountNumber` — auto-create one with `kind='credit_card'`, `institution='Cal'`, `name='Cal <last4-of-accountNumber>'`, `currency='ILS'`. Autonomy precedent: sync may create accounts / doc types without confirmation.
- **Known card → account mapping (do NOT create a duplicate account):** If Cal returns an `accountNumber` that is the same physical card as an existing Hapoalim-branded Mastercard account, map its txns onto the existing account and dedup against existing rows; never spin up a duplicate `Cal <last4>` account. Keep the concrete account/card mapping in private DB state or source docs, not in committed docs. **Installment caveat:** legacy `mastercard_statement` rows recorded installment legs clustered at the *purchase date*, whereas the Cal API dates each leg at its *charge date*. So `(account_id, date, amount_minor)` dedup leaves some pre-cutover installment legs unmatched — these are a mix of true duplicates (earlier legs) and real gaps. Reconcile those by hand once; don't blind-insert (risks double-counting).
- Sign convention: merchant charge → negative `amount_minor`; refund / payment to the card → positive (matches the Hapoalim Mastercard convention).
- `date` = `txn.date`; `value_date` = `txn.processedDate`.
- `category`: use judgment per merchant. New `merchant`-style categories are open vocab.
- **Lossy enrichments stashed in `description`** (the schema has no structured columns for these yet — out-of-scope follow-up):
  - FX (`originalCurrency !== 'ILS'`): `"AMAZON.COM (USD 45.20 → ILS 167.10 @ 3.697)"`
  - Installments (`type === 'installments'`): `"<merchant> (2/6, group #<identifier>)"` — `identifier` is the group key, so all installments of one purchase are findable.
  - Pending (`status === 'pending'`): append `"[pending]"` to `description`. On a later sync, when the same `identifier` reappears as `completed`, dedup by `(account_id, date, identifier)` and update in place rather than inserting a duplicate.

#### `cal_api_notes` (Cal sidecar)

- Same shape + treatment as `bank_api_notes`. Bullet vocabulary: installment chains, FX merchants, first-time merchants, pending status, cross-source divergence (Cal total vs Hapoalim's monthly `card_payment` row).
- Hints, not ground truth.

## fx-conversions/ — internal Hafenix ILS↔USD conversions

### `*-fx-<from>-<to>-<src-amount>.<png|jpg>`

Screenshot from Hafenix's app showing a single ILS↔USD conversion happening **inside the brokerage** (ILS Hafenix → USD Hafenix or the reverse). The money is already at Hafenix at this point — these screenshots are **not** Hapoalim sub-account events. The Hafenix account-header number on the screenshot is a Hafenix sub-identifier, not a separate Hapoalim account.

The full money path looks like:

1. **ILS Hapoalim → ILS Hafenix** (the actual deposit; recorded from the `investment_deposits` XLSX with `category='deposit'` on the brokerage account, USD-denominated rows where each row's description embeds the ILS source and rate)
2. **ILS Hafenix → USD Hafenix** ← the FX screenshot captures this leg
3. **Buy stocks**, reducing Hafenix's USD cash; tracked via `trades` rows

- **Typical content (Hebrew labels):**
  - `ממטבע` / `למטבע` — source / destination currency
  - `סכום המרה` — amount being converted (in the source currency)
  - `מועד הבקשה` — request date (DD.MM.YYYY HH:MM)
  - `אסמכתא` — Hafenix reference number (use as `transactions.reference`)
  - `שער המרה` — FX rate applied
  - `סטטוס פעולה` — operation status (טופל = processed)
- **Maps to:**
  - `transactions` — one row on the brokerage account with `category='deposit'`, `currency='USD'`, amount = converted USD (`src / rate` rounded to 2dp), `counterparty='Excellence/Hafenix'`, `reference=<אסמכתא>`, `description='from <src> ILS @ <rate>'`. This matches the convention used by the `investment_deposits` XLSX, so the FX shows up correctly in cumulative-deposits tracking.
  - `fx_rates` — one row: `(date, USD, ILS, rate)` so the dashboard can value USD holdings on/around this date.
- **Do NOT insert** debit/credit pairs on Hapoalim sub-accounts. The FX is happening entirely inside Hafenix; Hapoalim's ledger has nothing to record on this date.
- **Sign of converted amount:** Hafenix rounds; if the screenshot only shows the source amount, compute the USD amount (`src / rate` or `src * rate` depending on direction) and proceed.

---

## Expense vs transfer classification

The renderer needs to filter `transactions` into "real expenses" (the user spent this on consumption) vs "transfers" (the money just moved between owned accounts or got reclassified elsewhere). The classification is purely a function of `category` + sign — no per-row judgment at render time. This table is the canonical source; if you add a new category during sync, decide its bucket here.

| Bucket          | Categories (negative-amount rows count as expenses unless excluded)                |
|-----------------|------------------------------------------------------------------------------------|
| **Expense**     | `other`, `restaurant`, `groceries`, `gas`, `software_services`, `shopping`, `gym`, `parking`, `medical`, `entertainment`, `p2p_bit`, `fee`, `card_fee`, `home`, `small_purchases`, `online_shopping`, `travel`, `government`, `expense`, `car_purchase`, `rent` |
| **Not expense** | `transfer`, `card_payment`, `savings_deposit`, `savings_withdrawal`, `securities_buy`, `check`, `withdrawal`, `fx`, `refund`, `interest`, `salary`, `deposit`, `dividend`, `income`, `bank_gift`, `bank_credit`, `pension_deposit`, `study_fund_deposit`, `government_payment` |

Notes:
- `card_payment` rows on the credit-card account are the monthly bill being paid by checking — internal transfer, not an expense. The merchant-level charges (which *are* expenses) live as separate negative rows on the same card account from the mastercard statement.
- `שיק` rows on the checking statement carry no payee — the user may pay recurring rent by sequential monthly checks near the same day each month. Before assigning `category='check'` to a new check row, query `SELECT id, date, amount_minor, reference, category FROM transactions WHERE category IN ('rent','check') ORDER BY date DESC LIMIT 6`: if the new row's reference is the next in sequence, its amount is in the established band, and its date sits near the established monthly cadence, categorize as `rent` (Expense bucket). Only fall back to `check` (Not-expense bucket) when the pattern clearly breaks (different amount band, different cadence, non-sequential reference).
- `p2p_bit` is in **Expense** because the sign filter is what excludes positive (inbound) Bit rows. See the mastercard section's "Bit rule".
- Adding a new category in sync: add it to one bucket here in the same commit.

## Brokerage sell screenshots (`brokerage_screenshot` for a sell event)

When the user adds a screenshot documenting a **sell** (not just a position snapshot), the realized-cash row inserted into `transactions` must already be **net of Israeli taxes and brokerage fees**:

1. **Capital-gains tax: 25%.** Subtract 25% of the realized gain (sell proceeds − cost basis of the lots being sold, FIFO). Apply this only to the gain portion, not to the principal returned.
2. **Brokerage fee: 5 USD.** Subtract a flat $5 from the proceeds. (Adjust if the screenshot or note states a different fee.)

Net realized cash = (sell_proceeds − fee) − 0.25 × max(0, gain). Insert as `transactions` on the brokerage account: `category='income'` (or `'realized_gain'` if a more specific bucket is preferred), positive amount in the security's currency, `counterparty=<ticker>`, `description='sell <shares>@<price>, net of 25% tax + $5 fee'`, `reference=<broker-confirmation-id>` if present.

The user typically writes a one-line note on the screenshot when adding it — read that note for the fee and the cost-basis lots if specified. If the cost basis isn't on the screenshot, derive it from earlier `trades` rows on the same ticker (FIFO). Don't apply the 25% tax to the *unrealized* portion of the same position — that adjustment lives in the renderer's stocks-income calculation, not in the ingested row.

The dashboard's Stocks-income figure assumes realized rows are already net (no re-tax), and applies the 25% haircut only to the unrealized MV − cost-basis residue.

## Judgment calls

These are situations where rule-based categorization gets things wrong. The skill should *think*, not pattern-match:

- **Ambiguous / no-counterparty outflows: reason from history, don't stamp the literal category.** A `שיק` (cheque) or any opaque debit with no `counterparty` must not be auto-tagged `check` and left. First ask "what was a charge like this in the past?" — query the account's own history for a matching signature (amount band, monthly cadence, sequential `reference`) and inherit the category those rows were given. The canonical case (the recurring rent cheque → `rent`) has its exact query + criteria in **Expense vs transfer classification** above. **This applies identically to rows arriving via `bank_api_dump`** (the Hapoalim API feed), not just XLSX/PDF statements — the source format never changes the judgment. A literal placeholder like `check` is the fallback *only* when history shows no match.
- **Transfers between your own accounts are not expenses.** Hapoalim → Excellence is `category='transfer'`. Hapoalim checking → Hapoalim sub-account is `category='transfer'`. The same money will show as a credit on the receiving account — both rows are correct, they're two sides of one event.
- **Family transfers may or may not be a gift.** "זיכוי מדיסקונט" from a relative might be a loan repayment, a gift, or shared expense settlement. Default to `category='transfer', counterparty=<name>` and let the user re-categorize manually if needed.
- **Salary credits** ("משכורת-נט") are `category='salary'` but they also have a corresponding payslip. Don't double-count: the payslip is the source of truth for gross/deductions; the bank credit is just the net flow.
- **Credit-card consolidation entries** ("כאל", "Cal", "ויזה") on a checking statement are not individual purchases — they're the monthly payment to the card. Categorize as `card_payment` (or `transfer`) on the checking account; the actual purchases live on the credit-card statement.
- **Dividends** on Hapoalim ("ני"ע-דיבידנד") might be tax-withheld already; record the gross when known, the net otherwise, and add a `tax` transaction if you can tell them apart.
- **Excel serial dates** vs ISO strings: the xlsx helper converts dates flagged in styles, but some files store dates as plain numbers without date formatting. If a column header is "Date" and the values look like 45000–50000, treat as Excel serial.
- **Currency code "ILA"** in Israeli broker exports denotes agorot — the minor unit of ILS, already integer. Do **not** multiply by 100 when inserting `price_minor`. Normalize to `currency='ILS'` and store the raw value (it's already agorot). Likewise London prices in "GBX"/"GBp" are already pence; "GBP" prices need × 100.
- **OCR uncertainty (JPG screenshots):** if a number is unclear, prefer leaving the row out over inserting a wrong value. Better to have the user re-screenshot than to corrupt the database.
