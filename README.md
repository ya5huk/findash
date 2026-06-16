# FinDash

Get a daily finance status dashboard on Telegram.

<p align="center">
  <img alt="FinDash dashboard overview" src="docs/images/dashboard-overview.png" width="920">
</p>

<p align="center">
  <img alt="Static HTML" src="https://img.shields.io/badge/output-static%20HTML-9b1c1f?style=for-the-badge">
  <img alt="SQLite" src="https://img.shields.io/badge/store-SQLite-2f5d3a?style=for-the-badge">
  <img alt="Drive first" src="https://img.shields.io/badge/source-Google%20Drive-b49b4a?style=for-the-badge">
  <img alt="Privacy first" src="https://img.shields.io/badge/privacy-local%20secrets-2a211d?style=for-the-badge">
</p>

FinDash combines AI reasoning over your financial documents with deterministic code that does the math. The AI helps interpret messy real-world records like statements, payslips, brokerage screenshots, deposits, and transfers. The code stores the results in SQLite, applies repeatable calculations, and renders a dashboard whose numbers are backed by auditable source documents.

Each morning, it can fetch fresh bank/card data, sync new source documents, rebuild the dashboard, and send the result to Telegram.

## What It Does

<table>
  <tr>
    <td width="50%">
      <img alt="Net worth chart" src="docs/images/net-worth-line.jpg">
    </td>
    <td width="50%">
      <img alt="Stocks versus S&P 500 versus deposits" src="docs/images/stocks-vs-spy.jpg">
    </td>
  </tr>
  <tr>
    <td><strong>Net worth over time</strong><br>Cash, locked savings, pension, training fund, and brokerage value roll into one view.</td>
    <td><strong>Investment benchmark</strong><br>Brokerage performance is compared with cumulative deposits and an S&P 500 what-if line.</td>
  </tr>
</table>

<table>
  <tr>
    <td width="50%">
      <img alt="Expense category breakdown" src="docs/images/expenses-breakdown.jpg">
    </td>
    <td width="50%">
      <img alt="Daily spend chart" src="docs/images/daily-spend.jpg">
    </td>
  </tr>
  <tr>
    <td><strong>Expense breakdown</strong><br>Merchant-level credit-card rows and bank transactions are grouped into real spending categories.</td>
    <td><strong>Daily spend</strong><br>Recent spending is visible without opening a bank app or spreadsheet.</td>
  </tr>
</table>

<table>
  <tr>
    <td width="50%">
      <img alt="Monthly totals chart" src="docs/images/monthly-totals.jpg">
    </td>
    <td width="50%">
      <img alt="Monthly cash flow table" src="docs/images/monthly-cash-flow.jpg">
    </td>
  </tr>
  <tr>
    <td><strong>Monthly totals</strong><br>Long-running monthly expense history, including quiet months and spikes.</td>
    <td><strong>Monthly cash flow</strong><br>Average spend, income, and net flow are summarized across short, yearly, and all-time windows.</td>
  </tr>
</table>

<table>
  <tr>
    <td width="50%">
      <img alt="Dashboard section list" src="docs/images/dashboard-sections.jpg">
    </td>
    <td width="50%">
      <img alt="Brokerage deposits chart" src="docs/images/brokerage-deposits.jpg">
    </td>
  </tr>
  <tr>
    <td><strong>Sectioned dashboard</strong><br>Overview, expenses, stocks, payslips, and SQLite data stay separated for quick scanning.</td>
    <td><strong>Brokerage deposits</strong><br>Deposit timing is shown against USD/ILS and GBP/ILS movement.</td>
  </tr>
</table>

## How It Works

```text
                          +----------------------+
                          | Google Drive vault   |
                          | dump/                |
                          +----------+-----------+
                                     ^
                 manual upload       |        automatic fetch
        statements / PDFs / XLSX ----+---- fresh Hapoalim + Cal data
                                              fetch-bank-data

                                     |
                                     v
                          +----------------------+
                          | sync-finance-data    |
                          | AI interpretation    |
                          | audited inserts      |
                          +----------+-----------+
                                     |
                                     v
                          +----------------------+
                          | SQLite               |
                          | deterministic math   |
                          +----------+-----------+
                                     |
                                     v
                          +----------------------+
                          | render dashboard     |
                          | HTML + Telegram      |
                          +----------------------+
```

`sync-finance-data` scans the Drive vault, reasons through each source document, inserts rows with source links into SQLite, and backs the database up to Drive. `render-finance-dashboard` reads SQLite, fetches live prices/FX, fills the template, and writes one portable HTML file.

The dashboard is self-contained: CSS, fonts, Chart.js, chart data, and markup are inlined into `output/dashboard.html`.

## Privacy Model

This repo is designed so the public code can be shared while private financial state stays local or in your Drive vault.

Secrets live in small local files:

```text
.secrets/drive       # root_folder_id=<Drive folder ID>
.secrets/telegram    # bot_token=... / chat_id=...
.secrets/hapoalim    # user_code=... / password=...
.secrets/cal         # username=... / password=...
.secrets/pdf-passwords
```

The committed docs use placeholders for account suffixes, card suffixes, Drive IDs, balances, transaction IDs, and example amounts. Concrete mappings belong in the private SQLite DB or source documents, not in git.

## Repo Map

```text
docs/                 project docs: schema, Drive layout, source document types
scripts/              mechanical parsers, scrapers, renderers, daily runner
templates/            dashboard shell, CSS, and chart code
.claude/skills/       agent workflows for fetch, sync, render, and doctor
data/                 local SQLite database, gitignored
inbox/                transient downloads, gitignored
output/               rendered dashboard, gitignored
```

## Quickstart

Install the external tools you use:

```bash
python3 --version
node --version
rclone version
sqlite3 --version
```

Bundle dashboard assets once:

```bash
python3 scripts/bundle-assets.py
```

Install scraper dependencies:

```bash
cd scripts
npm install
```

Create local secrets with `chmod 600`, configure `rclone.conf`, then run the three workflows:

```text
/fetch-bank-data
/sync-finance-data
/render-finance-dashboard
```

For unattended daily runs, use:

```bash
CLAUDE_BIN="$(command -v claude)" scripts/run_daily.sh
```

## License

No open-source license is currently granted. All rights are reserved by the repository owner.
