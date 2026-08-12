"""Read findash secrets from the consolidated ``.secrets/findash`` file.

The setup guide intentionally starts with human-readable placeholders.  Callers
must never mistake those placeholders for configured credentials.
"""

from __future__ import annotations

import os
from pathlib import Path
import stat


MAX_SECRETS_BYTES = 1024 * 1024


_PLACEHOLDER_WORDS = {
    "changeme",
    "change-me",
    "replace-me",
    "replace_me",
    "todo",
    "your-id",
    "your-value",
    "your_chat_id",
    "your_password",
    "your_root_folder_id",
    "your_token",
    "your_user_code",
    "your_username",
    "your_value",
}


def is_placeholder_value(value):
    """Return whether *value* is one of the setup-template placeholders."""
    normalized = str(value or "").strip()
    if not normalized:
        return False
    lowered = normalized.casefold()
    return (
        (normalized.startswith("<") and normalized.endswith(">"))
        or lowered in _PLACEHOLDER_WORDS
    )


def is_usable_secret(value):
    """Return true only for a non-empty, non-placeholder value."""
    return bool(str(value or "").strip()) and not is_placeholder_value(value)


def parse_ini(text):
    """Parse a minimal INI: ``[section]`` headers and ``key=value`` lines, with
    ``#`` / ``;`` comments. Lines before the first header live under the ''
    (default) section."""
    text = text.removeprefix("\ufeff")
    out = {"": {}}
    section = ""
    for raw in text.split("\n"):
        line = raw.strip()
        if not line or line[0] in "#;":
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            out.setdefault(section, {})
            continue
        if "=" in line:
            key, value = line.split("=", 1)
            out[section][key.strip()] = value.strip()
    return out


def read_ini(path=".secrets/findash"):
    """Read the fixed private INI with bounded, no-follow file semantics."""
    candidate = Path(path)
    if candidate.parent.is_symlink() or candidate.is_symlink():
        raise ValueError("private configuration path may not be a symlink")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(candidate, flags)
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise ValueError("private configuration could not be opened safely") from exc

    with os.fdopen(descriptor, "rb") as stream:
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("private configuration must be a regular file")
        if metadata.st_size > MAX_SECRETS_BYTES:
            raise ValueError("private configuration is too large")
        raw = stream.read(MAX_SECRETS_BYTES + 1)
    if len(raw) > MAX_SECRETS_BYTES:
        raise ValueError("private configuration is too large")
    return parse_ini(raw.decode("utf-8-sig"))


def read_section(section, path=".secrets/findash"):
    """Return ``{key: value}`` for one section; an absent file yields ``{}``."""
    return read_ini(path).get(section, {})
