#!/usr/bin/env python3
"""Unlock a staged PDF without exposing its password in commands or logs."""

from __future__ import annotations

import argparse
import fnmatch
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
STAGING = ROOT / "inbox" / "staging"
DECRYPTED = ROOT / "data" / ".decrypted"
sys.path.insert(0, str(ROOT / "scripts" / "lib"))
import findash_secrets  # noqa: E402


def staged_pdf(path: str, root: Path | None = None) -> Path:
    root = ROOT if root is None else root
    if root.is_symlink():
        raise ValueError("private staging root may not be a symlink")
    root = Path(os.path.abspath(root))
    inbox = root / "inbox"
    staging_path = inbox / "staging"
    if inbox.is_symlink() or staging_path.is_symlink():
        raise ValueError("private staging directories may not be symlinks")
    staging = staging_path
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = root / candidate
    candidate = Path(os.path.abspath(candidate))
    if candidate.is_symlink():
        raise ValueError("input must not be a symlink")
    try:
        relative = candidate.relative_to(staging)
    except ValueError as exc:
        raise ValueError("input must be a staged file under inbox/staging") from exc
    current = staging
    for part in relative.parent.parts:
        current /= part
        if current.is_symlink():
            raise ValueError("staged PDF path may not cross a symlink")
    if candidate.suffix.lower() != ".pdf" or not candidate.is_file():
        raise ValueError("input must be a staged PDF")
    return candidate


def select_password(filename: str, passwords: dict[str, str]) -> str:
    # Drive staging prefixes filenames with <drive-id>__. Match both forms so a
    # user's existing basename pattern keeps working after download.
    logical_name = filename.split("__", 1)[1] if "__" in filename else filename
    matches = {
        password
        for pattern, password in passwords.items()
        if findash_secrets.is_usable_secret(password)
        and (fnmatch.fnmatchcase(filename, pattern) or fnmatch.fnmatchcase(logical_name, pattern))
    }
    if not matches:
        raise ValueError("no [pdf-passwords] pattern matched the staged PDF")
    if len(matches) > 1:
        raise ValueError("multiple [pdf-passwords] patterns matched with different passwords")
    return matches.pop()


def private_output_dir(root: Path | None = None) -> Path:
    root = ROOT if root is None else root
    if root.is_symlink():
        raise ValueError("private data root may not be a symlink")
    root = Path(os.path.abspath(root))
    data = root / "data"
    path = data / ".decrypted"
    if data.is_symlink() or path.is_symlink():
        raise ValueError("private data directories may not be symlinks")
    data.mkdir(exist_ok=True, mode=0o700)
    data.chmod(0o700)
    path.mkdir(exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


def unlock(input_path: str) -> Path:
    source = staged_pdf(input_path)
    qpdf = shutil.which("qpdf")
    if not qpdf:
        raise RuntimeError("qpdf is not installed")
    password = select_password(
        source.name, findash_secrets.read_section("pdf-passwords", ROOT / ".secrets" / "findash")
    )
    output_dir = private_output_dir()
    out_fd, out_name = tempfile.mkstemp(prefix="findash-", suffix=".pdf", dir=output_dir)
    password_fd, password_name = tempfile.mkstemp(prefix=".password-", dir=output_dir)
    output = Path(out_name)
    password_path = Path(password_name)
    try:
        os.close(out_fd)
        with os.fdopen(password_fd, "w", encoding="utf-8") as stream:
            stream.write(password)
        os.chmod(output, 0o600)
        os.chmod(password_path, 0o600)
        result = subprocess.run(
            [qpdf, f"--password-file={password_path}", "--decrypt", str(source), str(output)],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if result.returncode != 0:
            raise RuntimeError("qpdf could not unlock the staged PDF")
        os.chmod(output, 0o600)
        return output
    except Exception:
        output.unlink(missing_ok=True)
        raise
    finally:
        password_path.unlink(missing_ok=True)


def delete_output(path: str) -> None:
    data = ROOT / "data"
    if data.is_symlink() or DECRYPTED.is_symlink():
        raise ValueError("private data directories may not be symlinks")
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = ROOT / candidate
    candidate = Path(os.path.abspath(candidate))
    if candidate.parent != Path(os.path.abspath(DECRYPTED)) or not candidate.name.startswith("findash-"):
        raise ValueError("refusing to delete anything outside data/.decrypted")
    if candidate.is_symlink() or candidate.suffix.lower() != ".pdf":
        raise ValueError("refusing unexpected decrypted path")
    candidate.unlink(missing_ok=True)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    unlock_parser = subparsers.add_parser("unlock")
    unlock_parser.add_argument("staged_pdf")
    delete_parser = subparsers.add_parser("delete")
    delete_parser.add_argument("decrypted_pdf")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)
    args = parse_args(argv or sys.argv[1:])
    try:
        if args.command == "unlock":
            output = unlock(args.staged_pdf)
            # The caller runs from the checkout root; a relative private path is
            # sufficient and avoids leaking the local username into transcripts.
            print(output.relative_to(ROOT))
        else:
            delete_output(args.decrypted_pdf)
            print("Decrypted PDF removed")
        return 0
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"unlock_pdf: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
