# Investments — brokerage exports & screenshots

> Part of the [doc-types catalogue](./README.md) — principles, archetypes, and the full index live there.

## investments/ — brokerage exports & screenshots

Brokerage exports & screenshots describe trades, positions, and account state. The examples below use one Israeli broker's formats — adapt field names/titles to your broker.

### Full trade-history export

- **Columns:** `Symbol, Date, Quantity (signed), Price, Price Currency, Fees Percentage, Fees Amount, Fees Currency`
- **Maps to** `trades`: one row per non-zero Quantity row. `side='buy'` if Quantity > 0 else `'sell'`. `shares = abs(Quantity)`.
- **Securities** need a row in `securities` first — create on first sighting; `asset_class='stock'` unless obviously an ETF.

### Cash deposits into the brokerage

- **Maps to** a raw transaction/source event plus one confirmed canonical cash
  event on the **brokerage account**: `category='deposit'`,
  `flow_type='transfer'`, positive amount. This is external funding of the
  investment portfolio; preserve any stated source amount/rate in private source
  evidence rather than relying on prose for calculation.
- The same money usually shows up as an outflow on a Hapoalim checking statement (counterparty: `<your brokerage>`). Both rows should exist — they're not duplicates, they're the two sides of a transfer. The `reference` from the bank side helps tie them together.
- Funding changes external capital and therefore the cumulative contribution and
  performance benchmark. A later internal ILS→USD conversion is a separate FX
  purchase and must not change either statistic.
- **Date disambiguation — the DD/MM↔MM/DD gotcha (do this every ingest; see Principle 6 in the [README](./README.md)).** The Date column is Israeli DD/MM/YYYY, but Excel may have stored an *ambiguous* cell (day ≤ 12, so the day could pass as a month) under a MM/DD reading — e.g. `07/04` (7 Apr) saved as 4 Jul. Cells with day > 12 are self-resolving. `xlsx_to_rows.py` lists every such cell in its `ambiguous_dates` output as `{cell, value, swapped}`. For each, pick `value` vs `swapped` by these cross-checks: (1) **FX match** — compare the row's `$ price` to `fx_rates` on both candidate dates; the recorded rate runs a small spread *above* mid, so the closer candidate (after allowing ~0.0–0.06) wins, and a tight match is decisive; (2) **cumulative `Total ($)` / row order** — the sheet is strictly chronological top→bottom, so the chosen dates must keep deposits monotonic in time.
- **Do not seed `fx_rates` from this sheet.** The `$ price` is the transacted rate (carries a spread) and lives only in the transaction `description`. Valuation FX comes from Yahoo; the authoritative in-brokerage conversion rate is captured by the `fx_conversion` screenshot (which *does* write `fx_rates`, `source='document'`), not here. Seeding a spread-laden rate — worse, on a mis-parsed date — silently corrupts USD valuations.

### Brokerage value snapshot (simple total)

- **Maps to** `balances` on the brokerage account: `component=NULL`, `as_of = doc_date`, `amount_minor = total value`.

### Manual trading journal

- User-maintained trading journal: one row per closed buy/sell pair with prices, fees, P/L, and free-form "Loss/Win Explanation" prose. The same trades are already captured in `trades` from the brokerage's trade-history export, so this file's structured data is redundant. The prose isn't normalizable. **Skip extraction** unless the user explicitly wants the reflections preserved.

### Manual net-worth log

- User-maintained monthly net-worth journal. Each entry is a **section** delimited by a date in column A — never by row count. **Walk header-to-header**, not row-by-row: locate every Excel row whose column A is either a date serial (numeric cell with a date style, e.g. `46026.0`) or a `DD/MM/YYYY` text cell; the section runs from one date row up to (but not including) the next date row. Don't use the parser's flat row list — use the raw `xl/sharedStrings.xml` + `xl/worksheets/sheet1.xml` if needed.
- Layouts vary between sheets — handle both:
  - **Compact:** ~3 Excel rows per entry — `[date | detail in C/D/E with `\n`-separated `Label:\nValue` pairs] / [summary B-K] / [blank]`.
  - **Expanded:** 5–8 Excel rows per entry — labels and values are on *separate* rows (col C row N = a label like `'Poalim:'`, col C row N+1 = `'<amount>₪'`); the summary row sits at the bottom of the section. The parser's flat rows-list mangles this; section walking recovers it.
