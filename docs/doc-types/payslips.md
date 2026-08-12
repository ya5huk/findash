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
- **Payment reconciliation:** capture `paid_on` whenever the document states it. A payslip and salary credit are treated as the same net payment only when `paid_on`, native currency, and exact native net amount all match one-to-one. A period boundary substituted for a missing payment date never proves a match, and near amounts/dates are not guessed.
