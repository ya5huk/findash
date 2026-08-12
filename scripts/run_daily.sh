#!/bin/bash
# Backward-compatible wrapper for the unattended daily sync. On macOS this is invoked by the
# `com.findash.daily` user LaunchAgent at 10:00 local (template + install steps:
# scripts/com.findash.daily.plist). It's a LaunchAgent rather than cron because
# only a job inside your GUI login session can read the Claude OAuth token from
# the login Keychain — cron runs outside it and `claude -p` fails with "Not
# logged in".
#
# Loads the findash plugin and runs `sync-finance-data` via `claude -p`; on a
# failed attempt, waits then retries exactly once. Reason: morning runs sometimes
# hit a transient Anthropic API socket close right after lid-open, before
# networking has settled — so before each attempt we also poll for connectivity
# (wait_for_network). Both failed attempts are recorded in the private scheduler
# log; dashboard rendering and Telegram delivery are deliberately out of scope.
set -uo pipefail
umask 077

# Run from the repo root regardless of where the job was launched, so the skills'
# cwd-relative paths (scripts/, data/, .secrets/, output/, rclone.conf) resolve
# and --plugin-dir points at the plugin manifest.
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT" || { echo "run_sync: cannot enter the repository root" >&2; exit 1; }

CLAUDE=${CLAUDE_BIN:-$(command -v claude)}
if [ -z "$CLAUDE" ] || [ ! -x "$CLAUDE" ]; then
    echo "run_sync: claude binary not found; set CLAUDE_BIN=/path/to/claude" >&2
    exit 127
fi

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

ATTEMPT_TIMEOUT_SECONDS="${FINDASH_ATTEMPT_TIMEOUT_SECONDS:-1800}"
case "$ATTEMPT_TIMEOUT_SECONDS" in
    ''|*[!0-9]*) echo "run_sync: FINDASH_ATTEMPT_TIMEOUT_SECONDS must be an integer" >&2; exit 2 ;;
esac
if [ "$ATTEMPT_TIMEOUT_SECONDS" -lt 60 ] || [ "$ATTEMPT_TIMEOUT_SECONDS" -gt 7200 ]; then
    echo "run_sync: FINDASH_ATTEMPT_TIMEOUT_SECONDS must be between 60 and 7200" >&2
    exit 2
fi

# Atomic, portable lock: macOS does not ship `flock`. A stale directory is
# reclaimed only when its recorded process no longer exists.
LOCK_DIR="$REPO_ROOT/data/.daily-sync.lock"
[ ! -L "$REPO_ROOT/data" ] || { echo "run_sync: data/ must not be a symlink" >&2; exit 1; }
mkdir -p "$REPO_ROOT/data" || { echo "run_sync: could not create data/" >&2; exit 1; }
acquire_lock() {
    local owner=""
    [ ! -L "$LOCK_DIR" ] || { log "daily-sync lock path is a symlink; refusing"; return 75; }
    if mkdir "$LOCK_DIR" 2>/dev/null; then
        if ! echo "$$" >"$LOCK_DIR/pid"; then
            rmdir "$LOCK_DIR" 2>/dev/null || true
            log "could not write daily-sync lock owner; exiting"
            return 75
        fi
        return 0
    fi
    [ ! -L "$LOCK_DIR/pid" ] || { log "daily-sync lock owner is a symlink; refusing"; return 75; }
    [ -f "$LOCK_DIR/pid" ] && owner="$(sed -n '1p' "$LOCK_DIR/pid" 2>/dev/null)"
    case "$owner" in
        ''|*[!0-9]*) ;;
        *)
            if kill -0 "$owner" 2>/dev/null; then
                log "another daily sync is active (pid $owner); exiting"
                return 75
            fi
            ;;
    esac
    rm -f "$LOCK_DIR/pid" 2>/dev/null || return 75
    rmdir "$LOCK_DIR" 2>/dev/null || return 75
    if mkdir "$LOCK_DIR" 2>/dev/null; then
        if ! echo "$$" >"$LOCK_DIR/pid"; then
            rmdir "$LOCK_DIR" 2>/dev/null || true
            log "could not write daily-sync lock owner; exiting"
            return 75
        fi
        return 0
    fi
    log "could not acquire daily-sync lock; exiting"
    return 75
}

acquire_lock || exit $?

OUTPUT_FILE=""
ACTIVE_CHILD=""
# Invoked by the signal/exit traps below.
# shellcheck disable=SC2329
stop_active_child() {
    if [ -n "$ACTIVE_CHILD" ]; then
        kill -TERM "$ACTIVE_CHILD" 2>/dev/null || true
        wait "$ACTIVE_CHILD" 2>/dev/null || true
        ACTIVE_CHILD=""
    fi
}
# Invoked by the EXIT trap below.
# shellcheck disable=SC2329
cleanup() {
    stop_active_child
    [ -n "$OUTPUT_FILE" ] && rm -f "$OUTPUT_FILE"
    rm -f "$LOCK_DIR/pid" 2>/dev/null || true
    rmdir "$LOCK_DIR" 2>/dev/null || true
}
trap cleanup EXIT
trap 'stop_active_child; exit 130' INT
trap 'stop_active_child; exit 143' TERM

# Wait for outbound connectivity to the Anthropic API before invoking claude. The
# 10:00 job fires right after lid-open, often before networking has settled, which
# surfaces as "API Error: Unable to connect (ConnectionRefused)". Any HTTP reply
# (even 4xx) means the path is up; curl only errors on real connectivity failures
# (DNS / refused / timeout). Polls up to ~5 min, then proceeds regardless.
wait_for_network() {
    local tries=0 max=20
    if ! command -v curl >/dev/null 2>&1; then
        log "curl unavailable; skipping network preflight"
        return 1
    fi
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

run_once() {
    local output_file="$1" status marker_count

    python3 scripts/run_with_timeout.py \
        --seconds "$ATTEMPT_TIMEOUT_SECONDS" \
        --output "$output_file" \
        -- "$CLAUDE" --plugin-dir "$REPO_ROOT" -p "/findash:sync-finance-data" &
    ACTIVE_CHILD=$!
    wait "$ACTIVE_CHILD"
    status=$?
    ACTIVE_CHILD=""
    cat "$output_file"

    if [ "$status" -ne 0 ]; then
        return "$status"
    fi

    marker_count="$(grep -Ec '^FINDASH_SYNC_STATUS=(ok|failed)$' "$output_file" 2>/dev/null || true)"
    if [ "$marker_count" -ne 1 ] || ! grep -Eq '^FINDASH_SYNC_STATUS=ok$' "$output_file"; then
        echo "run_sync: completion marker missing, duplicated, or failed; treating attempt as failed" >&2
        return 1
    fi

    return 0
}

# One private capture file for the whole run, so the final give-up branch can
# inspect the last attempt's output to pick a failure reason. Cleaned on exit.
OUTPUT_FILE="$(mktemp "${TMPDIR:-/tmp}/findash-sync.XXXXXX")" || {
    echo "run_sync: could not create private capture file" >&2
    exit 1
}

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

log "attempt 2 also failed (exit $status); giving up until next scheduled sync"
exit 1
