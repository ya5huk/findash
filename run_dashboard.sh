#!/bin/bash
# Run the private localhost API and React dashboard together.
set -uo pipefail
umask 077

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT" || exit 1

UI_PORT="${FINDASH_DASHBOARD_PORT:-4173}"
API_PORT="${FINDASH_DASHBOARD_API_PORT:-8765}"
for value in "$UI_PORT" "$API_PORT"; do
  case "$value" in
    ''|*[!0-9]*) echo "dashboard ports must be integers" >&2; exit 2 ;;
  esac
  [ "$value" -ge 1 ] && [ "$value" -le 65535 ] || {
    echo "dashboard ports must be between 1 and 65535" >&2
    exit 2
  }
done

if [ ! -x dashboard/node_modules/.bin/vite ]; then
  echo "dashboard dependencies missing; run scripts/install_dashboard_deps.sh" >&2
  exit 1
fi

API_PID=""
cleanup() {
  if [ -n "$API_PID" ]; then
    kill -TERM "$API_PID" 2>/dev/null || true
    wait "$API_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

python3 scripts/dashboard_server.py --port "$API_PORT" &
API_PID=$!

echo "FinDash live dashboard: http://127.0.0.1:$UI_PORT"
echo "Market data refresh: intraday quotes every 5 minutes, full refresh every 30 minutes"
FINDASH_DASHBOARD_API_PORT="$API_PORT" \
  npm --prefix dashboard run dev -- --host 127.0.0.1 --port "$UI_PORT"
