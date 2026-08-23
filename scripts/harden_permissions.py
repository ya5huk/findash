#!/usr/bin/env python3
"""Create and secure findash's local private-state directories.

Only fixed, gitignored locations are touched.  Filenames and contents are never
printed, which keeps doctor/setup output safe even when staging contains bank
statements.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import stat
import sys


ROOT = Path(__file__).resolve().parents[1]
# Secure the roots recursively so parent directories do not leak private
# filenames, and ensure both staging queues required by setup exist.
PRIVATE_DIRS = (
    "data",
    "data/backups",
    "inbox",
    "inbox/staging/fetched",
    "inbox/staging/drive",
    "inbox/staging/captures",
    "inbox/staging/operations",
    "output",
    ".secrets",
)


def has_symlink_component(base: Path, path: Path) -> bool:
    """Return true when a path at/below a trusted base crosses a symlink."""
    try:
        relative = path.relative_to(base)
    except ValueError:
        return True
    current = base
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            return True
    return False


def secure_tree(path: Path) -> tuple[int, int]:
    """Return ``(changed, unsafe_symlinks)`` for one private tree."""
    changed = 0
    unsafe_symlinks = 0
    if path.is_symlink():
        return changed, 1
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if stat.S_IMODE(path.stat().st_mode) != 0o700:
        path.chmod(0o700)
        changed += 1
    for current, directories, files in os.walk(path, followlinks=False):
        current_path = Path(current)
        for name in list(directories):
            candidate = current_path / name
            if candidate.is_symlink():
                directories.remove(name)
                unsafe_symlinks += 1
                continue
            if stat.S_IMODE(candidate.stat().st_mode) != 0o700:
                candidate.chmod(0o700)
                changed += 1
        for name in files:
            candidate = current_path / name
            if candidate.is_symlink() or not candidate.is_file():
                unsafe_symlinks += 1
                continue
            if stat.S_IMODE(candidate.stat().st_mode) != 0o600:
                candidate.chmod(0o600)
                changed += 1
    return changed, unsafe_symlinks


def harden(root: Path, browser_cache: Path | None = None) -> tuple[int, int]:
    changed = unsafe = 0
    if root.is_symlink():
        return changed, 1
    for relative in PRIVATE_DIRS:
        path = root / relative
        if has_symlink_component(root, path):
            unsafe += 1
            continue
        c, u = secure_tree(path)
        changed += c
        unsafe += u
    if browser_cache is not None and (browser_cache.exists() or browser_cache.is_symlink()):
        home = Path.home()
        if has_symlink_component(home, browser_cache):
            unsafe += 1
        else:
            c, u = secure_tree(browser_cache)
            changed += c
            unsafe += u
    return changed, unsafe


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Secure findash local state to user-only access.")
    parser.add_argument(
        "--include-browser-profile",
        action="store_true",
        help="also audit the optional Chromium cookie/profile tree",
    )
    args = parser.parse_args(argv or sys.argv[1:])
    os.umask(0o077)
    browser_cache = Path.home() / ".cache" / "findash" if args.include_browser_profile else None
    changed, unsafe = harden(ROOT, browser_cache)
    if unsafe:
        print(
            f"Permissions: refused {unsafe} symlink/non-file path(s) in private state",
            file=sys.stderr,
        )
        return 1
    print(f"Permissions: private local state secured ({changed} mode change(s))")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
