# Long-term savings — locked / long-term vehicles

> Part of the [doc-types catalogue](./README.md) — principles, archetypes, and the full index live there.

## long-term-savings/ — locked / long-term vehicles

### Pension statement (e.g. Harel)

- Cover page lists fund, status, holder, ID, employer, investment track, total accumulated.
- "תנועות אחרונות" table = recent deposits, broken down by (employee tagmul, employer tagmul, severance).
- "ערכי פדיון" row = balance per component.
- **Maps to:**
  - `accounts` — create once with kind='pension', record `investment_track`.
  - `balances` — 3 rows (one per component) for the as-of date.
  - `transactions` — one row per (deposit month, component); `category='pension_deposit'`, `counterparty=employer`.

### Training fund statement (e.g. Harel)

- Same shape as pension but with 2 components (no severance), and the table headers are slightly different.
- Note the **liquidity date** ("מועד נזילות") → `accounts.liquidity_date`.
- **Maps to:** same pattern as pension but 2-component.

### Pension movements report

- Movement-only report (no balance summary). Use for additional transaction rows; don't insert `balances` unless the report includes a snapshot.

### Short-term savings deposit screenshot (e.g. Hapoalim Sprint)

- Screenshot of a Hapoalim short-term savings deposit. Read with Read tool's image support.
- **Maps to:** `accounts` (kind='savings'), `balances`. Note maturity → `liquidity_date`.

### Fixed-deposit monthly statement (e.g. Hapoalim Sprint)

- Monthly PDF statement for a Hapoalim Digital Sprint fixed deposit. Cover shows opening balance, accrued interest, closing principal, projected re-evaluated balance at next monthly stop, and maturity date (`19.05.26 יחול מועד פרעון הפקדון`).
- The "תחנה" row is a snapshot at the monthly stop — the re-evaluated balance equals principal + accumulated interest at that stop.
- **Maps to:**
  - `balances` on the Sprint account: `as_of = stop date`, `amount_minor = re-evaluated balance`.
  - Don't insert separate interest transactions — interest is implicit in the balance growth and is realized only on redemption.
- On maturity, the principal + interest is credited to the linked checking account as `פרעון פקדון`; record that as a `transfer` (or `savings_withdrawal`) on the checking side, and a closing balance of 0 on the Sprint side.

### Bank deposit / savings notice (e.g. Hapoalim)

- Hapoalim deposit/savings notices for non-Sprint or legacy products. Common events: opening/renewal, monthly standing-order notice, and termination.
- **Maps to:** `accounts` (`kind='savings'`) when the product is material enough to affect net worth; `balances` for explicit or inferable point-in-time deposit value; `transactions` for redemption, tax, or transfer legs when they are not already represented by a checking statement.
- Small historical notices that only confirm already-ingested checking rows may create only a `documents` row with explanatory `notes`.
- For termination notices, store the last pre-redemption balance as principal plus accrued interest before tax, then a zero balance on the redemption date. If the notice shows withholding tax, insert a `tax` transaction on the savings account so the redemption drop is explainable.

### Bank securities notices (e.g. Hapoalim capital-market)

- Hapoalim bank-side securities trade/gift notices, usually for the internal capital-market account linked to checking.
- If the same trade and cash legs are already captured by a statement or a balance screenshot, treat the notice as audit evidence and avoid double-inserting trades. Otherwise, insert a `trade` on the Hapoalim capital-market account and the matching checking cash leg when a reliable account mapping exists.

### Bank FX notices (e.g. Hapoalim)

- Bank-side foreign-currency purchase/sale notices for the checking account, distinct from your brokerage's internal FX screenshots.
- Insert document-derived `fx_rates` only when the notice states the actual conversion rate. Insert checking-account transactions only when the statement side is not already present and the cash impact is unambiguous.
