# Payslips

> Part of the [doc-types catalogue](./README.md) — principles, archetypes, and the full index live there.

## payslips/ — Israeli תלוש משכורת

- **Format:** PDF, password-protected. Passwords are stored locally under `[pdf-passwords]` in `.secrets/findash`.
- **Unlock:** `python3 scripts/unlock_pdf.py unlock <staged-file>` then read the returned private path and run `python3 scripts/unlock_pdf.py delete <returned-path>` afterward. The wrapper resolves `[pdf-passwords]` internally, so the password never appears in a command or transcript.
- **Typical content:**
  - Employer name + ID
  - Pay period (start / end / pay date)
  - Gross, net
  - Deductions: מס הכנסה, ביטוח לאומי, ביטוח בריאות, ניכוי פנסיה (תגמולי עובד), קרן השתלמות (תגמולי עובד)
  - Employer contributions: הפרשת פנסיה (תגמולים + פיצויים), הפרשת קרן השתלמות
  - Misc earnings/deductions (bonuses, allowances, refunds)
- **Writes to:**
  - `payslips` — one row, columns for the structured bits.
  - `payslip_line_items` — one row per unusual line (anything not in the explicit columns), with `kind` in {earning, deduction, info}.
  - `documents` — one row.
- **Fund-balance estimate:** after the latest pension/study-fund snapshot, the
  dashboard adds the employee + employer contribution fields from later payslips
  to that account's displayed value. This is an estimate—investment return is
  still unknown—and the next fund statement replaces it with an observed balance.
  Route automatically only when exactly one active account of that kind and
  currency exists. Multiple eligible policies remain unchanged with a review
  warning; never duplicate or split one payslip across them mechanically.
- A fund movement report is more authoritative than payroll. Once an exact
  post-snapshot contribution is recorded, older payslip estimates covered by that
  movement history are not added again; only newer payslips continue the estimate.
- **Payment reconciliation — `paid_on` is when the net pay reached the bank.**
  It is not the slip's printed pay date. The two often differ: weekends, bank
  processing, or a slip issued before or after the transfer. Cash flow counts a
  payslip and a salary credit as one payment only when `paid_on`, native currency
  and exact native net amount all equal that credit's. A missing or printed-only
  date therefore counts the same pay twice. The code's match stays exact, so pair
  them with judgment at ingest:
  - **Credit already in the ledger:** find salary credits on an owned account with
    the exact native net amount and currency, posted after the pay period starts,
    and not already paired with another payslip. With exactly one such candidate,
    set `paid_on` to its posting date. Keep the printed pay date, if any, in
    `documents.notes`.
  - **Slip arrives before the money:** store the printed pay date (or NULL) and
    note that pairing is pending. The salary credit's ingest completes it; see
    "Salary credits" in [classification.md](./classification.md#judgment-calls).
  - **Several candidates, or no exact net match** (split payment, correction,
    second employer): do not guess. Leave `paid_on` unpaired and flag the slip
    for review in `documents.notes`. Never adjust an amount or create a credit to
    force a match.
