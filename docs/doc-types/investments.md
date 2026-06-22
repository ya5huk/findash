# Investments — brokerage exports & screenshots

> Part of the [doc-types catalogue](./README.md) — principles, folder routing, and the full index live there.

## investments/ — brokerage exports & screenshots

Brokerage exports & screenshots describe trades, positions, and account state. The examples below use one Israeli broker's formats — adapt field names/titles to your broker.

### `*-stock-events-transactions-*.xlsx` — full trade history

- **Columns:** `Symbol, Date, Quantity (signed), Price, Price Currency, Fees Percentage, Fees Amount, Fees Currency`
- **Maps to** `trades`: one row per non-zero Quantity row. `side='buy'` if Quantity > 0 else `'sell'`. `shares = abs(Quantity)`.
- **Securities** need a row in `securities` first — create on first sighting; `asset_class='stock'` unless obviously an ETF.

### `*-investment-deposits-*.xlsx` — cash deposits into brokerage

- **Maps to** `transactions` on the **brokerage account**: `category='deposit'`, positive amount. Store the ILS source + rate in `description` as `from <ILS> ILS @ <rate>`.
- The same money usually shows up as an outflow on a Hapoalim checking statement (counterparty: `<your brokerage>`). Both rows should exist — they're not duplicates, they're the two sides of a transfer. The `reference` from the bank side helps tie them together.
- **Date disambiguation — the DD/MM↔MM/DD gotcha (do this every ingest; see Principle 6 in the [README](./README.md)).** The Date column is Israeli DD/MM/YYYY, but Excel may have stored an *ambiguous* cell (day ≤ 12, so the day could pass as a month) under a MM/DD reading — e.g. `07/04` (7 Apr) saved as 4 Jul. Cells with day > 12 are self-resolving. `xlsx_to_rows.py` lists every such cell in its `ambiguous_dates` output as `{cell, value, swapped}`. For each, pick `value` vs `swapped` by these cross-checks: (1) **FX match** — compare the row's `$ price` to `fx_rates` on both candidate dates; the recorded rate runs a small spread *above* mid, so the closer candidate (after allowing ~0.0–0.06) wins, and a tight match is decisive; (2) **cumulative `Total ($)` / row order** — the sheet is strictly chronological top→bottom, so the chosen dates must keep deposits monotonic in time.
- **Do not seed `fx_rates` from this sheet.** The `$ price` is the transacted rate (carries a spread) and lives only in the transaction `description`. Valuation FX comes from Yahoo; the authoritative in-brokerage conversion rate is captured by the `fx_conversion` screenshot (which *does* write `fx_rates`, `source='document'`), not here. Seeding a spread-laden rate — worse, on a mis-parsed date — silently corrupts USD valuations.

### `*-net-worth-*.xlsx` — brokerage account value snapshot

- **Maps to** `balances` on the brokerage account: `component=NULL`, `as_of = doc_date`, `amount_minor = total value`.

### `*-stock-market-operations-*.xlsx` (`manual_trading_journal`)

- User-maintained trading journal: one row per closed buy/sell pair with prices, fees, P/L, and free-form "Loss/Win Explanation" prose. The same trades are already captured in `trades` from the brokerage's `trade_history` export, so this file's structured data is redundant. The prose isn't normalizable. **Skip extraction** unless the user explicitly wants the reflections preserved.

### `*-net-worth-*.xlsx` (`manual_networth_log`)

- User-maintained monthly net-worth journal. Each entry is a **section** delimited by a date in column A — never by row count. **Walk header-to-header**, not row-by-row: locate every Excel row whose column A is either a date serial (numeric cell with a date style, e.g. `46026.0`) or a `DD/MM/YYYY` text cell; the section runs from one date row up to (but not including) the next date row. Don't use the parser's flat row list — use the raw `xl/sharedStrings.xml` + `xl/worksheets/sheet1.xml` if needed.
- Layouts vary between sheets — handle both:
  - **Compact:** ~3 Excel rows per entry — `[date | detail in C/D/E with `\n`-separated `Label:\nValue` pairs] / [summary B-K] / [blank]`.
  - **Expanded:** 5–8 Excel rows per entry — labels and values are on *separate* rows (col C row N = a label like `'Poalim:'`, col C row N+1 = `'<amount>₪'`); the summary row sits at the bottom of the section. The parser's flat rows-list mangles this; section walking recovers it.
