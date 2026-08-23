#!/usr/bin/env python3
"""Safe lifecycle operations for files in findash's private staging queue.

``delete`` removes staged files after a successful ingest.  ``save-download``
turns a Google Drive connector download (base64 inside a tool result) into a
real staged file under ``inbox/staging/drive/`` so the bytes never travel
through a shell command line.  Output contains counts only.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
from pathlib import Path
import re
import sys


ROOT = Path(__file__).resolve().parents[1]
STAGING = ROOT / "inbox" / "staging"
DRIVE_ID = re.compile(r"^[A-Za-z0-9_-]{10,128}$")
# Claude Code persists oversized tool results here; small results are written
# into staging by the skill itself. Nothing else is an acceptable source.
TOOL_RESULTS_RELATIVE = Path(".claude") / "projects"


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


def validate_download_source(raw_path: str, root: Path, home: Path) -> Path:
    """Accept only a regular file inside staging or Claude Code's tool-results."""
    root = Path(os.path.abspath(root))
    home = Path(os.path.abspath(home))
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        candidate = root / candidate
    candidate = Path(os.path.abspath(candidate))
    staging = root / "inbox" / "staging"
    tool_results = home / TOOL_RESULTS_RELATIVE
    inside_staging = staging in candidate.parents
    inside_tool_results = tool_results in candidate.parents
    if not (inside_staging or inside_tool_results):
        raise ValueError("download source must be a staged file or a Claude tool result")
    if candidate.is_symlink() or not candidate.is_file():
        raise ValueError("download source must be a regular file")
    return candidate


def decode_download(source_path: Path, expected_id: str) -> bytes:
    """Return the raw bytes from a connector download saved as JSON or bare base64."""
    text = source_path.read_text(encoding="utf-8")
    if text.lstrip().startswith("{"):
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError("download result is not valid JSON") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("content"), str):
            raise ValueError("download result has no content field")
        result_id = payload.get("id")
        if result_id is not None and result_id != expected_id:
            raise ValueError("download result belongs to a different Drive file")
        encoded = payload["content"]
    else:
        encoded = text
    encoded = "".join(encoded.split())
    try:
        return base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("download content is not valid base64") from exc


def save_download(
    source: str,
    drive_id: str,
    filename: str,
    expected_size: int | None = None,
    root: Path = ROOT,
    home: Path | None = None,
) -> int:
    """Decode one connector download into ``inbox/staging/drive/<id>__<filename>``."""
    if not DRIVE_ID.fullmatch(drive_id):
        raise ValueError("invalid Drive file id")
    if (
        not filename
        or filename in (".", "..")
        or filename.startswith(".")
        or any(char in filename for char in "/\\\n\r\0")
    ):
        raise ValueError("invalid staged filename")
    home = home or Path.home()
    source_path = validate_download_source(source, root, home)
    destination = validate_staged_file(
        str(Path(root) / "inbox" / "staging" / "drive" / f"{drive_id}__{filename}"), root
    )
    raw = decode_download(source_path, drive_id)
    if not raw:
        raise ValueError("download content is empty")
    if expected_size is not None and len(raw) != expected_size:
        raise ValueError(
            f"decoded size {len(raw)} does not match the listed size {expected_size}"
        )
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(raw)
    os.chmod(destination, 0o600)
    # A scratch base64 file written into staging has served its purpose.
    staging = Path(os.path.abspath(root)) / "inbox" / "staging"
    if staging in source_path.parents:
        source_path.unlink(missing_ok=True)
    return len(raw)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    delete = subparsers.add_parser("delete")
    delete.add_argument("paths", nargs="+")
    save = subparsers.add_parser("save-download")
    save.add_argument("source", help="tool-result JSON or bare base64 text file")
    save.add_argument("drive_id")
    save.add_argument("filename")
    save.add_argument("--expected-size", type=int, default=None, metavar="BYTES")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    os.umask(0o077)
    try:
        if args.command == "delete":
            count = delete_files(args.paths)
            print(f"Staging: deleted {count} file(s)")
        else:
            size = save_download(
                args.source, args.drive_id, args.filename, args.expected_size
            )
            print(f"Staging: saved 1 file ({size} bytes)")
    except (OSError, ValueError) as exc:
        print(f"staging_files: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
