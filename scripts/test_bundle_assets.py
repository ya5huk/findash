"""Tests for pinned vendor-asset integrity and atomic writes."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import tempfile
import unittest


MODULE_PATH = Path(__file__).with_name("bundle-assets.py")
SPEC = importlib.util.spec_from_file_location("bundle_assets", MODULE_PATH)
assert SPEC and SPEC.loader
bundle = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bundle)


class BundleAssetsTests(unittest.TestCase):
    def test_verified_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "asset.js"
            old_hashes = bundle.assets.EXPECTED_SHA256
            bundle.assets.EXPECTED_SHA256 = {
                "asset.js": "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
            }
            try:
                bundle.write_verified(destination, b"hello")
            finally:
                bundle.assets.EXPECTED_SHA256 = old_hashes
            self.assertEqual(destination.read_bytes(), b"hello")

    def test_mismatch_does_not_replace_existing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "asset.js"
            destination.write_bytes(b"existing")
            old_hashes = bundle.assets.EXPECTED_SHA256
            bundle.assets.EXPECTED_SHA256 = {"asset.js": "0" * 64}
            try:
                with self.assertRaisesRegex(ValueError, "integrity check failed"):
                    bundle.write_verified(destination, b"untrusted")
            finally:
                bundle.assets.EXPECTED_SHA256 = old_hashes
            self.assertEqual(destination.read_bytes(), b"existing")

    def test_check_mode_detects_tampered_existing_asset(self):
        with tempfile.TemporaryDirectory() as tmp:
            vendor = Path(tmp)
            old_dir = bundle.VENDOR_DIR
            old_hashes = bundle.assets.EXPECTED_SHA256
            bundle.VENDOR_DIR = vendor
            bundle.assets.EXPECTED_SHA256 = {
                "asset.js": "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
            }
            try:
                (vendor / "asset.js").write_bytes(b"tampered")
                self.assertEqual(bundle.main(["--check"]), 1)
                (vendor / "asset.js").write_bytes(b"hello")
                self.assertEqual(bundle.main(["--check"]), 0)
            finally:
                bundle.VENDOR_DIR = old_dir
                bundle.assets.EXPECTED_SHA256 = old_hashes


if __name__ == "__main__":
    unittest.main()