- When a section has no detail breakdown (only a summary `C` total), use the summary value and note the imprecision in `documents.notes` — older entries may include small Paypal amounts folded into the Cash total.
- **Maps to** `balances` (`source_doc_id` = this doc):
  - Cash → primary Hapoalim checking account, `component=NULL`.
  - Locked-Poalim sub-amount → Hapoalim Digital Sprint savings account, `component=NULL`. Only valid for dates ≥ the Sprint open date recorded in the DB.
  - **Skip Pension and Training Fund** rows — the log gives single totals but the DB requires component breakdown (`tagmul_employee` / `tagmul_employer` / `pitsuyim`). Guessing the split would corrupt the chart.
  - **Skip brokerage equity totals** — the dashboard computes brokerage value from `trades` × historical prices; don't double-source. Brokerage **cash** is tracked separately, ideally via a brokerage balance screenshot (see `brokerage_screenshot — brokerage balance screenshot` below). If a `manual_networth_log` entry explicitly breaks out brokerage cash by currency, insert those as `balances` rows with `component='cash_usd' | 'cash_gbp' | 'cash_ils'` (screenshot is still the preferred source).
- **Date hygiene — the Excel-locale gotcha** (see Principle 6 in the [README](./README.md)). Entries are typed in Israeli DD/MM/YYYY. Cells stored as text (e.g. `14/02/2026`) come through verbatim; cells Excel auto-converted to date serials come through ISO (`2026-02-14`). When the day is >12, the DD/MM interpretation is the only legal one, so the date is recoverable either way. **When day and month are both ≤ 12, the auto-converted ISO output may be wrong**: e.g. `2025-04-12` is really `2025-12-04` and `2025-04-10` is really `2025-10-04`. Resolve ambiguous cases using these checks, in order of decisiveness:
  1. **FX-rate column (most decisive).** Each entry's row has the `1$` and `1£` cells (cols 9, 10). Look up the actual USD/ILS rate in `fx_rates` for each candidate date — the one matching the typed rate within a few mils is the right date. Tight match (Δ ≤ 0.01) is conclusive; mismatch (Δ > 0.05) is conclusive in the other direction.
  2. **Cross-doc references in `Income since last entry` / `Notes` columns.** A "+<net salary> job" entry must postdate the matching payslip; a "now <total> USD were ever transferred" cumulative tally pins the date between the deposit that reached that total and the next deposit.
  3. **Sprint balance monotonicity.** An interest-bearing savings balance increases over time absent withdrawals, so the Sprint balance is strictly increasing. Any candidate date that produces a Sprint balance smaller than a later balance is wrong.
  4. **Sprint open date.** Use the Sprint (savings) account's `opened_on` from the `accounts` table; any candidate date before it is impossible.
- **Dedup:** use `INSERT OR IGNORE` against `UNIQUE(account_id, as_of, component)`. The log's entries should not overlap with official statement closing balances; if they do (e.g. a manual entry on the same day as a Hapoalim XLSX closing), prefer the statement.

### `*-bank-hapoalim-capital-market-*.jpg` — Hapoalim brokerage screenshot

- Read with Read tool's image support. Transcribe positions and/or total value. Treat as a position snapshot if listing securities, or a `balances` row if just a total.

### Brokerage balance screenshot (`brokerage_screenshot` for brokerage cash)

- Screenshot from your brokerage's app showing the account's cash holdings, typically broken out per currency (USD, GBP, ILS). May also include a positions list — those are informational only; positions are tracked via `trades`.
- **Maps to** `balances` on the brokerage account, one row per non-zero currency:
  - `as_of = <screenshot date>`
  - `component = 'cash_usd' | 'cash_gbp' | 'cash_ils'` (lowercase ISO currency)
  - `currency = 'USD' | 'GBP' | 'ILS'`
  - `amount_minor = <amount in minor units of that currency>` (cents/pence/agorot)
  - `source_doc_id = <doc>`
