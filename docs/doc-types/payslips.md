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
- **Payment reconciliation:** capture `paid_on` whenever the document states it. A payslip and salary credit are treated as the same net payment only when `paid_on`, native currency, and exact native net amount all match one-to-one. A period boundary substituted for a missing payment date never proves a match, and near amounts/dates are not guessed.
