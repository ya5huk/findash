#!/usr/bin/env python3
"""Vendor offline assets for the dashboard (Chart.js, date adapter, fonts).

Run once after setup, and only re-run when pinned versions change. Outputs
land in templates/vendor/. Stdlib-only. Idempotent: re-running overwrites
the same files. Safe to interrupt — files are written only after each
fetch fully succeeds.

Usage:
    python3 scripts/bundle-assets.py [--check]
"""

import argparse
import base64
import hashlib
import os
import re
import sys
import tempfile
import urllib.request
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
from findash_core import assets

PROJECT_ROOT = Path(__file__).resolve().parent.parent
VENDOR_DIR = PROJECT_ROOT / "templates" / "vendor"

CHART_JS_URL = (
    "https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"
)
ADAPTER_JS_URL = (
    "https://cdn.jsdelivr.net/npm/"
    "chartjs-adapter-date-fns@3.0.0/dist/"
    "chartjs-adapter-date-fns.bundle.min.js"
)

# Same families/weights the template was loading from Google Fonts.
FONTS_CSS_URL = (
    "https://fonts.googleapis.com/css2"
    "?family=EB+Garamond:ital,wght@0,400;0,500;0,700;1,400"
    "&family=Cormorant+Garamond:ital,wght@0,500;0,700"
    "&display=swap"
)

# Google Fonts varies its CSS by User-Agent. A modern desktop Chrome UA
# gets us woff2 (smallest, widely supported); without it we may end up
# with legacy formats and unicode-range slicing.
MODERN_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

FONT_URL_PATTERN = re.compile(r"url\((https://fonts\.gstatic\.com/[^)]+)\)")

# Each @font-face block in Google's CSS is preceded by a /* subset */ comment.
# The dashboard only renders Latin glyphs (Hebrew is intentionally rendered with
# system fallback fonts — see docs/design-system.md), so dropping cyrillic,
# greek, vietnamese, etc. saves ~1 MB without changing what the user sees.
FONT_BLOCK_PATTERN = re.compile(
    r"/\*\s*([a-z-]+)\s*\*/\s*(@font-face\s*\{[^}]*\})",
    re.DOTALL,
)
KEEP_SUBSETS = {"latin", "latin-ext"}


def filter_to_latin(css):
    kept = []
    for subset, block in FONT_BLOCK_PATTERN.findall(css):
        if subset in KEEP_SUBSETS:
            kept.append(f"/* {subset} */\n{block}")
    return "\n\n".join(kept) + "\n"


def fetch(url, ua=None):
    req = urllib.request.Request(url)
    if ua:
        req.add_header("User-Agent", ua)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def kb(n):
    return f"{n / 1024:.1f} KB"


def write_verified(dest, data):
    expected = assets.EXPECTED_SHA256[dest.name]
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected:
        raise ValueError(
            f"integrity check failed for {dest.name}: expected {expected}, got {actual}"
        )
    dest.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{dest.name}.", suffix=".tmp", dir=dest.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, dest)
    finally:
        temporary.unlink(missing_ok=True)


def vendor_js(url, dest):
    data = fetch(url)
    write_verified(dest, data)
    print(f"  {dest.name}  {kb(len(data))}")


def vendor_fonts(css_url, dest):
    css = fetch(css_url, ua=MODERN_UA).decode("utf-8")
    css = filter_to_latin(css)

    cache = {}
    total_fonts = 0
    total_bytes = 0

    def repl(match):
        nonlocal total_fonts, total_bytes
        font_url = match.group(1)
        if font_url not in cache:
            data = fetch(font_url, ua=MODERN_UA)
            ext = font_url.rsplit(".", 1)[-1].lower()
            mime = "font/woff2" if ext == "woff2" else "font/woff"
            b64 = base64.b64encode(data).decode("ascii")
            cache[font_url] = f"data:{mime};base64,{b64}"
            total_fonts += 1
            total_bytes += len(data)
        return f"url({cache[font_url]})"

    inlined = FONT_URL_PATTERN.sub(repl, css)
    data = inlined.encode("utf-8")
    write_verified(dest, data)
    size = len(data)
    print(
        f"  {dest.name}  {kb(size)}  "
        f"(embedded {total_fonts} fonts, {kb(total_bytes)} raw)"
    )


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify existing assets without downloading or changing files",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    if args.check:
        try:
            assets.require_vendor_assets(VENDOR_DIR)
        except assets.AssetIntegrityError as exc:
            print(f"Vendor assets: not ready ({exc})", file=sys.stderr)
            return 1
        print(f"Vendor assets: verified ({len(assets.EXPECTED_SHA256)})")
        return 0
    if VENDOR_DIR.parent.is_symlink() or VENDOR_DIR.is_symlink():
        raise ValueError("templates and templates/vendor must not be symlinks")
    VENDOR_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Vendoring offline assets into {VENDOR_DIR.relative_to(PROJECT_ROOT)}/")
    vendor_js(CHART_JS_URL, VENDOR_DIR / "chart.umd.min.js")
    vendor_js(ADAPTER_JS_URL, VENDOR_DIR / "chartjs-adapter-date-fns.bundle.min.js")
    vendor_fonts(FONTS_CSS_URL, VENDOR_DIR / "fonts-inline.css")
    assets.require_vendor_assets(VENDOR_DIR)
    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
