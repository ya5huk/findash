"""Tests for staging_files.py path confinement."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import tempfile
import unittest


MODULE_PATH = Path(__file__).with_name("staging_files.py")
SPEC = importlib.util.spec_from_file_location("staging_files", MODULE_PATH)
assert SPEC and SPEC.loader
staging_files = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(staging_files)


class StagingFilesTests(unittest.TestCase):
    def test_deletes_only_staged_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            staged = root / "inbox" / "staging" / "drive" / "doc.pdf"
            staged.parent.mkdir(parents=True)
            staged.write_bytes(b"private")
            self.assertEqual(staging_files.delete_files([str(staged)], root), 1)
            self.assertFalse(staged.exists())

    def test_rejects_outside_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outside = root / "outside"
            outside.write_text("keep")
            with self.assertRaisesRegex(ValueError, "outside"):
                staging_files.delete_files([str(outside)], root)
            self.assertTrue(outside.exists())

    def test_rejects_dotdot_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            staging = root / "inbox" / "staging" / "drive"
            staging.mkdir(parents=True)
            outside = root / "outside"
            outside.write_text("keep")
            escaped = staging / ".." / ".." / ".." / "outside"
            with self.assertRaisesRegex(ValueError, "outside"):
                staging_files.delete_files([str(escaped)], root)
            self.assertTrue(outside.exists())

    def test_refuses_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            target.write_text("keep")
            link = root / "inbox" / "staging" / "drive" / "link"
            link.parent.mkdir(parents=True)
            link.symlink_to(target)
            with self.assertRaisesRegex(ValueError, "non-symlink"):
                staging_files.delete_files([str(link)], root)
            self.assertTrue(target.exists())

    def test_refuses_symlinked_staging_ancestor(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            root.mkdir()
            outside = Path(tmp) / "outside"
            outside.mkdir()
            target = outside / "keep"
            target.write_text("keep")
            (root / "inbox").symlink_to(outside, target_is_directory=True)

            with self.assertRaisesRegex(ValueError, "directories may not be symlinks"):
                staging_files.delete_files([str(root / "inbox" / "keep")], root)
            self.assertEqual(target.read_text(), "keep")

    def test_refuses_symlinked_directory_inside_staging(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            staging = root / "inbox" / "staging"
            real = staging / "real"
            real.mkdir(parents=True)
            target = real / "keep"
            target.write_text("keep")
            link = staging / "linked"
            link.symlink_to(real, target_is_directory=True)

            with self.assertRaisesRegex(ValueError, "may not cross a symlink"):
                staging_files.delete_files([str(link / "keep")], root)
            self.assertEqual(target.read_text(), "keep")

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO files are not supported")
    def test_refuses_non_regular_staged_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fifo = root / "inbox" / "staging" / "drive" / "queue"
            fifo.parent.mkdir(parents=True)
            os.mkfifo(fifo)

            with self.assertRaisesRegex(ValueError, "staged file"):
                staging_files.delete_files([str(fifo)], root)
            self.assertTrue(fifo.exists())


if __name__ == "__main__":
    unittest.main()
