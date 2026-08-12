"""JSON serialization safe for embedding in an HTML ``<script>`` element."""

from __future__ import annotations

import json
from typing import Any


def json_for_script(value: Any) -> str:
    """Serialize ``value`` without allowing data to terminate its script tag.

    Escaping ``<`` blocks ``</script>`` and HTML comment openers. ``>``, ``&``,
    and JavaScript's two line-separator characters are escaped as defense in
    depth. The result remains valid JSON and round-trips through ``json.loads``.
    """

    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return (
        payload.replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


__all__ = ["json_for_script"]
