---
name: send-finance-dashboard
description: Use when the user says "send my dashboard", "send dashboard to Telegram", "Telegram dashboard", "message my finances", or explicitly requests a finance summary in Telegram. Deterministically renders the self-contained HTML dashboard from the existing SQLite cache, then sends it and the pending sync summary through the configured Telegram bot. It does not fetch or sync financial or market data.
---

# send-finance-dashboard

Render the fixed HTML artifact from the current SQLite cache, then send it through the committed Telegram helper. Do not fetch banks, IBKR, Drive, prices, or FX in this skill. If the user wants fresh data first, run `sync-finance-data` as a separate requested action.

## Flow

1. Run `python3 scripts/migrate_db.py`. Stop if migration reports a conflict.
2. Run `python3 scripts/render_dashboard.py`. The renderer is read-only, verifies pinned assets, and writes `output/dashboard.html` atomically at mode 600. Never reuse an older artifact after a failed render.
3. Run `scripts/send_telegram.sh`. Never construct an inline token or `curl` command. The helper verifies freshness, derives its caption from the new HTML, sends `data/last_sync_summary.md` when present, and never prints the chat ID.

Missing `[telegram]` configuration means delivery is skipped; tell the user to run `/findash:setup` if they want to enable it. A configured send is successful only when the helper prints `Telegram delivery: Sent`.

## Report

Return only:

- the absolute path to `output/dashboard.html`;
- the helper's exact Telegram delivery line;
- the sync-summary delivery status.

Do not print balances, prices, account identifiers, counterparties, or source rows. Market and FX values come solely from the stored cache so repeated renders against the same SQLite state are deterministic.
