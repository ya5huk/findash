#!/usr/bin/env python3
"""Print the configured Drive vault root folder id and nothing else.

Skills drive the Google Drive connector with explicit folder ids.  The vault
root id lives in the private secrets file, which skills may not read directly,
so this is the one sanctioned way to surface it.  A missing, placeholder, or
malformed value exits non-zero without printing a value.
"""

from __future__ import annotations

from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))

import findash_secrets  # noqa: E402

FOLDER_ID = re.compile(r"^[A-Za-z0-9_-]{10,128}$")


def root_folder_id() -> str:
    """Return the usable vault root id or raise ``ValueError``."""
    try:
        value = findash_secrets.read_section("drive").get("root_folder_id", "")
    except (OSError, UnicodeError, ValueError) as exc:
        raise ValueError("could not read .secrets/findash") from exc
    if not findash_secrets.is_usable_secret(value):
        raise ValueError("root_folder_id missing in .secrets/findash [drive]")
    if not FOLDER_ID.fullmatch(value):
        raise ValueError("root_folder_id in .secrets/findash [drive] is not a Drive folder id")
    return value


def main() -> int:
    try:
        print(root_folder_id())
    except ValueError as exc:
        print(f"drive_root: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
