#!/usr/bin/env python3
"""Safely copy rclone's default config into this checkout.

The source remains in place for other rclone users.  The destination is created
atomically at mode 600 and is never overwritten.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = Path.home() / ".config" / "rclone" / "rclone.conf"
DESTINATION = ROOT / "rclone.conf"


def copy_config(source: Path, destination: Path) -> None:
    if source.is_symlink() or not source.is_file():
        raise ValueError("default rclone config is missing or is not a regular file")
    if destination.exists() or destination.is_symlink():
        raise FileExistsError("project rclone.conf already exists; refusing to overwrite it")
    if destination.parent.is_symlink():
        raise ValueError("project root may not be a symlink")

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".rclone.conf.", suffix=".tmp", dir=destination.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as output, source.open("rb") as input_stream:
            shutil.copyfileobj(input_stream, output)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, 0o600)
        # Hard-linking fails atomically if the destination appeared meanwhile.
        os.link(temporary, destination)
        os.chmod(destination, 0o600)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    os.umask(0o077)
    try:
        copy_config(DEFAULT_SOURCE, DESTINATION)
    except (FileExistsError, OSError, ValueError) as exc:
        print(f"rclone config copy failed: {exc}", file=sys.stderr)
        return 1
    print("rclone config copied into this checkout (source preserved; mode 600)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
