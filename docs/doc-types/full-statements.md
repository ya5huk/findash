# Full statements — checking & credit card

> Part of the [doc-types catalogue](./README.md) — principles, archetypes, and the full index live there.

## full-statements/ — checking & credit card

### Bank app screenshot

- Mobile/web app screenshot of a Hapoalim checking account showing current balance + recent transactions. Use when the user wants to capture mid-month state before the official XLSX/PDF statement is available.
- Read with Read tool's image support; transcribe each transaction row.
- **Maps to:**
  - `transactions` on the checking account — one row per visible row not already in DB. **Always cross-check `(account_id, date, amount, reference)`** against existing rows to avoid duplicating txns from a later monthly statement.
  - `balances` — one row at the snapshot timestamp using the prominent header balance.
- **Note:** screenshots may show only partial transactions (e.g. last 10). Don't assume completeness — when the official statement arrives later, additional txns may surface.


### Bank checking statement (e.g. Hapoalim)

- Hapoalim checking account statement. Columns (Hebrew, RTL): תאריך, הפעולה, פרטים, אסמכתא, חובה, זכות, יתרה בש"ח, תאריך ערך, לטובת, עבור.
- **Maps to:** `transactions` (one row per data row) + optionally `balances` (final balance, as_of = statement date).
- **Sign:** חובה (debit) → negative amount; זכות (credit) → positive.
- **`reference`:** the אסמכתא column.
- **`counterparty`:** prefer the לטובת column when present; otherwise extract from פרטים.

### Credit-card statement (e.g. Hapoalim Mastercard)

- Credit card statement. Each row = one charge. **Sign convention:** charges are negative (money leaving you), payments to the card from your checking account are positive.
- **Maps to:** `transactions` on the credit-card account.
- **Hapoalim "transaction itemization" exports** (filename in Hebrew: `פירוט עסקאות וזיכויים.xlsx`) have the same shape. Columns: `תאריך עסקה`, `שם בית עסק`, `סכום בש"ח`, `מועד חיוב`, `סוג עסקה`, `מזהה כרטיס בארנק דיגיטלי`, `הנחה`, `הערות`. Use `שם בית עסק` for `counterparty`, `תאריך עסקה` for `date`, `מועד חיוב` for `value_date`, and stash `סוג עסקה` + `הערות` (e.g. "תשלומים | 3 installments" or original USD amount) into `description`.
- **Bit (BIT) rows** are P2P transfers via Israel's Bit app. Set `category='p2p_bit'`. Sign rule: if the row is an **outflow** (negative on this card / charge from your account), it's an **expense** — money you sent to someone. If the row is an **inflow** (positive — money coming back into your account via Bit), it's **not an expense**; it's a transfer-in or refund. The dashboard's expense filter relies on `amount_minor < 0`, so sign alone disambiguates — keep `category='p2p_bit'` regardless of direction.

### API-fetched pairs (emitted by the `fetch-bank-data` skill)

Produced by the `fetch-bank-data` skill (see `skills/fetch-bank-data/SKILL.md`). Each fetch run drops a pair of files into Drive `dump/` per non-empty account: a `.json` raw scrape and a `.notes.md` sidecar with prose observations. Both files describe the same account on the same fetch date — read them together.

**Filename anatomy:**

```
<YYYY-MM-DD>-<company>-<acct-suffix>-api-fetch[__tag__tag…].(json|notes.md)
```

`<YYYY-MM-DD>` is the fetch date (not txn date). `<company>` is the source you fetched (e.g. `hapoalim`, `cal`). `<acct-suffix>` is the last 4 of the institution's `accountNumber`. Tags are compact observation markers (`roundtrip-5000-brokerage`, `installments-amazon`, `fx-usd-zara`, `pending-3`, etc.); when none apply (quiet day) the filename has no `__tag` segment.

#### `bank_api_dump` (Hapoalim raw JSON)

- Library: `israeli-bank-scrapers` (`companyId: 'hapoalim'`). Field shape: `{success, accounts:[{accountNumber, balance, txns:[{date, processedDate, originalAmount, chargedAmount, description, memo, identifier, …}]}]}`. The skill slices `accounts[i]` per account before writing — each file holds one account.
- Amounts are JSON floats; multiply by 100 for `amount_minor`.
- Per-transaction running balance is **not** included — only the latest `balance` on the account object. Treat as a snapshot: `balances` row with `as_of = <fetch date>`, `component=NULL`, `amount_minor = round(balance × 100)`.
- **Maps to:** existing `transactions` + `balances` rows on the matching Hapoalim checking `accounts` row. Dedup at row level by `(account_id, date, amount_minor, reference)` — overlapping date ranges with prior XLSX statements are expected and harmless.

#### `bank_api_notes` (Hapoalim sidecar)

- Plain markdown. Header line names the date + company + acct suffix. Body is a bulleted list of observations the skill made at fetch time. Vocabulary: `roundtrip`, `internal transfer`, `first-time counterparty`, `amount anomaly`.
- Treat each bullet as a **hint, not ground truth.** Verify against the JSON and against cross-source documents (brokerage periodic statements, FX screenshots) before acting on it. A "first-time counterparty" might just be a known counterparty whose name changed on the bank side.

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
- **Maps to** `transactions` on a `kind='credit_card'` `accounts` row (institution `'Cal'`). First-ever Cal fetch: no `accounts` row exists for the returned `accountNumber` — auto-create one with `kind='credit_card'`, `institution='Cal'`, `name='Cal <last4-of-accountNumber>'`, `currency='ILS'`. Autonomy precedent: sync may create accounts without confirmation.
- **Known card → account mapping (do NOT create a duplicate account):** If Cal returns an `accountNumber` that is the same physical card as an existing Hapoalim-branded Mastercard account, map its txns onto the existing account and dedup against existing rows; never spin up a duplicate `Cal <last4>` account. Keep the concrete account/card mapping in private DB state or source docs, not in committed docs.
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
