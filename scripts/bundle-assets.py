#!/usr/bin/env python3
"""Vendor offline assets for the dashboard (Chart.js, date adapter, fonts).

Run once after setup, and only re-run when pinned versions change. Outputs
land in templates/vendor/. Stdlib-only. Idempotent: re-running overwrites
the same files. Safe to interrupt — files are written only after each
fetch fully succeeds.

Usage:
    python3 scripts/bundle-assets.py
"""

import base64
import re
import sys
import urllib.request
from pathlib import Path

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


def vendor_js(url, dest):
    data = fetch(url)
    dest.write_bytes(data)
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
    dest.write_text(inlined, encoding="utf-8")
    size = len(inlined.encode("utf-8"))
    print(
        f"  {dest.name}  {kb(size)}  "
        f"(embedded {total_fonts} fonts, {kb(total_bytes)} raw)"
    )


def main():
    VENDOR_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Vendoring offline assets into {VENDOR_DIR.relative_to(PROJECT_ROOT)}/")
    vendor_js(CHART_JS_URL, VENDOR_DIR / "chart.umd.min.js")
    vendor_js(ADAPTER_JS_URL, VENDOR_DIR / "chartjs-adapter-date-fns.bundle.min.js")
    vendor_fonts(FONTS_CSS_URL, VENDOR_DIR / "fonts-inline.css")
    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