- When a section has no detail breakdown (only a summary `C` total), use the summary value and note the imprecision in `documents.notes` — older entries may include small Paypal amounts folded into the Cash total.
- **Maps to** `balances` (`source_doc_id` = this doc):
  - Cash → the logical Hapoalim checking account that owns the entry on that
    date, `component=NULL`. A bank-initiated account-number migration with
    non-overlapping activity and a matching closing/opening transfer is one
    economic account with separate `account_feeds`, not two balances to sum.
    For genuinely separate predecessor/successor accounts, set `opened_on` and
    `closed_on` before importing snapshots and map each section within those
    boundaries. Never assign the entire journal to whichever account is primary
    today.
  - Locked-Poalim sub-amount → Hapoalim Digital Sprint savings account, `component=NULL`. Only valid for dates ≥ the Sprint open date recorded in the DB.
  - **Pension and Training Fund totals:** retain a reliable dated single total as
    `component=NULL`; never invent a component split. A later coherent component
    snapshot (all three pension components or both training-fund components on
    one date) supersedes an older total. A partial breakdown does not.
  - **Brokerage equity total:** retain a dated total as a brokerage `balances` row with `component=NULL` when reliable detailed positions plus cash are unavailable for that date. It is a coarse whole-account anchor, especially useful before trade history begins. Never add it beside complete same-day position/cash anchors: if the journal provides only a cash breakdown but no holdings, prefer the whole-account total; if complete detailed state exists, keep the total as source/reconciliation evidence rather than another balance. Later posted cash movements and known trade fees advance a coarse anchor, while a later explicitly complete positions-and-cash snapshot supersedes it.
- **Date hygiene — the Excel-locale gotcha** (see Principle 6 in the [README](./README.md)). Entries are typed in Israeli DD/MM/YYYY. Cells stored as text (e.g. `14/02/2026`) come through verbatim; cells Excel auto-converted to date serials come through ISO (`2026-02-14`). When the day is >12, the DD/MM interpretation is the only legal one, so the date is recoverable either way. **When day and month are both ≤ 12, the auto-converted ISO output may be wrong**: e.g. `2025-04-12` is really `2025-12-04` and `2025-04-10` is really `2025-10-04`. Resolve ambiguous cases using these checks, in order of decisiveness:
  1. **FX-rate column (most decisive).** Each entry's row has the `1$` and `1£` cells (cols 9, 10). Look up the actual USD/ILS rate in `fx_rates` for each candidate date — the one matching the typed rate within a few mils is the right date. Tight match (Δ ≤ 0.01) is conclusive; mismatch (Δ > 0.05) is conclusive in the other direction.
  2. **Cross-doc references in `Income since last entry` / `Notes` columns.** A "+<net salary> job" entry must postdate the matching payslip; a "now <total> USD were ever transferred" cumulative tally pins the date between the deposit that reached that total and the next deposit.
  3. **Sprint balance monotonicity.** An interest-bearing savings balance increases over time absent withdrawals, so the Sprint balance is strictly increasing. Any candidate date that produces a Sprint balance smaller than a later balance is wrong.
  4. **Sprint open date.** Use the Sprint (savings) account's `opened_on` from the `accounts` table; any candidate date before it is impossible.
- **Dedup:** use `INSERT OR IGNORE` against `UNIQUE(account_id, as_of, component)`. The log's entries should not overlap with official statement closing balances; if they do (e.g. a manual entry on the same day as a Hapoalim XLSX closing), prefer the statement.

### Bank brokerage screenshot (e.g. Hapoalim capital-market)

- Read with Read tool's image support. Transcribe positions and/or total value. Treat as a position snapshot if listing securities, or a `balances` row if just a total.

### Brokerage balance screenshot

- Screenshot from a brokerage app showing cash and possibly positions, typically broken out per currency. Both are useful snapshot anchors when event history is incomplete.
- **Maps to** `balances` on the brokerage account, one row per reported currency:
  - `as_of = <screenshot date>`
  - `component = 'cash_usd' | 'cash_gbp' | 'cash_ils'` (lowercase ISO currency)
  - `currency = 'USD' | 'GBP' | 'ILS'`
  - `amount_minor = <amount in minor units of that currency>` (cents/pence/agorot)
  - `source_doc_id = <doc>`
- Keep the source observation in `balance_evidence` and select one canonical anchor per `(account, as_of, component)`. Multiple currencies are separate components. Another feed reporting the same snapshot corroborates or conflicts; never sum it.
- Do not write a `component=NULL` total alongside per-currency brokerage cash; the total would overlap positions + cash rather than add a new asset.
- A clearly complete cash breakdown writes an explicit zero anchor for every
  previously observed currency that is shown as zero or omitted. That closes the
  older component instead of carrying stale cash forward. A cropped/partial view
  writes only currencies it actually shows; omission from a partial source is
  never zero.
- The accounting layer starts from this anchor and applies confirmed canonical events after its date. A later snapshot reconciles the derived state.

### Brokerage periodic statement

- Multi-page PDF generated by your brokerage. One Israeli broker's periodic statement is titled **דוח תקופתי**, has a brokerage account header, and carries a heading that says **הננו מתכבדים להציג את מצב חשבונך אצלנו נכון לתאריך: DD/MM/YYYY** — use these as format hints to recognize the doc type; adapt to your broker. Typically one per calendar month, cutoff = month-end.
- **Two main sections:**
  - **פירוט יתרות** (Holdings detail) — every position held on the cutoff date. One broker's column layout, left-to-right in RTL output (yours will differ — read the headers): `מספר נייר` (security #), `שם נייר` (name), `כמות` (quantity), `שער נוכחי` (current price in *agorot* — divide by 100 for ILS), `עלות הרכישה` (cost basis in ILS), `שווי נייר בשקלים` (market value in ILS), `אחוז מהתיק` (% of portfolio).
  - **Transactions detail** — every transaction during the report period: trades, cash distributions, withholding tax, commissions, FX conversions, deposits and withdrawals. Section names and transaction codes vary by provider and locale; examples such as `פירוט תנועות`, `הפ/דיב`, and `מס/דיב` are format hints only, never detection rules.
