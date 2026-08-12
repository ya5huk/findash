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
#   scripts/drive.sh probe                            # private connectivity check
#   scripts/drive.sh ensure-layout                    # create fixed vault folders
#   scripts/drive.sh ls [-R] <folder>/               # rclone lsjson [--recursive]
#   scripts/drive.sh pull <remote-path> <local-path> # rclone copyto (download)
#   scripts/drive.sh push <snapshot> <remote-path>   # backup snapshots only
#   scripts/drive.sh move <remote-from> <remote-to>  # rclone moveto
#   scripts/drive.sh delete <remote-file>            # rclone deletefile
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
umask 077

[ -f ./rclone.conf ] && [ ! -L ./rclone.conf ] \
  || { echo "drive.sh: ./rclone.conf missing or unsafe — Drive unavailable" >&2; exit 3; }

ROOT_ID=$(python3 - <<'PY'
import sys
sys.path.insert(0, 'scripts/lib')
import findash_secrets
try:
    value = findash_secrets.read_section('drive').get('root_folder_id', '')
except (OSError, UnicodeError, ValueError):
    sys.exit(1)
print(value if findash_secrets.is_usable_secret(value) else '')
PY
) || { echo "drive.sh: could not read .secrets/findash" >&2; exit 3; }
[ -n "$ROOT_ID" ] || { echo "drive.sh: root_folder_id missing in .secrets/findash [drive]" >&2; exit 3; }

RC=(rclone --config ./rclone.conf --drive-root-folder-id="$ROOT_ID"
    --contimeout 15s --retries 1 --low-level-retries 2)

valid_remote_path() {
  local path="$1"
  [ -n "$path" ] || return 1
  case "$path" in
    /*|../*|*/../*|*/..|..|*$'\n'*|*$'\r'*) return 1 ;;
  esac
  return 0
}

valid_staging_target() {
  local path="$1" name
  case "$path" in
    inbox/staging/drive/*) name="${path#inbox/staging/drive/}" ;;
    "$PWD"/inbox/staging/drive/*) name="${path#"$PWD"/inbox/staging/drive/}" ;;
    *) return 1 ;;
  esac
  [ -n "$name" ] && [[ "$name" != */* ]] && [ "$name" != "." ] && [ "$name" != ".." ] || return 1
  [ -d "$PWD/inbox/staging/drive" ] && [ ! -L "$PWD/inbox" ] \
    && [ ! -L "$PWD/inbox/staging" ] && [ ! -L "$PWD/inbox/staging/drive" ] \
    && [ ! -L "$path" ]
}

valid_backup_source() {
  local path="$1" name
  case "$path" in
    data/*) name="${path#data/}" ;;
    "$PWD"/data/*) name="${path#"$PWD"/data/}" ;;
    *) return 1 ;;
  esac
  # The backup command creates one direct tempfile inside data/. Do not let a
  # crafted path use the permissive shell '*' match to cross a directory.
  [ ! -L "$PWD/data" ] && [ -f "$path" ] && [ ! -L "$path" ] \
    && [[ "$name" != */* ]] \
    && [[ "$name" =~ ^\.finance-backup-[A-Za-z0-9_-]+\.db$ ]]
}

valid_backup_destination() {
  local path="$1"
  [ "$path" = "finance.db" ] && return 0
  [[ "$path" =~ ^backups/finance-[0-9]{4}-[0-9]{2}-[0-9]{2}-[0-9]{6}\.db$ ]]
}

valid_dump_file() {
  local path="$1" name
  case "$path" in
    dump/*) name="${path#dump/}" ;;
    *) return 1 ;;
  esac
  # A sort operation owns exactly one file immediately inside dump/. In
  # particular, reject dump/ itself, nested paths, and directory-looking paths;
  # rclone moveto treats a directory source as a recursive move.
  [ -n "$name" ] && [[ "$name" != */* ]] \
    && [ "$name" != "." ] && [ "$name" != ".." ]
}

