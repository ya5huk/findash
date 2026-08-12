#!/bin/bash
# Deterministic Node dependency install for the bank scraper.
set -euo pipefail
cd "$(dirname "$0")" || exit 1
umask 077

[ -f package.json ] && [ ! -L package.json ] || {
  echo "install_node_deps: scripts/package.json missing or unsafe" >&2
  exit 1
}
[ -f package-lock.json ] && [ ! -L package-lock.json ] || {
  echo "install_node_deps: scripts/package-lock.json missing or unsafe" >&2
  exit 1
}

exec npm ci
