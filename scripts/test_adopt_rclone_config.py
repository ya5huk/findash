"""Tests for safe, non-destructive rclone config adoption."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import stat
import tempfile
import unittest


MODULE_PATH = Path(__file__).with_name("adopt_rclone_config.py")
SPEC = importlib.util.spec_from_file_location("adopt_rclone_config", MODULE_PATH)
assert SPEC and SPEC.loader
adopt = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(adopt)


class AdoptRcloneConfigTests(unittest.TestCase):
    def test_copies_without_moving_and_sets_private_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.conf"
            destination = root / "checkout" / "rclone.conf"
            destination.parent.mkdir()
            source.write_text("[gdrive]\ntoken=private\n")

            adopt.copy_config(source, destination)

            self.assertTrue(source.exists())
            self.assertEqual(destination.read_text(), source.read_text())
            self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)

    def test_refuses_to_overwrite_existing_destination(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.conf"
            destination = root / "rclone.conf"
            source.write_text("new")
            destination.write_text("existing")
            with self.assertRaises(FileExistsError):
                adopt.copy_config(source, destination)
            self.assertEqual(destination.read_text(), "existing")

    def test_refuses_source_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            real = root / "real"
            source = root / "source"
            destination = root / "destination"
            real.write_text("private")
            source.symlink_to(real)
            with self.assertRaisesRegex(ValueError, "regular file"):
                adopt.copy_config(source, destination)


if __name__ == "__main__":
    unittest.main()