- **Maps to:**
  - **`balances` — month-end cash snapshot** (one row per reported currency,
    plus explicit zero anchors for previously observed currencies omitted by a
    clearly complete cash section; dedup via
    `UNIQUE(account_id, as_of, component)`):
    - **USD cash** (one broker's security codes — find the equivalents on yours): quantity of security `99028` (דולר ארה"ב). If a `99218 התחייבות דולרית` (USD liability) row is also present on the same statement, subtract its quantity from 99028's quantity to get net USD cash — the liability is a pending-settlement debit, not separately tracked.
    - **GBP cash:** quantity of security `99069` (לישט). Preserve an explicit
      zero when this complete statement closes an older GBP component.
    - **ILS cash:** value (in ILS) of the row labeled `יתרה כספית` or `יתרה פח"ק בבנק` (residual cash). Preserve zero; it may also be negative on rare days (slight overdraft), which is stored as-is.
  - **`fx_rates`:**
    - `(date=cutoff, base='USD', quote='ILS', rate = 99028.שער_נוכחי / 100)`.
    - `(date=cutoff, base='GBP', quote='ILS', rate = 99069.שער_נוכחי / 100)` if GBP held.
  - **`documents` — one row** per PDF (`doc_type` = a descriptive label like `brokerage-periodic-statement`), `doc_date = cutoff`. Skip if `drive_id` already ingested.
- **Retain transaction evidence without double-counting it.** Trades, deposits and FX conversions may already appear through another feed. Give each statement row a source identity and link clear matches to existing canonical events; never skip/delete raw evidence merely because another source probably reported it. Equal amount/date alone is not enough to merge.
- **Relate posted trade cash explicitly.** A canonical trade normally derives its brokerage-cash effect. If the statement also has a distinct posted settlement-cash row, retain its raw/canonical cash event and add `trade_cash_links` to every canonical fill the row covers only after account, currency, full economics, dates, fees, source ids and aggregation shape reconcile. One cash event may cover several fills, but one trade cannot be split across several canonical cash events; consolidate split source evidence into one reviewed canonical settlement or leave it unlinked. This supports aggregate settlement rows without counting trade cash twice. Never infer the relationship from amount/date alone; uncertain cash stays `needs_review` and unlinked.
- **Complete vs partial account snapshots:** a full holdings section may close a previously held but omitted security with an explicit zero-quantity `positions`/`position_evidence` anchor. Set `account_snapshot_complete=1` on that read's `position_evidence` and `balance_evidence` rows only when the same source also gives a complete cash breakdown, including explicit zeros for omitted prior currencies. A complete account with no positions carries the marker on its cash evidence; never create a fake security. This marker is what allows detailed rows to supersede an older `component=NULL` whole-account total. Complete holdings with incomplete cash, a screenshot, clipped page, filtered export, or unclear table leaves the flag at `0`: insert only visible facts and never turn omission into zero.
- **Dividends require an actual cash observation.** Identify the economic event
  from the full row and account context, regardless of provider wording. Parse
  the payment date, security or issuer, currency, and amount exactly as reported;
  insert a raw transaction with `category='dividend', flow_type='income'` and
  create or support one canonical cash event. A modeled distribution date and a
  matching amount strengthen reconciliation but do not become transaction
  evidence by themselves.
- Treat withholding according to the statement shape, regardless of its label:
  if the dividend row is already net and a tax row is explanatory only, do not
  subtract it again. If tax is a separate posted cash debit, retain it as
  `flow_type='tax'` and link its own canonical cash event.
- **The total portfolio value** (סה"כ row) is reconciliation evidence when complete positions plus cash are stored for the same date, so do not add it as another balance beside them. If the detailed sections are absent or unusable, retain the dated total as a `component=NULL` whole-account brokerage balance instead of losing the only supported historical anchor.

### Single trade confirmation

- One raw `trades` row plus its source/canonical event evidence. Read with Read tool's image support. Do not invent a separate cash row unless the source actually reports settlement cash; if it does, retain and explicitly link that cash event as described above.

## Brokerage sell screenshots

When a screenshot documents a **sell**, record what the source proves: the raw trade (quantity, price, fee, currency and date) and its canonical trade event. Do not invent a 25% tax or insert full sale proceeds as income. Trade settlement is derived from the canonical trade once; principal returned is not income.

If a statement separately reports an actual tax debit or fee, retain that cash observation with `flow_type='tax'` or `flow_type='expense'`. Cost basis and realized gain are deterministic outputs of the chosen lot method; they are not additional cash transactions. If acquisition history is incomplete, report cost basis as unavailable before the relevant anchor instead of guessing.
