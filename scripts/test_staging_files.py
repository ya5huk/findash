"""Tests for staging_files.py path confinement and connector-download decoding."""

from __future__ import annotations

import base64
import importlib.util
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest


MODULE_PATH = Path(__file__).with_name("staging_files.py")
SPEC = importlib.util.spec_from_file_location("staging_files", MODULE_PATH)
assert SPEC and SPEC.loader
staging_files = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(staging_files)

DRIVE_ID = "1AbCdEfGhIjKlMnOpQrStUvWxYz"
PRIVATE_BYTES = b"%PDF-1.4 private statement bytes"


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


class SaveDownloadTests(unittest.TestCase):
    def _layout(self, tmp: str) -> tuple[Path, Path]:
        root = Path(tmp) / "repo"
        home = Path(tmp) / "home"
        (root / "inbox" / "staging" / "drive").mkdir(parents=True)
        (home / ".claude" / "projects" / "x" / "tool-results").mkdir(parents=True)
        return root, home

    def test_decodes_persisted_tool_result_and_keeps_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, home = self._layout(tmp)
            result = home / ".claude" / "projects" / "x" / "tool-results" / "r.txt"
            result.write_text(
                json.dumps(
                    {
                        "content": base64.b64encode(PRIVATE_BYTES).decode(),
                        "id": DRIVE_ID,
                        "mimeType": "application/pdf",
                        "title": "statement.pdf",
                    }
                )
            )

            size = staging_files.save_download(
                str(result), DRIVE_ID, "statement.pdf", len(PRIVATE_BYTES), root, home
            )

            staged = root / "inbox" / "staging" / "drive" / f"{DRIVE_ID}__statement.pdf"
            self.assertEqual(size, len(PRIVATE_BYTES))
            self.assertEqual(staged.read_bytes(), PRIVATE_BYTES)
            self.assertEqual(stat.S_IMODE(staged.stat().st_mode), 0o600)
            self.assertTrue(result.exists())

    def test_decodes_bare_base64_scratch_file_and_removes_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, home = self._layout(tmp)
            scratch = root / "inbox" / "staging" / "drive" / "download.b64"
            encoded = base64.b64encode(PRIVATE_BYTES).decode()
            scratch.write_text(encoded[:10] + "\n" + encoded[10:] + "\n")

            size = staging_files.save_download(
                str(scratch), DRIVE_ID, "statement.pdf", None, root, home
            )

            staged = root / "inbox" / "staging" / "drive" / f"{DRIVE_ID}__statement.pdf"
            self.assertEqual(size, len(PRIVATE_BYTES))
            self.assertEqual(staged.read_bytes(), PRIVATE_BYTES)
            self.assertFalse(scratch.exists())

    def test_rejects_result_for_another_file_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, home = self._layout(tmp)
            scratch = root / "inbox" / "staging" / "drive" / "download.json"
            scratch.write_text(
                json.dumps(
                    {"content": base64.b64encode(PRIVATE_BYTES).decode(), "id": "1OtherFileIdXYZ"}
                )
            )
            with self.assertRaisesRegex(ValueError, "different Drive file"):
                staging_files.save_download(
                    str(scratch), DRIVE_ID, "statement.pdf", None, root, home
                )
            self.assertEqual(list((root / "inbox" / "staging" / "drive").glob("1A*")), [])

    def test_rejects_size_mismatch_without_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, home = self._layout(tmp)
            scratch = root / "inbox" / "staging" / "drive" / "download.b64"
            scratch.write_text(base64.b64encode(PRIVATE_BYTES).decode())
            with self.assertRaisesRegex(ValueError, "does not match the listed size"):
                staging_files.save_download(
                    str(scratch), DRIVE_ID, "statement.pdf", len(PRIVATE_BYTES) + 1, root, home
                )
            self.assertEqual(list((root / "inbox" / "staging" / "drive").glob("1A*")), [])
            self.assertTrue(scratch.exists())

    def test_rejects_invalid_base64_and_empty_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, home = self._layout(tmp)
            scratch = root / "inbox" / "staging" / "drive" / "download.b64"
            scratch.write_text("not*base64")
            with self.assertRaisesRegex(ValueError, "not valid base64"):
                staging_files.save_download(str(scratch), DRIVE_ID, "a.pdf", None, root, home)
            scratch.write_text(json.dumps({"content": "", "id": DRIVE_ID}))
            with self.assertRaisesRegex(ValueError, "empty"):
                staging_files.save_download(str(scratch), DRIVE_ID, "a.pdf", None, root, home)

    def test_rejects_sources_outside_staging_and_tool_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, home = self._layout(tmp)
            elsewhere = Path(tmp) / "elsewhere.json"
            elsewhere.write_text(json.dumps({"content": "AAAA", "id": DRIVE_ID}))
            with self.assertRaisesRegex(ValueError, "staged file or a Claude tool result"):
                staging_files.save_download(str(elsewhere), DRIVE_ID, "a.pdf", None, root, home)
            link = root / "inbox" / "staging" / "drive" / "link.json"
            link.symlink_to(elsewhere)
            with self.assertRaisesRegex(ValueError, "regular file"):
                staging_files.save_download(str(link), DRIVE_ID, "a.pdf", None, root, home)
            self.assertTrue(elsewhere.exists())

    def test_rejects_bad_ids_and_filenames(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, home = self._layout(tmp)
            scratch = root / "inbox" / "staging" / "drive" / "download.b64"
            scratch.write_text(base64.b64encode(PRIVATE_BYTES).decode())
            with self.assertRaisesRegex(ValueError, "invalid Drive file id"):
                staging_files.save_download(str(scratch), "../x", "a.pdf", None, root, home)
            for bad in ("", "..", "../escape.pdf", "nested/a.pdf", ".hidden", "a\nb"):
                with self.subTest(filename=bad):
                    with self.assertRaisesRegex(ValueError, "invalid staged filename"):
                        staging_files.save_download(
                            str(scratch), DRIVE_ID, bad, None, root, home
                        )
            self.assertTrue(scratch.exists())


if __name__ == "__main__":
    unittest.main()
