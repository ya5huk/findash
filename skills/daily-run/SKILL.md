---
name: daily-run
description: Use when the user says "run the daily flow", "run the morning flow", "run everything", "fetch sync render", "do my finances", or any whole-routine equivalent — and the skill `scripts/run_daily.sh` invokes for the unattended daily cron job. Runs the full findash flow end-to-end (fetch → sync → render → deliver to Telegram).
---

# daily-run

You run the **entire** findash morning flow in one session: fetch fresh bank data, ingest it into SQLite, render the dashboard, and deliver it to Telegram. This is the skill `scripts/run_daily.sh` invokes for the unattended daily cron job — and it also works when a human just asks for the whole flow. The actual work lives in the three step skills; this skill owns only the orchestration: order, best-effort fetch, the unattended rules, and the final status report.

## Operating rules

- **Run everything in the foreground, to completion.** Never launch a scraper or other long-running command as a background task and then yield. When running unattended (cron, via `run_daily.sh`) no background-task notifications reach you, so anything deferred to "when it finishes" never runs — the process exits the instant the turn ends. Wait for each command to finish before starting the next.
- **Don't end the turn early.** You are done only when the dashboard has rendered **and** been confirmed sent to Telegram (or retries are exhausted and the failure is reported).
- **Privacy / log hygiene.** This repository is public and cron logs are local but still sensitive. Never print account numbers, balances, transaction amounts, merchant / counterparty names, raw scrape JSON, or other personal finance details to stdout. Report only high-level status — source names, counts, and whether files were uploaded / synced / rendered / sent. Detailed reasoning goes only into the local artifact files the flow already uses.

## The flow, in order

1. **Run the `fetch-bank-data` skill** — pull fresh transactions + balances from the banks, scrapers to completion in the foreground. **Best-effort:** if it fails for any reason (a bank forces a fresh OTP, a scraper times out, a login breaks), do NOT abort the run — note which source failed and why, then continue. See [`../fetch-bank-data/SKILL.md`](../fetch-bank-data/SKILL.md).
2. **Run the `sync-finance-data` skill** — triage `dump/`, ingest everything new in the vault, reasoning carefully over each file. See [`../sync-finance-data/SKILL.md`](../sync-finance-data/SKILL.md).
3. **Run the `render-finance-dashboard` skill** — render the dashboard, then deliver it with `scripts/send_telegram.sh` (do NOT construct an inline curl command). If step 1 (fetch) failed, surface that on Telegram alongside the dashboard via `scripts/send_telegram.sh --note "<which source failed + short reason>"`, so the user knows the data may be stale. The run is complete only once the Telegram send has succeeded. See [`../render-finance-dashboard/SKILL.md`](../render-finance-dashboard/SKILL.md).

> **IBKR is deliberately not in this flow.** Interactive Brokers data comes from the official IBKR connector in Claude, which only works in an **interactive** session (it can't authenticate from the unattended `claude -p` cron run). So `fetch-investments` is a manual step, not chained here — refresh holdings by hand with `/findash:fetch-investments` and then re-render. See [`../fetch-investments/SKILL.md`](../fetch-investments/SKILL.md).

## Report

End with a short status block (no financial details):

- **Fetch:** per-source result, e.g. `Hapoalim ok, Cal skipped` — or the reason if the best-effort fetch failed.
- **Sync:** counts only (docs ingested / triaged).
- **Render:** `output/dashboard.html` written.
- **Telegram:** relay `scripts/send_telegram.sh`'s own line verbatim — `Sent to chat <id>` on success. `run_daily.sh` keys its success check on this line, so always surface it.

If the dashboard could not be rendered or sent, say so plainly — e.g. `dashboard was not rendered or sent: <reason>` — rather than implying success. The `run_daily.sh` wrapper treats that phrasing as a failed attempt and retries, which is what you want.
