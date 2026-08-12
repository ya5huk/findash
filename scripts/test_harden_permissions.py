"""Tests for private local-state permissions."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import stat
import tempfile
import unittest


MODULE_PATH = Path(__file__).with_name("harden_permissions.py")
SPEC = importlib.util.spec_from_file_location("harden_permissions", MODULE_PATH)
assert SPEC and SPEC.loader
harden_permissions = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(harden_permissions)


class HardenPermissionsTests(unittest.TestCase):
    def test_creates_private_dirs_and_locks_existing_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            staged = root / "inbox" / "staging" / "fetched" / "statement.json"
            staged.parent.mkdir(parents=True)
            staged.write_text("private")
            os.chmod(staged, 0o644)

            changed, unsafe = harden_permissions.harden(root)

            self.assertGreater(changed, 0)
            self.assertEqual(unsafe, 0)
            self.assertEqual(stat.S_IMODE(staged.stat().st_mode), 0o600)
            for relative in harden_permissions.PRIVATE_DIRS:
                path = root / relative
                self.assertTrue(path.is_dir())
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)

    def test_refuses_symlink_inside_private_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outside = root / "outside"
            outside.write_text("leave me")
            secrets = root / ".secrets"
            secrets.mkdir()
            (secrets / "link").symlink_to(outside)

            _, unsafe = harden_permissions.harden(root)

            self.assertEqual(unsafe, 1)
            self.assertEqual(outside.read_text(), "leave me")

    def test_refuses_symlinked_private_root_without_chmodding_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            root.mkdir()
            outside = Path(tmp) / "outside"
            outside.mkdir()
            private = outside / "statement"
            private.write_text("leave me")
            os.chmod(private, 0o644)
            (root / "inbox").symlink_to(outside, target_is_directory=True)

            _, unsafe = harden_permissions.harden(root)

            self.assertGreaterEqual(unsafe, 1)
            self.assertEqual(stat.S_IMODE(private.stat().st_mode), 0o644)


if __name__ == "__main__":
    unittest.main()
