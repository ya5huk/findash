#!/bin/bash
# Wrapper for the unattended daily run. On macOS this is invoked by the
# `com.findash.daily` user LaunchAgent at 10:00 local (template + install steps:
# scripts/com.findash.daily.plist). It's a LaunchAgent rather than cron because
# only a job inside your GUI login session can read the Claude OAuth token from
# the login Keychain — cron runs outside it and `claude -p` fails with "Not
# logged in".
#
# Loads the findash plugin and runs its `daily-run` skill via `claude -p`; on a
# failed attempt, waits 60s and retries exactly once. Reason: morning runs
# sometimes hit a transient Anthropic API socket close right after lid-open,
# before networking has settled.
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

run_once() {
    local output_file status
    output_file="$(mktemp "${TMPDIR:-/tmp}/findash-daily.XXXXXX")"

    "$CLAUDE" --plugin-dir "$REPO_ROOT" -p "/findash:daily-run" >"$output_file" 2>&1
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

    if telegram_configured && ! grep -Eiq 'Sent to chat|Telegram delivery status: Sent|Telegram delivery: Sent' "$output_file"; then
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
