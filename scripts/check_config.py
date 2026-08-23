#!/usr/bin/env python3
"""Report privacy-safe integration readiness from the fixed secrets file."""

from __future__ import annotations

import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "lib"))
import findash_secrets  # noqa: E402


REQUIRED_KEYS = {
    "drive": ("root_folder_id",),
    "hapoalim": ("user_code", "password"),
    "cal": ("username", "password"),
}
OPTIONAL_VALUE_SECTIONS = ("pdf-passwords", "ibkr")


def configuration_status(path: Path) -> dict:
    path = Path(path)
    exists = path.exists() and path.is_file() and not path.is_symlink()
    parsed = findash_secrets.read_ini(path)
    sections = {}
    for section, keys in REQUIRED_KEYS.items():
        values = parsed.get(section, {})
        sections[section] = all(
            findash_secrets.is_usable_secret(values.get(key)) for key in keys
        )
    for section in OPTIONAL_VALUE_SECTIONS:
        values = parsed.get(section, {})
        sections[section] = any(
            findash_secrets.is_usable_secret(value) for value in values.values()
        )
    return {"secrets_file": exists, "configured": sections}


def main() -> int:
    try:
        status = configuration_status(ROOT / ".secrets" / "findash")
    except (OSError, UnicodeError, ValueError) as exc:
        print(f"Configuration check failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(status, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
