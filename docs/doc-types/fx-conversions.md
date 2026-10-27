# FX conversions — internal brokerage ILS↔USD conversions

> Part of the [doc-types catalogue](./README.md) — principles, archetypes, and the full index live there.

## fx-conversions/ — internal brokerage ILS↔USD conversions

### Brokerage FX conversion screenshot

Screenshot from your brokerage's app showing a single ILS↔USD conversion happening **inside the brokerage** (ILS brokerage → USD brokerage or the reverse). The money is already at the brokerage at this point — these screenshots are **not** Hapoalim sub-account events. The brokerage account-header number on the screenshot is a brokerage sub-identifier, not a separate Hapoalim account.

The full money path looks like:

1. **ILS Hapoalim → ILS brokerage** (external funding; a `transfer` intent — the brokerage side stays expected until observed)
2. **ILS brokerage → USD brokerage** ← the FX screenshot captures this leg
3. **Buy stocks**, reducing the brokerage's USD cash; tracked via `trades` rows

- **Typical content (one broker's Hebrew labels — vary by broker):**
  - `ממטבע` / `למטבע` — source / destination currency
  - `סכום המרה` — amount being converted (in the source currency)
  - `מועד הבקשה` — request date (DD.MM.YYYY HH:MM)
  - `אסמכתא` — brokerage reference number (use as `transactions.reference`)
  - `שער המרה` — FX rate applied
  - `סטטוס פעולה` — operation status (טופל = processed)
- **Maps to:** one `fx_conversion` intent on the **same logical brokerage account**: `date` = the conversion date, `sell`/`buy` currencies, and what the screenshot states (two of converted amount, received amount, `שער המרה` rate), with the document row as evidence. The engine writes both cash legs (`category='fx'`, `flow_type='investment'`), stores the rate beside that day's market rate, and links them. Nothing goes into `fx_rates`. If reconcile already inferred this conversion from cash snapshots, the same intent attaches the document to it instead of creating a second one.
- **Not external funding:** this conversion does not add money to the investment portfolio. Never label either leg as a `deposit`/`withdrawal` transfer merely to make a chart move; the earlier bank→brokerage event is the external flow.
- Dashboard contribution totals, cumulative funding, and performance benchmarks
  exclude both conversion legs. The positive foreign-currency leg is shown as an
  **FX purchase** for rate history only; it never changes external-capital stats.
- **Do NOT insert** debit/credit pairs on Hapoalim sub-accounts. The FX is happening entirely inside the brokerage; Hapoalim's ledger has nothing to record on this date.
- **Converted amount:** give both posted amounts when shown; if only one side and the applied rate are explicit, give those two and the engine computes the other. If either is ambiguous, don't send the intent: keep the document for review instead of confirming invented cash.
