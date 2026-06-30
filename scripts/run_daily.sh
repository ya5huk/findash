#!/bin/bash
# Wrapper for the unattended daily run. On macOS this is invoked by the
# `com.findash.daily` user LaunchAgent at 10:00 local (template + install steps:
# scripts/com.findash.daily.plist). It's a LaunchAgent rather than cron because
# only a job inside your GUI login session can read the Claude OAuth token from
# the login Keychain — cron runs outside it and `claude -p` fails with "Not
# logged in".
#
# Loads the findash plugin and runs its `daily-run` skill via `claude -p`; on a
# failed attempt, waits then retries exactly once. Reason: morning runs sometimes
# hit a transient Anthropic API socket close right after lid-open, before
# networking has settled — so before each attempt we also poll for connectivity
# (wait_for_network). If both attempts fail, a one-line Telegram alert is sent
# (notify_failure) so a broken run — expired login, lost workspace trust, a stuck
# approval — doesn't fail silently for days.
set -uo pipefail

# Run from the repo root regardless of where the job was launched, so the skills'
# cwd-relative paths (scripts/, data/, .secrets/, output/, rclone.conf) resolve
# and --plugin-dir points at the plugin manifest.
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT" || { echo "run_daily: cannot cd to repo root '$REPO_ROOT'" >&2; exit 1; }

CLAUDE=${CLAUDE_BIN:-$(command -v claude)}
if [ -z "$CLAUDE" ]; then
    echo "run_daily: claude binary not found; set CLAUDE_BIN=/path/to/claude" >&2
    exit 127
fi

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

# Wait for outbound connectivity to the Anthropic API before invoking claude. The
# 10:00 job fires right after lid-open, often before networking has settled, which
# surfaces as "API Error: Unable to connect (ConnectionRefused)". Any HTTP reply
# (even 4xx) means the path is up; curl only errors on real connectivity failures
# (DNS / refused / timeout). Polls up to ~5 min, then proceeds regardless.
wait_for_network() {
    local tries=0 max=20
    while [ "$tries" -lt "$max" ]; do
        if curl -s -o /dev/null -m 5 https://api.anthropic.com/ 2>/dev/null; then
            [ "$tries" -gt 0 ] && log "network ready after ~$((tries * 15))s"
            return 0
        fi
        tries=$((tries + 1))
        log "network not ready (probe $tries/$max); waiting 15s"
        sleep 15
    done
    log "network still unreachable after ~$((max * 15))s; proceeding anyway"
    return 1
}

# Telegram is configured only when both bot_token and chat_id actually resolve —
# matching send_telegram.sh's own value gate (a present-but-empty [telegram]
# section makes send_telegram.sh skip with exit 0, which must not be treated as a
# failed send). Reuses the shared parser; never prints the values.
telegram_configured() {
    python3 - <<'PY' 2>/dev/null
import sys
sys.path.insert(0, 'scripts/lib')
try:
    import findash_secrets as fs
    s = fs.read_section('telegram')  # .secrets/findash [telegram]
    sys.exit(0 if (s.get('bot_token') and s.get('chat_id')) else 1)
except Exception:
    sys.exit(1)
PY
}

# Best-effort one-line Telegram alert when the whole run gives up. Maps the last
# attempt's captured output to a short, actionable cause so you know what to do
# (re-auth, re-trust, etc.) without opening the log. Never fatal — a failed or
# unconfigured send must not change the run's exit status.
notify_failure() {
    local out_file="$1" reason stamp
    if grep -qiE 'not been trusted|hasTrustDialogAccepted' "$out_file" 2>/dev/null; then
        reason="workspace not trusted — Claude is ignoring the project allowlist, so the scrapers can't run. Open Claude in the repo once and accept the trust dialog."
    elif grep -qiE 'Not logged in|Please run /login' "$out_file" 2>/dev/null; then
        reason="Claude is not logged in — open Claude Code interactively and run /login to refresh the token."
    elif grep -qiE 'Unable to connect|ConnectionRefused|ECONNREFUSED' "$out_file" 2>/dev/null; then
        reason="couldn't reach the Anthropic API (network not ready at run time)."
    elif grep -qiE 'requires approval|hard-denied|permission sandbox blocked|commands are hard-denied' "$out_file" 2>/dev/null; then
        reason="a command needed manual approval and the run stalled."
    else
        reason="see ~/Library/Logs/findash/daily.log for details."
    fi
    stamp="$(date '+%Y-%m-%d %H:%M')"
    log "sending failure alert to Telegram"
    scripts/send_telegram.sh --alert "⚠️ findash daily run failed ($stamp): $reason" \
        || log "failure alert not delivered (Telegram unconfigured or send failed)"
}

run_once() {
    local output_file="$1" status

    "$CLAUDE" --plugin-dir "$REPO_ROOT" -p "/findash:daily-run" >"$output_file" 2>&1
    status=$?
    cat "$output_file"

    if [ "$status" -ne 0 ]; then
        return "$status"
    fi

    if grep -Eiq 'could not complete|dashboard was not rendered|not rendered or sent|requires approval|hard-denied|permission sandbox blocked|commands are hard-denied' "$output_file"; then
        echo "run_daily: Claude reported a blocker; treating attempt as failed" >&2
        return 1
    fi

    if telegram_configured && ! grep -Eiq 'Sent to chat|Telegram delivery status: Sent|Telegram delivery: Sent' "$output_file"; then
        echo "run_daily: Telegram send confirmation missing; treating attempt as failed" >&2
        return 1
    fi

    return 0
}

# One persistent capture file for the whole run, so the final give-up branch can
# inspect the last attempt's output to pick a failure reason. Cleaned on exit.
OUTPUT_FILE="$(mktemp "${TMPDIR:-/tmp}/findash-daily.XXXXXX")"
trap 'rm -f "$OUTPUT_FILE"' EXIT

wait_for_network

log "attempt 1"
run_once "$OUTPUT_FILE"
status=$?
if [ "$status" -eq 0 ]; then
    log "attempt 1 succeeded"
    exit 0
fi

log "attempt 1 failed (exit $status); sleeping 120s then retrying"
sleep 120
wait_for_network

log "attempt 2"
run_once "$OUTPUT_FILE"
status=$?
if [ "$status" -eq 0 ]; then
    log "attempt 2 succeeded"
    exit 0
fi

log "attempt 2 also failed (exit $status); giving up until next scheduled run"
notify_failure "$OUTPUT_FILE"
exit 1
