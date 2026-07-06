#!/bin/bash
# One front door for every findash → Google Drive operation.
#
# Why this exists: the skills used to repeat the same rclone incantation
# (--config ./rclone.conf --drive-root-folder-id=<ID from .secrets/findash>)
# in four places, and the unattended run's allowlist had to cover raw rclone.
# This wrapper owns the config + root-ID resolution and gives every caller one
# contract: exit 0 = done, anything else = "Drive degraded" — callers add a ⚠️
# warning and continue, never abort (see sync-finance-data SKILL.md).
#
# Fast-fail on network trouble (short connect timeout, single retry) so a Drive
# outage costs the unattended run seconds, not minutes.
#
# Usage:
#   scripts/drive.sh ls [-R] <folder>/               # rclone lsjson [--recursive]
#   scripts/drive.sh pull <remote-path> <local-path> # rclone copyto (download)
#   scripts/drive.sh push <local-path> <remote-path> # rclone copyto (upload)
#   scripts/drive.sh move <remote-from> <remote-to>  # rclone moveto
#   scripts/drive.sh delete <remote-file>            # rclone deletefile
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

[ -f ./rclone.conf ] || { echo "drive.sh: ./rclone.conf missing — Drive unavailable" >&2; exit 3; }

ROOT_ID=$(python3 - <<'PY'
import sys
sys.path.insert(0, 'scripts/lib')
import findash_secrets
print(findash_secrets.read_section('drive').get('root_folder_id', ''))
PY
) || { echo "drive.sh: could not read .secrets/findash" >&2; exit 3; }
[ -n "$ROOT_ID" ] || { echo "drive.sh: root_folder_id missing in .secrets/findash [drive]" >&2; exit 3; }

RC=(rclone --config ./rclone.conf --drive-root-folder-id="$ROOT_ID"
    --contimeout 15s --retries 1 --low-level-retries 2)

CMD="${1:-}"
[ $# -gt 0 ] && shift
case "$CMD" in
  ls)
    if [ "${1:-}" = "-R" ]; then
      shift
      exec "${RC[@]}" lsjson --recursive "gdrive:${1:?drive.sh ls: missing <folder>}"
    fi
    exec "${RC[@]}" lsjson "gdrive:${1:?drive.sh ls: missing <folder>}"
    ;;
  pull)   exec "${RC[@]}" copyto "gdrive:${1:?drive.sh pull: missing <remote>}" "${2:?drive.sh pull: missing <local>}" ;;
  push)   exec "${RC[@]}" copyto "${1:?drive.sh push: missing <local>}" "gdrive:${2:?drive.sh push: missing <remote>}" ;;
  move)   exec "${RC[@]}" moveto "gdrive:${1:?drive.sh move: missing <from>}" "gdrive:${2:?drive.sh move: missing <to>}" ;;
  delete) exec "${RC[@]}" deletefile "gdrive:${1:?drive.sh delete: missing <remote-file>}" ;;
  *)
    echo "usage: scripts/drive.sh ls [-R] <folder>/ | pull <remote> <local> | push <local> <remote> | move <from> <to> | delete <remote-file>" >&2
    exit 2
    ;;
esac
