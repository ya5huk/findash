# Classification — expense vs transfer & judgment calls

> Part of the [doc-types catalogue](./README.md) — principles, archetypes, and the full index live there.

## Accounting class vs display category

Every new transaction gets two independent labels:

- `flow_type` is the closed accounting dimension used by calculations: `expense | income | transfer | investment | tax | refund | other`.
- `category` is open vocabulary for human detail: `groceries`, `salary`, `card_payment`, `rent`, and so on.

Choose `flow_type` with judgment during ingestion. Rendering never decides that an unknown negative category “must be an expense.” Existing rows with `flow_type=NULL` use the compatibility mapping in `scripts/findash_core/categories.py`; that mapping is migration support, not the design for new data.

| Meaning | `flow_type` | Common categories |
|---|---|---|
| Consumption or service cost | `expense` | `groceries`, `rent`, `fee`, `travel` |
| Earned/received value | `income` | `salary`, `interest`, recorded `dividend` |
| Movement between owned accounts | `transfer` | `card_payment`, `savings_deposit`, brokerage funding |
| Trade-related cash/evidence | `investment` | `securities_buy`, `securities_sell`, `realized_gain` |
| Posted tax | `tax` | `income_tax`, `dividend_tax`, `capital_gains_tax` |
| Reversal of prior spend | `refund` | `refund`, merchant reversal |
| Non-income external receipt or unresolved cash flow | `other` | loan principal returned, neutral receipt |

Notes:
- `card_payment` is an internal transfer, not an expense. Merchant-level card charges are separate expense events.
- `שיק` rows on checking carry no payee. Use amount band, cadence and sequential references to identify a recurring rent cheque (`category='rent', flow_type='expense'`). If evidence remains unclear, keep a neutral category with `flow_type='other'` and flag it rather than declaring every cheque non-expense.
- A P2P provider name does not determine `flow_type`: outbound spending, an owned-account transfer and inbound reimbursement have different meanings.
- A new display category needs no renderer edit; its `flow_type` carries the accounting meaning.

## Judgment calls

These are situations where rule-based categorization gets things wrong. The skill should *think*, not pattern-match:

- **Ambiguous / no-counterparty outflows: reason from history, don't stamp the literal category.** A cheque or opaque debit must not be auto-tagged and forgotten. Compare the account's own history for a matching signature—amount band, cadence and sequential reference—and inherit meaning only when the evidence is clear. This applies identically to API and statement observations; source format never changes the economics.
- **Transfers between your own accounts are not expenses.** Bank → brokerage and checking → savings use `flow_type='transfer'`. The two account legs remain separate canonical events; consolidated expense/income counts neither.
- **FX inside one brokerage is not external funding.** Its negative source-currency
  and positive destination-currency cash legs use `flow_type='investment'` and
  `category='fx'`; the earlier bank→brokerage contribution is the transfer.
- **Person-to-person transfers need their economic meaning.** Returned loan principal and other non-income receipts use `flow_type='other'`; reimbursements use `flow_type='refund'`; gifts use `flow_type='income'`. Ask when the evidence cannot distinguish them. The cash-flow dashboard groups positive `other` and `refund` events under Other receipts, while owned-account transfers remain excluded.
- **Salary credits** use `category='salary', flow_type='income'`, but may correspond to a payslip. Cash-flow job income reconciles observations one-to-one only when the payslip has an explicit `paid_on` and its posting date, native currency, and exact native net amount equal a salary credit. Each exact mirror counts once; unmatched second employers, bonuses, and bank credits remain additive. There is no date window, amount tolerance, or employer-name guessing. Gross/deduction analysis remains payslip-only.
- **Credit-card consolidation entries** on checking are `category='card_payment', flow_type='transfer'`; merchant charges on the card are `flow_type='expense'`.
- **Dividend signals are semantic, not provider strings.** Providers may describe
  the same economic event as a dividend, distribution, income payment, corporate
  action, or a localized equivalent. Use the account context, positive cash
  movement, linked security or issuer, payment or entitlement date, currency,
  and any paired withholding row to decide what the source proves. Normalize a
  confirmed payment to `category='dividend', flow_type='income'`; never hardcode
  one institution's wording. Record gross when the source separates payment and
  withholding, net when that is all it proves, and retain a distinct posted tax
  debit as `flow_type='tax'`.
- **Excel serial dates** vs ISO strings: the xlsx helper converts dates flagged in styles, but some files store dates as plain numbers without date formatting. If a column header is "Date" and the values look like 45000–50000, treat as Excel serial.
- **Currency code "ILA"** in Israeli broker exports denotes agorot — the minor unit of ILS, already integer. Do **not** multiply by 100 when inserting `price_minor`. Normalize to `currency='ILS'` and store the raw value (it's already agorot). Likewise London prices in "GBX"/"GBp" are already pence; "GBP" prices need × 100.
- **OCR uncertainty (JPG screenshots):** if a number is unclear, prefer leaving the row out over inserting a wrong value. Better to have the user re-screenshot than to corrupt the database.