valid_filing_destination() {
  local path="$1" folder rest period filename
  case "$path" in
    payslips/*) folder="payslips"; rest="${path#payslips/}" ;;
    investments/*) folder="investments"; rest="${path#investments/}" ;;
    long-term-savings/*) folder="long-term-savings"; rest="${path#long-term-savings/}" ;;
    full-statements/*) folder="full-statements"; rest="${path#full-statements/}" ;;
    fx-conversions/*) folder="fx-conversions"; rest="${path#fx-conversions/}" ;;
    other/*) folder="other"; rest="${path#other/}" ;;
    *) return 1 ;;
  esac
  period="${rest%%/*}"
  filename="${rest#*/}"
  [ "$filename" != "$rest" ] && [ -n "$filename" ] \
    && [[ "$filename" != */* ]] \
    && [ "$filename" != "." ] && [ "$filename" != ".." ] || return 1
  if [ "$folder" = "full-statements" ]; then
    [[ "$period" =~ ^[0-9]{4}-(0[1-9]|1[0-2])$ ]]
  else
    [[ "$period" =~ ^[0-9]{4}$ ]]
  fi
}

CMD="${1:-}"
[ $# -gt 0 ] && shift
case "$CMD" in
  probe)
    [ "$#" -eq 0 ] || { echo "drive.sh probe: no arguments expected" >&2; exit 2; }
    if "${RC[@]}" lsf --max-depth 1 "gdrive:" >/dev/null; then
      echo "Drive probe: ok"
    else
      echo "Drive probe: unavailable" >&2
      exit 1
    fi
    ;;
  ensure-layout)
    [ "$#" -eq 0 ] || { echo "drive.sh ensure-layout: no arguments expected" >&2; exit 2; }
    # Fixed public folder names only. mkdir is idempotent, so this both prepares
    # a new vault and upgrades an older vault when a folder is added.
    for folder in dump payslips investments long-term-savings full-statements fx-conversions other backups; do
      if ! "${RC[@]}" mkdir "gdrive:$folder" >/dev/null; then
        echo "Drive layout: could not prepare fixed folders" >&2
        exit 1
      fi
    done
    echo "Drive layout: ready"
    ;;
  ls)
    if [ "${1:-}" = "-R" ]; then
      shift
      [ "$#" -eq 1 ] || { echo "drive.sh ls: expected one <folder>" >&2; exit 2; }
      remote="${1:?drive.sh ls: missing <folder>}"
      valid_remote_path "$remote" || { echo "drive.sh ls: invalid remote path" >&2; exit 2; }
      exec "${RC[@]}" lsjson --recursive "gdrive:$remote"
    fi
    [ "$#" -eq 1 ] || { echo "drive.sh ls: expected one <folder>" >&2; exit 2; }
    remote="${1:?drive.sh ls: missing <folder>}"
    valid_remote_path "$remote" || { echo "drive.sh ls: invalid remote path" >&2; exit 2; }
    exec "${RC[@]}" lsjson "gdrive:$remote"
    ;;
  pull)
    [ "$#" -eq 2 ] || { echo "drive.sh pull: expected <remote> <local>" >&2; exit 2; }
    remote="${1:?drive.sh pull: missing <remote>}"
    local_path="${2:?drive.sh pull: missing <local>}"
    valid_remote_path "$remote" || { echo "drive.sh pull: invalid remote path" >&2; exit 2; }
    valid_staging_target "$local_path" || { echo "drive.sh pull: local target must be under inbox/staging/drive" >&2; exit 2; }
    "${RC[@]}" copyto "gdrive:$remote" "$local_path" || exit $?
    chmod 600 "$local_path"
    ;;
  push)
    [ "$#" -eq 2 ] || { echo "drive.sh push: expected <snapshot> <remote>" >&2; exit 2; }
    local_path="${1:?drive.sh push: missing <snapshot>}"
    remote="${2:?drive.sh push: missing <remote>}"
    valid_backup_source "$local_path" || { echo "drive.sh push: only a private .finance-backup-*.db snapshot is allowed" >&2; exit 2; }
    valid_backup_destination "$remote" || { echo "drive.sh push: invalid backup destination" >&2; exit 2; }
    if [ "$remote" = "finance.db" ]; then
      exec "${RC[@]}" copyto "$local_path" "gdrive:$remote"
    fi
    # Timestamped archives are immutable evidence. A manual/concurrent retry
    # must fail visibly instead of replacing an existing archive with the same name.
    exec "${RC[@]}" copyto "$local_path" "gdrive:$remote" \
      --ignore-existing --error-on-no-transfer
    ;;
  move)
    [ "$#" -eq 2 ] || { echo "drive.sh move: expected <dump-file> <destination>" >&2; exit 2; }
    from="${1:?drive.sh move: missing <from>}"
    to="${2:?drive.sh move: missing <to>}"
    if ! valid_remote_path "$from" || ! valid_remote_path "$to"; then
      echo "drive.sh move: invalid remote path" >&2
      exit 2
    fi
    valid_dump_file "$from" || { echo "drive.sh move: source must be one file directly under dump/" >&2; exit 2; }
    valid_filing_destination "$to" || { echo "drive.sh move: destination must be a managed category/date/file path" >&2; exit 2; }
    # Never collapse two Drive identities. --ignore-existing leaves the source
    # untouched on every collision (even byte-identical content), while
    # --error-on-no-transfer makes that skip visible to the caller as a failure.
    exec "${RC[@]}" moveto --ignore-existing --error-on-no-transfer "gdrive:$from" "gdrive:$to"
    ;;
  delete)
    [ "$#" -eq 1 ] || { echo "drive.sh delete: expected <remote-file>" >&2; exit 2; }
    remote="${1:?drive.sh delete: missing <remote-file>}"
    valid_backup_destination "$remote" || { echo "drive.sh delete: only timestamped database backups may be deleted" >&2; exit 2; }
    [ "$remote" != "finance.db" ] || { echo "drive.sh delete: finance.db may not be deleted" >&2; exit 2; }
    exec "${RC[@]}" deletefile "gdrive:$remote"
    ;;
  *)
    echo "usage: scripts/drive.sh probe | ensure-layout | ls [-R] <folder>/ | pull <remote> <staging-local> | push <db-snapshot> <backup-remote> | move <dump-file> <to> | delete <backup-file>" >&2
    exit 2
    ;;
esac
