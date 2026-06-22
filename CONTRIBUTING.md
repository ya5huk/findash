# Contributing

I would be very happy to see PRs, ideas, and experiments that help FinDash grow beyond the current setup.

The project works today, but it is intentionally small. That leaves a lot of useful room to expand:

- **More bank and card providers.** FinDash currently fetches data from Hapoalim and Cal, while `israeli-bank-scrapers` supports many more Israeli financial institutions. Adding more providers would make the automatic fetch flow useful to more people.
- **Adapting beyond Israel.** FinDash is Israel-first (banks, the 25% capital-gains tax, ILS base currency, payslip/retirement structure). The locale-specific touch points are listed in [Adapting to another country](README.md#adapting-to-another-country); PRs that generalize them — or that adapt the system to another country — are very welcome.
- **More dashboard delivery options.** The dashboard currently renders as a self-contained static HTML file. That is simple and portable, but some users may prefer a local React app, a packaged desktop-style viewer, a different notification channel, or another delivery model entirely.
- **Better packaging and setup.** There may be a cleaner way to package the skills, scripts, templates, local secrets, and dashboard output so users can get started with less manual setup.
- **New finance views.** Cash flow, investment performance, source-document auditability, and category review can all be improved without making the system less private or less transparent.

If you have a rough idea, open an issue. If you already know the shape of the change, PRs are welcome.

Please keep contributions compatible with the repo's privacy model:

- Do not commit real account numbers, balances, transaction amounts, counterparties, credentials, Drive IDs, local database contents, rendered dashboards, or other personal financial data.
- Keep reusable code and docs generalized for public use.
- Keep interpretation in the agent workflow where possible; scripts should do mechanical work, not hard-code personal categorization rules.
- Store money as integer minor units, not floating point values.
- Preserve the audit trail from every inserted finance row back to its source document.

If you want to discuss an idea, find me on LinkedIn: [Ilan Yashuk](https://www.linkedin.com/in/ilan-yashuk/).
