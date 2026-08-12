# Full statements — checking & credit card

> Part of the [doc-types catalogue](./README.md) — principles, archetypes, and the full index live there.

## full-statements/ — checking & credit card

### Bank app screenshot

- Mobile/web app screenshot of a Hapoalim checking account showing current balance + recent transactions. Use when the user wants to capture mid-month state before the official XLSX/PDF statement is available.
- Read with Read tool's image support; transcribe each transaction row.
- **Maps to:**
  - Raw transactions on the checking account, with source-event identities. Cross-check later statement observations and link clear matches to the same canonical event; date/amount/reference are evidence, not an automatic-delete key.
  - One canonical balance anchor plus `balance_evidence` for the prominent header balance.
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
- **P2P rows** keep a provider-specific display category, but sign alone does not determine economics. An outflow may be spending or an owned-account transfer; an inflow may be income, reimbursement, refund or transfer. Set `flow_type` from the evidence and ask when ambiguous.

### API-fetched pairs (emitted by the `fetch-bank-data` skill)

Produced by the `fetch-bank-data` skill (see `skills/fetch-bank-data/SKILL.md`). Each fetch run stages a pair of files in local `inbox/staging/fetched/` per non-empty account — never Drive: a `.json` raw scrape and a `.notes.md` sidecar with prose observations. Both files describe the same account on the same fetch date — read them together. Sync creates **one** `documents` row per pair (synthetic `drive_id = 'local:fetch:<json-filename>:<sha256-first-12>'`), folds the sidecar bullets into `documents.notes`, and deletes both files after the rows commit — SQLite is the pair's only persistence.

**Filename anatomy:**

```
<YYYY-MM-DD>-<company>-account-<index>-api-fetch.(json|notes.md)
```

`<YYYY-MM-DD>` is the fetch date (not transaction date), `<company>` is the
configured source, and `<index>` is a zero-padded ordinal in that result. The
ordinal is deliberately unrelated to an account/card number. Amounts,
counterparties, merchants, anomaly tags, and account fragments never appear in
staging filenames; the private JSON and sidecar contain the evidence.

#### `bank_api_dump` (Hapoalim raw JSON)

- Library: `israeli-bank-scrapers` (`companyId: 'hapoalim'`). Field shape: `{success, accounts:[{accountNumber, balance, txns:[{date, processedDate, originalAmount, chargedAmount, description, memo, identifier, …}]}]}`. The skill slices `accounts[i]` per account before writing — each file holds one account.
- Amounts are JSON major-unit numbers; convert with `findash_core.currencies.major_to_minor` for the row's currency.
- Per-transaction running balance is **not** included—only the latest account balance. Treat it as a snapshot observation, retain it in `balance_evidence`, and select one canonical plain balance anchor for the fetch date.
- **Maps to:** the logical checking account through its bank `account_feeds` row. Provider transaction ids form source keys; when an XLSX later reports the same economic event, retain both observations and link them to one canonical event.

#### `bank_api_notes` (Hapoalim sidecar)

- Plain markdown. Header names only the date, company, and opaque result index. Body is a bulleted list of observations the skill made at fetch time. Vocabulary: `roundtrip`, `internal transfer`, `first-time counterparty`, `amount anomaly`.
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
- Do not accumulate card charges into a historical liability before the first
  balance snapshot. Card exports commonly omit the repayment leg, so charges
  alone cannot establish a point-in-time balance. Keep the transactions for
  expense history, show liability coverage as incomplete, and begin advancing
  the liability only after a real balance anchor exists.
- **Maps to** `transactions` on a `kind='credit_card'` `accounts` row (institution `'Cal'`). First-ever Cal fetch: no `accounts` row may exist for the returned `accountNumber` — auto-create one with a neutral name such as `Cal card 01`, never an account/card fragment. Keep the provider identifier only in the private `account_feeds.external_account_ref` field. Sync may create this unambiguous account without confirmation.
- **Known card → account mapping (do NOT create a duplicate account):** If Cal returns an `accountNumber` that is the same physical card as an existing Hapoalim-branded Mastercard account, map its txns onto the existing account and dedup against existing rows; never create a second logical card account. Keep the concrete account/card mapping in private DB state or source docs, not in committed docs.
- Sign convention: merchant charge → negative `amount_minor`; refund / payment to the card → positive (matches the Hapoalim Mastercard convention).
- `date` = `txn.date`; `value_date` = `txn.processedDate`.
- `category`: use judgment per merchant. New `merchant`-style categories are open vocab.
- **Lossy enrichments stashed in `description`** (the schema has no structured columns for these yet — out-of-scope follow-up):
  - FX (`originalCurrency !== 'ILS'`): `"<merchant> (<currency> <original> → ILS <charged> @ <rate>)"`
  - Installments (`type === 'installments'`): `"<merchant> (2/6, group #<identifier>)"` — `identifier` is the group key, so all installments of one purchase are findable.
  - Pending (`status === 'pending'`): preserve the raw observation but link it to
    a canonical event with `status='needs_review'`, so calculations do not count
    an authorization as posted cash. When the same installment/transaction later
    appears as completed, update that reviewed event to `confirmed` rather than
    creating another economic event.
- **Source identity:** `identifier` is a purchase-group key, not necessarily a
  transaction key. Installment rows for one purchase share it, so combine it with
  the installment number (and the provider's stable row ID when available). Never
  deduplicate all installments on `(account_id, purchase_date, identifier)`; that
  would erase legitimate monthly charges. The key remains stable across a
  pending→completed status change.

#### `cal_api_notes` (Cal sidecar)

- Same shape + treatment as `bank_api_notes`. Bullet vocabulary: installment chains, FX merchants, first-time merchants, pending status, cross-source divergence (Cal total vs Hapoalim's monthly `card_payment` row).
- Hints, not ground truth.
