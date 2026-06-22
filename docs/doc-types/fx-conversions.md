# FX conversions — internal brokerage ILS↔USD conversions

> Part of the [doc-types catalogue](./README.md) — principles, folder routing, and the full index live there.

## fx-conversions/ — internal brokerage ILS↔USD conversions

### `*-fx-<from>-<to>-<src-amount>.<png|jpg>`

Screenshot from your brokerage's app showing a single ILS↔USD conversion happening **inside the brokerage** (ILS brokerage → USD brokerage or the reverse). The money is already at the brokerage at this point — these screenshots are **not** Hapoalim sub-account events. The brokerage account-header number on the screenshot is a brokerage sub-identifier, not a separate Hapoalim account.

The full money path looks like:

1. **ILS Hapoalim → ILS brokerage** (the actual deposit; recorded from the `investment_deposits` XLSX with `category='deposit'` on the brokerage account, USD-denominated rows where each row's description embeds the ILS source and rate)
2. **ILS brokerage → USD brokerage** ← the FX screenshot captures this leg
3. **Buy stocks**, reducing the brokerage's USD cash; tracked via `trades` rows

- **Typical content (Hebrew labels):**
  - `ממטבע` / `למטבע` — source / destination currency
  - `סכום המרה` — amount being converted (in the source currency)
  - `מועד הבקשה` — request date (DD.MM.YYYY HH:MM)
  - `אסמכתא` — brokerage reference number (use as `transactions.reference`)
  - `שער המרה` — FX rate applied
  - `סטטוס פעולה` — operation status (טופל = processed)
- **Maps to:**
  - `transactions` — one row on the brokerage account with `category='deposit'`, `currency='USD'`, amount = converted USD (`src / rate` rounded to 2dp), `counterparty='<your brokerage>'`, `reference=<אסמכתא>`, `description='from <src> ILS @ <rate>'`. This matches the convention used by the `investment_deposits` XLSX, so the FX shows up correctly in cumulative-deposits tracking.
  - `fx_rates` — one row: `(date, USD, ILS, rate)` so the dashboard can value USD holdings on/around this date.
- **Do NOT insert** debit/credit pairs on Hapoalim sub-accounts. The FX is happening entirely inside the brokerage; Hapoalim's ledger has nothing to record on this date.
- **Sign of converted amount:** the brokerage rounds; if the screenshot only shows the source amount, compute the USD amount (`src / rate` or `src * rate` depending on direction) and proceed.