- **Dedup:** `INSERT OR IGNORE` on `UNIQUE(account_id, as_of, component)`. Multiple currencies on the same date insert as separate rows because `component` differs.
- **Do not** write a `component=NULL` row for a trade-fed brokerage account — it would conflict with the per-currency cash rows. (Brokerage equity is computed from `trades`, never stored as a total balance.)
- Skip a currency component if its amount is zero (keeps the chart clean).
- This is the **canonical** path for tracking brokerage cash. The renderer combines these snapshots with post-snapshot flows (deposits, FX conversions, sells, and buy cash-legs derived from `trades`) so the displayed balance stays accurate between screenshots.

### Brokerage periodic statement (`brokerage_periodic_statement`)

- Multi-page PDF generated by your brokerage. One Israeli broker's periodic statement is titled **דוח תקופתי**, has a brokerage account header, and carries a heading that says **הננו מתכבדים להציג את מצב חשבונך אצלנו נכון לתאריך: DD/MM/YYYY** — use these as format hints to recognize the doc type; adapt to your broker. Typically one per calendar month, cutoff = month-end.
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
- **EXCEPT: `הפ/דיב` (dividend) rows are extracted as confirmations.** Sync (step 5b) autonomously fetches live Yahoo dividend events every run and inserts them as synthetic `transactions` (`source_doc_id` points to a `doc_type='yahoo_dividend_estimate'` document) so brokerage cash stays accurate between snapshots. When a periodic statement is ingested:
  - For each `הפ/דיב` row in פירוט תנועות, parse `(pay_date, ticker, net amount as the statement shows it — already post-withholding)`.
  - Delete any matching synthetic transaction on the brokerage account: `counterparty=ticker, ABS(julianday(date) - julianday(row_date)) <= 5, source doc has doc_type='yahoo_dividend_estimate'`.
  - Insert the real row referencing the statement's `documents.id` — now `source` is a real brokerage doc, the dashboard tags it "confirmed".
  - Then run the **snapshot-supersession cleanup** described in `skills/sync-finance-data/SKILL.md` step 5b: drop remaining synthetic dividend transactions on the brokerage account dated `<= cutoff_date`, since the new snapshot's cash component already includes them.
- `מס/דיב` (withholding tax) rows are informational only — the synthetic + statement amounts are already net of withholding, so don't insert these as separate transactions.
- **The total portfolio value** (סה"כ row) is informational — it equals positions + cash and the dashboard already computes brokerage equity from `trades` × historical prices. **Do not** insert it as a `component=NULL` balance row; the brokerage account uses per-currency cash components only.

### `*-stock-trade-confirmation-*.jpg` — single trade

- One row in `trades`. Read with Read tool's image support.

## Brokerage sell screenshots (`brokerage_screenshot` for a sell event)

When the user adds a screenshot documenting a **sell** (not just a position snapshot), the realized-cash row inserted into `transactions` must already be **net of Israeli taxes and brokerage fees**:

1. **Capital-gains tax: 25%.** Subtract 25% of the realized gain (sell proceeds − cost basis of the lots being sold, FIFO). Apply this only to the gain portion, not to the principal returned.
2. **Brokerage fee: 5 USD.** Subtract a flat $5 from the proceeds. (Adjust if the screenshot or note states a different fee.)

Net realized cash = (sell_proceeds − fee) − 0.25 × max(0, gain). Insert as `transactions` on the brokerage account: `category='income'` (or `'realized_gain'` if a more specific bucket is preferred), positive amount in the security's currency, `counterparty=<ticker>`, `description='sell <shares>@<price>, net of 25% tax + $5 fee'`, `reference=<broker-confirmation-id>` if present.

The user typically writes a one-line note on the screenshot when adding it — read that note for the fee and the cost-basis lots if specified. If the cost basis isn't on the screenshot, derive it from earlier `trades` rows on the same ticker (FIFO). Don't apply the 25% tax to the *unrealized* portion of the same position — that adjustment lives in the renderer's stocks-income calculation, not in the ingested row.

The dashboard's Stocks-income figure assumes realized rows are already net (no re-tax), and applies the 25% haircut only to the unrealized MV − cost-basis residue.
