#!/usr/bin/env python3
"""Safe lifecycle operations for files in findash's private staging queue."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
STAGING = ROOT / "inbox" / "staging"


def validate_staged_file(raw_path: str, root: Path = ROOT) -> Path:
    if root.is_symlink():
        raise ValueError("private staging root may not be a symlink")
    root = Path(os.path.abspath(root))
    inbox = root / "inbox"
    staging = inbox / "staging"
    if inbox.is_symlink() or staging.is_symlink():
        raise ValueError("private staging directories may not be symlinks")
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        candidate = root / candidate
    # Collapse dot components without resolving filesystem links. Every parent
    # below staging is checked explicitly before a final unlink is allowed.
    candidate = Path(os.path.abspath(candidate))
    try:
        relative = candidate.relative_to(staging)
    except ValueError as exc:
        raise ValueError("path is outside inbox/staging") from exc
    current = staging
    for part in relative.parent.parts:
        current /= part
        if current.is_symlink():
            raise ValueError("staged path may not cross a symlink")
    if (
        candidate == staging
        or candidate.is_symlink()
        or (candidate.exists() and not candidate.is_file())
    ):
        raise ValueError("path must be a non-symlink staged file")
    return candidate


def delete_files(paths: list[str], root: Path = ROOT) -> int:
    candidates = [validate_staged_file(path, root) for path in paths]
    deleted = 0
    for candidate in candidates:
        try:
            candidate.unlink()
            deleted += 1
        except FileNotFoundError:
            pass
    return deleted


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    delete = subparsers.add_parser("delete")
    delete.add_argument("paths", nargs="+")
    args = parser.parse_args(argv or sys.argv[1:])
    os.umask(0o077)
    try:
        count = delete_files(args.paths)
    except (OSError, ValueError) as exc:
        print(f"staging_files: {exc}", file=sys.stderr)
        return 1
    print(f"Staging: deleted {count} file(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
