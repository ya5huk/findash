# Payslips

> Part of the [doc-types catalogue](./README.md) — principles, archetypes, and the full index live there.

## payslips/ — Israeli תלוש משכורת

- **Format:** PDF, password-protected. Passwords are stored locally under `[pdf-passwords]` in `.secrets/findash`.
- **Unlock:** `qpdf --password=$PASS --decrypt <file> <tmp>` then read the temp file. Delete temp when done.
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
