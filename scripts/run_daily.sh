#!/bin/bash
# Wrapper for the launchd daily job. Runs claude -p; on non-zero exit, waits 60s
# and retries exactly once. Reason: morning runs sometimes hit a transient
# Anthropic API socket close right after lid-open, before networking has settled.
set -uo pipefail

CLAUDE=${CLAUDE_BIN:-$(command -v claude)}
if [ -z "$CLAUDE" ]; then
    echo "run_daily: claude binary not found; set CLAUDE_BIN=/path/to/claude" >&2
    exit 127
fi
PROMPT="You are running UNATTENDED — no human is watching this session, and background-task notifications will NOT reach you. Therefore run every step in the FOREGROUND and wait for each command to finish before moving on. Never launch a scraper or any long-running command as a background task and then yield: anything you defer to 'when it finishes' will never run, because the process exits the instant you end your turn. Do NOT end your turn until step 3 is fully done — the dashboard rendered AND confirmed sent to Telegram (or retries exhausted and the failure reported).

Privacy/log hygiene: this repository is public and cron logs are local but still sensitive. Do not print account numbers, balances, transaction amounts, merchant/counterparty names, raw scrape JSON, or other personal finance details to stdout. Report only high-level status, source names, counts, and whether files were uploaded/synced/rendered/sent. Put detailed reasoning only in local artifact files intended for the flow.

Run the morning finance flow in order:

1. /fetch-bank-data — pull fresh transactions and balances from the banks. Run the scrapers to completion in the foreground; do not background them. This step is BEST-EFFORT: if it fails for any reason (a bank forces a fresh OTP, a scraper times out, login breaks, etc.), do NOT abort the run. Note what failed and why, then continue to the next step.
2. /sync-finance-data — reason carefully against the files you stumble upon.
3. /render-finance-dashboard — then send the dashboard on Telegram using scripts/send_telegram.sh. Do not construct an inline curl command. The run is only complete once the Telegram send has succeeded.

If step 1 (fetch-bank-data) failed, also surface that failure on Telegram alongside the dashboard: a short, clear note saying which bank/source failed and the error, so I know the data may be stale."

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

run_once() {
    local output_file status
    output_file="$(mktemp "${TMPDIR:-/tmp}/findash-daily.XXXXXX")"

    "$CLAUDE" -p "$PROMPT" >"$output_file" 2>&1
    status=$?
    cat "$output_file"

    if [ "$status" -ne 0 ]; then
        rm -f "$output_file"
        return "$status"
    fi

    if grep -Eiq 'could not complete|dashboard was not rendered|not rendered or sent|requires approval|hard-denied|permission sandbox blocked|commands are hard-denied' "$output_file"; then
        echo "run_daily: Claude reported a blocker; treating attempt as failed" >&2
        rm -f "$output_file"
        return 1
    fi

    if [ -f .secrets/telegram ] && ! grep -Eiq 'Sent to chat|Telegram delivery status: Sent|Telegram delivery: Sent' "$output_file"; then
        echo "run_daily: Telegram send confirmation missing; treating attempt as failed" >&2
        rm -f "$output_file"
        return 1
    fi

    rm -f "$output_file"
}

log "attempt 1"
if run_once; then
    log "attempt 1 succeeded"
    exit 0
fi

log "attempt 1 failed (exit $?); sleeping 60s then retrying"
sleep 60

log "attempt 2"
if run_once; then
    log "attempt 2 succeeded"
    exit 0
fi

log "attempt 2 also failed (exit $?); giving up until next scheduled run"
exit 1
