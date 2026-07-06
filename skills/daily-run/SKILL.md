---
name: daily-run
description: Use when the user says "run the daily flow", "run the morning flow", "run everything", "fetch sync render", "do my finances", or any whole-routine equivalent — and the skill `scripts/run_daily.sh` invokes for the unattended daily cron job. Runs the full findash flow end-to-end (fetch → sync → render → deliver to Telegram).
---

# daily-run

You run the **entire** findash morning flow in one session: fetch fresh bank data, attempt IBKR, ingest everything into SQLite, render the dashboard, and deliver it to Telegram. This is the skill `scripts/run_daily.sh` invokes for the unattended daily cron job — and it also works when a human just asks for the whole flow. The actual work lives in the four step skills; this skill owns only the orchestration: order, best-effort semantics, the unattended rules, and the final status report.

## Operating rules

- **Run everything in the foreground, to completion.** Never launch a scraper or other long-running command as a background task and then yield. When running unattended (cron, via `run_daily.sh`) no background-task notifications reach you, so anything deferred to "when it finishes" never runs — the process exits the instant the turn ends. Wait for each command to finish before starting the next.
- **Don't end the turn early.** You are done only when the dashboard has rendered **and** been confirmed sent to Telegram (or retries are exhausted and the failure is reported).
- **Privacy / log hygiene.** This repository is public and cron logs are local but still sensitive. Never print account numbers, balances, transaction amounts, merchant / counterparty names, raw scrape JSON, or other personal finance details to stdout. Report only high-level status — source names, counts, and whether files were uploaded / synced / rendered / sent. Detailed reasoning goes only into the local artifact files the flow already uses.

## The flow, in order

1. **Run the `fetch-bank-data` skill** — pull fresh transactions + balances from the banks, scrapers to completion in the foreground; pairs land in `inbox/staging/fetched/` (never Drive). **Best-effort:** if it fails for any reason (a bank forces a fresh OTP, a scraper times out, a login breaks), do NOT abort the run — note which source failed and why, then continue. See [`../fetch-bank-data/SKILL.md`](../fetch-bank-data/SKILL.md).
2. **Run the `fetch-investments` skill (best-effort, gated)** — only attempt it if IBKR was ever onboarded: `sqlite3 data/finance.db "SELECT 1 FROM accounts WHERE live_source='ibkr' LIMIT 1"` returns a row (or the connector is visibly connected). Never onboarded → skip silently, like a bank with no credentials. Onboarded but the connector is unavailable (every unattended run) → the skill itself writes the `⚠️ IBKR skipped` warning bullet and you continue. See [`../fetch-investments/SKILL.md`](../fetch-investments/SKILL.md).
3. **Run the `sync-finance-data` skill** — gather new Drive drops into staging (best-effort), ingest everything staged, sort `dump/`, weekly DB backup. This step is required, but its Drive phases degrade internally into `⚠️` warning bullets — only a sync *crash* fails the run. See [`../sync-finance-data/SKILL.md`](../sync-finance-data/SKILL.md).
4. **Run the `render-finance-dashboard` skill** — render the dashboard, then deliver it with `scripts/send_telegram.sh` (do NOT construct an inline curl command). If step 1 (bank fetch) failed, surface that on Telegram alongside the dashboard via `scripts/send_telegram.sh --note "<which source failed + short reason>"`, so the user knows the data may be stale — the `--note` caption line is for stale-data fetch failures only; every other problem reaches Telegram as a `⚠️` bullet riding in the summary message. The run is complete only once the Telegram send has succeeded. See [`../render-finance-dashboard/SKILL.md`](../render-finance-dashboard/SKILL.md).

## Report

End with a short status block (no financial details):

- **Fetch:** per-source result, e.g. `Hapoalim ok, Cal skipped` — or the reason if the best-effort fetch failed.
- **IBKR:** `ok (<N> trades)` / `skipped (<reason>)` / `not onboarded`.
- **Sync:** counts only (staged files ingested / dump files filed / backup done|skipped|failed).
- **Render:** `output/dashboard.html` written.
- **Telegram:** relay `scripts/send_telegram.sh`'s own line verbatim — `Sent to chat <id>` on success. `run_daily.sh` keys its success check on this line, so always surface it.
- **Warnings:** `<N>` (the `⚠️` bullets sent in the summary), or `none`.

**Degraded ≠ failed.** A run that delivered the dashboard despite warnings (Drive down, IBKR skipped, a bank source failed) is a SUCCESS — never describe it with failure phrasing like `could not complete` or `not rendered or sent`; `run_daily.sh` greps those exact phrases as blockers and would retry a run that actually worked. Reserve them for a genuine render/send failure: if the dashboard could not be rendered or sent, say so plainly — e.g. `dashboard was not rendered or sent: <reason>` — rather than implying success; the wrapper treats that phrasing as a failed attempt and retries, which is what you want.
