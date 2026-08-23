# FX conversions — internal brokerage ILS↔USD conversions

> Part of the [doc-types catalogue](./README.md) — principles, archetypes, and the full index live there.

## fx-conversions/ — internal brokerage ILS↔USD conversions

### Brokerage FX conversion screenshot

Screenshot from your brokerage's app showing a single ILS↔USD conversion happening **inside the brokerage** (ILS brokerage → USD brokerage or the reverse). The money is already at the brokerage at this point — these screenshots are **not** Hapoalim sub-account events. The brokerage account-header number on the screenshot is a brokerage sub-identifier, not a separate Hapoalim account.

The full money path looks like:

1. **ILS Hapoalim → ILS brokerage** (external funding; recorded directly or through the reviewed bank+FX reconciliation)
2. **ILS brokerage → USD brokerage** ← the FX screenshot captures this leg
3. **Buy stocks**, reducing the brokerage's USD cash; tracked via `trades` rows

- **Typical content (one broker's Hebrew labels — vary by broker):**
  - `ממטבע` / `למטבע` — source / destination currency
  - `סכום המרה` — amount being converted (in the source currency)
  - `מועד הבקשה` — request date (DD.MM.YYYY HH:MM)
  - `אסמכתא` — brokerage reference number (use as `transactions.reference`)
  - `שער המרה` — FX rate applied
  - `סטטוס פעולה` — operation status (טופל = processed)
- **Maps to:**
  - two cash legs on the **same logical brokerage account**: a negative source-currency event and a positive destination-currency event. Give each a stable source identity/evidence link, `category='fx'`, and `flow_type='investment'`.
  - `fx_rates` — one `source='document'` row in the stated orientation (or its mathematically exact inverse) so historical valuation can use the actual conversion rate.
- **Not external funding:** this conversion does not add money to the investment portfolio. Never label either leg as a `deposit`/`withdrawal` transfer merely to make a chart move; the earlier bank→brokerage event is the external flow.
- Dashboard contribution totals, cumulative funding, and performance benchmarks
  exclude both conversion legs. The positive foreign-currency leg is shown as an
  **FX purchase** for rate history only; it never changes external-capital stats.
- **Do NOT insert** debit/credit pairs on Hapoalim sub-accounts. The FX is happening entirely inside the brokerage; Hapoalim's ledger has nothing to record on this date.
- **Converted amount:** prefer both posted amounts. If one side and an actual applied rate are explicit, compute the missing side once using the shared currency helpers and the stated direction. If either is ambiguous, retain the observation as review evidence instead of confirming invented cash.
