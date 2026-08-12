#!/bin/bash
# Canonical unattended entry point. It delegates to the established hardened
# wrapper filename so older LaunchAgents keep working.
set -euo pipefail
cd "$(dirname "$0")/.."
exec scripts/run_daily.sh "$@"
