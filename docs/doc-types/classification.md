# Classification — expense vs transfer & judgment calls

> Part of the [doc-types catalogue](./README.md) — principles, folder routing, and the full index live there.

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

## Judgment calls

These are situations where rule-based categorization gets things wrong. The skill should *think*, not pattern-match:

- **Ambiguous / no-counterparty outflows: reason from history, don't stamp the literal category.** A `שיק` (cheque) or any opaque debit with no `counterparty` must not be auto-tagged `check` and left. First ask "what was a charge like this in the past?" — query the account's own history for a matching signature (amount band, monthly cadence, sequential `reference`) and inherit the category those rows were given. The canonical case (the recurring rent cheque → `rent`) has its exact query + criteria in **Expense vs transfer classification** above. **This applies identically to rows arriving via `bank_api_dump`** (the Hapoalim API feed), not just XLSX/PDF statements — the source format never changes the judgment. A literal placeholder like `check` is the fallback *only* when history shows no match.
- **Transfers between your own accounts are not expenses.** Hapoalim → your brokerage is `category='transfer'`. Hapoalim checking → Hapoalim sub-account is `category='transfer'`. The same money will show as a credit on the receiving account — both rows are correct, they're two sides of one event.
- **Family transfers may or may not be a gift.** "זיכוי מדיסקונט" from a relative might be a loan repayment, a gift, or shared expense settlement. Default to `category='transfer', counterparty=<name>` and let the user re-categorize manually if needed.
- **Salary credits** ("משכורת-נט") are `category='salary'` but they also have a corresponding payslip. Don't double-count: the payslip is the source of truth for gross/deductions; the bank credit is just the net flow.
- **Credit-card consolidation entries** ("כאל", "Cal", "ויזה") on a checking statement are not individual purchases — they're the monthly payment to the card. Categorize as `card_payment` (or `transfer`) on the checking account; the actual purchases live on the credit-card statement.
- **Dividends** on Hapoalim ("ני"ע-דיבידנד") might be tax-withheld already; record the gross when known, the net otherwise, and add a `tax` transaction if you can tell them apart.
- **Excel serial dates** vs ISO strings: the xlsx helper converts dates flagged in styles, but some files store dates as plain numbers without date formatting. If a column header is "Date" and the values look like 45000–50000, treat as Excel serial.
- **Currency code "ILA"** in Israeli broker exports denotes agorot — the minor unit of ILS, already integer. Do **not** multiply by 100 when inserting `price_minor`. Normalize to `currency='ILS'` and store the raw value (it's already agorot). Likewise London prices in "GBX"/"GBp" are already pence; "GBP" prices need × 100.
- **OCR uncertainty (JPG screenshots):** if a number is unclear, prefer leaving the row out over inserting a wrong value. Better to have the user re-screenshot than to corrupt the database.
