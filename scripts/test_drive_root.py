"""Tests for drive_root.py — the only sanctioned way skills learn the vault id."""

from __future__ import annotations

import importlib.util
import io
from pathlib import Path
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock


MODULE_PATH = Path(__file__).with_name("drive_root.py")
SPEC = importlib.util.spec_from_file_location("drive_root", MODULE_PATH)
assert SPEC and SPEC.loader
drive_root = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(drive_root)


class DriveRootTests(unittest.TestCase):
    def _run(self, section: dict[str, str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(
            drive_root.findash_secrets, "read_section", return_value=section
        ), redirect_stdout(out), redirect_stderr(err):
            code = drive_root.main()
        return code, out.getvalue(), err.getvalue()

    def test_prints_only_a_usable_folder_id(self):
        code, out, err = self._run({"root_folder_id": "1AbCdEfGhIjKlMnOpQrStUvWxYz_-"})
        self.assertEqual(code, 0)
        self.assertEqual(out, "1AbCdEfGhIjKlMnOpQrStUvWxYz_-\n")
        self.assertEqual(err, "")

    def test_rejects_missing_placeholder_and_malformed_ids(self):
        for section in (
            {},
            {"root_folder_id": ""},
            {"root_folder_id": "<paste your vault folder ID here>"},
            {"root_folder_id": "short"},
            {"root_folder_id": "has spaces in it and more"},
        ):
            with self.subTest(section=section):
                code, out, err = self._run(section)
                self.assertEqual(code, 1)
                self.assertEqual(out, "")
                self.assertIn("drive_root:", err)
                for value in section.values():
                    self.assertNotIn(value or "\0", err)

    def test_unreadable_secrets_file_fails_closed(self):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(
            drive_root.findash_secrets, "read_section", side_effect=OSError("denied")
        ), redirect_stdout(out), redirect_stderr(err):
            self.assertEqual(drive_root.main(), 1)
        self.assertEqual(out.getvalue(), "")
        self.assertIn("could not read", err.getvalue())


if __name__ == "__main__":
    unittest.main()
