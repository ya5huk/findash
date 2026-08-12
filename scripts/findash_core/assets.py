"""Pinned integrity checks for executable dashboard vendor assets."""

from __future__ import annotations

import hashlib
from pathlib import Path


# Review and update these only when intentionally updating a pinned dependency.
EXPECTED_SHA256 = {
    "chart.umd.min.js": "0e2326c6868072bec1592760c6729043caeea2960a2b46cee6a2192aac6abff0",
    "chartjs-adapter-date-fns.bundle.min.js": "ea7ab30d26c38dcf1f2d26bb43e73a94537b58f1906f55e1a546dd09321b5615",
    "fonts-inline.css": "e2300a335133f9c06111a16eb5238862a38a5455c67582b078f60c1354e82d85",
}


class AssetIntegrityError(ValueError):
    """Raised when a required offline asset is absent or has changed."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def invalid_vendor_assets(vendor_dir: Path) -> tuple[str, ...]:
    """Return public asset names that are missing, unsafe, or unverified."""
    vendor_dir = Path(vendor_dir)
    if vendor_dir.is_symlink():
        return ("vendor directory",)
    invalid = []
    for name, expected in EXPECTED_SHA256.items():
        path = vendor_dir / name
        if not path.is_file() or path.is_symlink() or sha256_file(path) != expected:
            invalid.append(name)
    return tuple(invalid)


def require_vendor_assets(vendor_dir: Path) -> None:
    invalid = invalid_vendor_assets(vendor_dir)
    if invalid:
        raise AssetIntegrityError(
            "offline dashboard assets are missing or failed integrity verification: "
            + ", ".join(invalid)
            + "; run `python3 scripts/bundle-assets.py`"
        )
