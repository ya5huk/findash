---
name: render-finance-dashboard
description: Use when the user says "render dashboard", "show my finances", "build the dashboard", or any morning-summary equivalent. Reads SQLite, fetches live prices/FX from Yahoo Finance, fills the HTML template, and writes a self-contained dashboard to `output/dashboard.html`.
---

# render-finance-dashboard

You render the booky finance dashboard from the current state of `data/finance.db`. **The visual design is fixed by the template; you only fill it with data.** The mechanical part (SQL → HTML, live-price fetch, asset inlining) lives in `scripts/render_dashboard.py` — invoke it, don't re-derive it.

## Where things live

- Canonical renderer: `scripts/render_dashboard.py`
- HTML template (placeholders in `{{NAME}}` form): `templates/dashboard.html.tpl`
- CSS: `templates/styles.css` — inlined into the HTML at render time
- Chart constructors: `templates/charts.js` — inlined into the HTML
- Vendored offline assets (gitignored, produced by `scripts/bundle-assets.py`):
  - `templates/vendor/chart.umd.min.js`
  - `templates/vendor/chartjs-adapter-date-fns.bundle.min.js`
  - `templates/vendor/fonts-inline.css` (EB Garamond + Cormorant Garamond, base64 woff2)
- Telegram credentials: `.secrets/telegram` (key=value, two lines: `bot_token=…` and `chat_id=…`)
- Local DB: `data/finance.db`
- Output: a single self-contained file at `output/dashboard.html`
- SQL schema and example queries: [`docs/sqlite-schema.md`](../../../docs/sqlite-schema.md)
- Visual rules: [`docs/design-system.md`](../../../docs/design-system.md)

## Flow

1. **Run the renderer.** From the project root:

   ```bash
   python3 scripts/render_dashboard.py
   ```

   The script handles everything mechanical:
   - Live-fetches prices for currently-held securities and `USD/ILS`, `GBP/ILS` FX rates from Yahoo Finance (`query1.finance.yahoo.com/v8/finance/chart/{symbol}`). User-Agent header required.
   - Yahoo symbol translation: `RR.LSE` → `RR.L`. Quote units: USD direct; LSE pence (`÷100` → GBP); TASE agorot (`÷100` → ILS).
   - Persists fetched prices into `prices` and the new FX into `fx_rates` so a history accumulates day-by-day.
   - Computes net worth in ILS, brokerage market value, unrealized P/L per position.
   - Excludes accounts with `closed_on IS NOT NULL` from Cash and from the headline (they still show in SQLite-data).
   - Sorts the positions table by ILS-equivalent market value, descending (so SPY ranks above RR.LSE despite fewer shares).
   - Orders brokerage deposits newest first.
   - Builds the SQLite-data debug tabs (first 50 rows + count per table).
   - Inlines CSS, fonts, Chart.js, and the chart-app JS into a single HTML file.
   - Writes `output/dashboard.html` and a small meta file at `/tmp/dashboard_meta.json` (used by the Telegram step).

   If any vendor file is missing, the script stops with a clear message. Run `python3 scripts/bundle-assets.py` once to fix.

2. **Send to Telegram.** Delivery lives in one committed, allowlisted command so the
   unattended daily `claude -p` run can run it without per-call approval (it can't build
   the read-secret + `curl` send inline — the permission analyzer won't auto-approve a
   command that sources `.secrets` and expands `${bot_token}`, and the `Write` tool isn't
   granted unattended). The token is read at runtime and never printed.

   ```bash
   scripts/send_telegram.sh            # add: --note "<line>" to append a caption line
   ```

   What it does (= the old steps 2+3, now deterministic):
   - Sends `output/dashboard.html` as a document, caption `Finance — <AS_OF_DATE> · <net worth>`
     (literal `·` U+00B7), read from `/tmp/dashboard_meta.json`.
   - If `data/last_sync_summary.md` exists, prepends `Sync — <AS_OF_DATE>` and sends it as
     `sendMessage` (plain text; split into ≤4000-char parts on `\n` boundaries), then deletes
     it on success. Absent → prints `Sync summary: Skipped (no summary)`.
   - Missing `.secrets/telegram` → prints `Telegram delivery skipped: …` and exits 0 (local
     file still useful). Never deletes the local dashboard on a Telegram failure.

   Pass `--note "<line>"` to append a caption line — the morning flow uses this to surface a
   best-effort **fetch** failure. The script prints `Sent to chat <id>` /
   `Telegram delivery failed: <description>` / the `Sync summary: …` line — relay them in step 3.

3. **Report.** Print:
   - The absolute path of `output/dashboard.html`.
   - The Telegram delivery status: `Sent to chat <chat_id>`, `Telegram delivery skipped: …`, or `Telegram delivery failed: <description>`.
   - The sync summary delivery status: `Sync summary: Sent (<N> bullets)`, `Sync summary: Skipped (no summary)`, or `Sync summary: Failed: <description>`.
   - Any tickers Yahoo couldn't price (the script logs them on `FAILED:` — flag them so the user knows which positions fell back to cost basis).

## Where judgment still lives (not in the script)

- Deciding whether a fresh balance figure looks plausible or whether a sync mis-parsed something. If a number looks wrong, check the source doc and explain rather than rendering a broken figure (e.g. a closed Hapoalim sub-account showing an apparent negative balance from a parser artifact when the real closing balance was 0).
- Whether to mention a particular caveat in the Overview footnote (e.g. "no price for X" or "this account was just opened").
- The contents of the Telegram caption when the dashboard contains something unusual.

## Principles

- **Don't invent design.** All visual choices are in `styles.css` and the template. Don't add inline styles, classes, or markup not already defined.
- **Be honest with empty states.** If a section has no data yet, show a brief italic muted note ("No payslips yet"), not a fake placeholder row.
- **Number formatting:** group thousands; 2 decimals for amounts <10,000, 0 decimals for ≥10,000. Right-align money columns.
- **Hebrew text passes through unchanged.** `dir="auto"` on table cells is already handled by the script.
- **The "SQLite data" tab is for debugging.** Show raw rows truthfully. Don't reformat or hide columns.
