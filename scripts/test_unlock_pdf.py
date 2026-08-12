"""Unit tests for safe staged-PDF path and password selection."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


MODULE_PATH = Path(__file__).with_name("unlock_pdf.py")
SPEC = importlib.util.spec_from_file_location("unlock_pdf", MODULE_PATH)
assert SPEC and SPEC.loader
unlock_pdf = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(unlock_pdf)


class UnlockPdfTests(unittest.TestCase):
    def test_matches_logical_name_after_drive_prefix(self):
        password = unlock_pdf.select_password(
            "drive-id__2026-payroll.pdf", {"*-payroll.pdf": "secret"}
        )
        self.assertEqual(password, "secret")

    def test_rejects_ambiguous_passwords(self):
        with self.assertRaisesRegex(ValueError, "multiple"):
            unlock_pdf.select_password(
                "payroll.pdf", {"*.pdf": "one", "payroll.*": "two"}
            )

    def test_rejects_placeholder_passwords(self):
        with self.assertRaisesRegex(ValueError, "no .* pattern matched"):
            unlock_pdf.select_password("payroll.pdf", {"*.pdf": "<your password>"})

    def test_rejects_pdf_outside_staging(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outside = root / "outside.pdf"
            outside.write_bytes(b"pdf")
            with self.assertRaisesRegex(ValueError, "under inbox/staging"):
                unlock_pdf.staged_pdf(str(outside), root)

    def test_rejects_symlinked_staging_ancestor(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            root.mkdir()
            outside = Path(tmp) / "outside"
            outside.mkdir()
            target = outside / "private.pdf"
            target.write_bytes(b"pdf")
            (root / "inbox").symlink_to(outside, target_is_directory=True)

            with self.assertRaisesRegex(ValueError, "directories may not be symlinks"):
                unlock_pdf.staged_pdf(str(root / "inbox" / "private.pdf"), root)

    def test_rejects_symlinked_private_output_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            root.mkdir()
            outside = Path(tmp) / "outside"
            outside.mkdir()
            (root / "data").symlink_to(outside, target_is_directory=True)

            with self.assertRaisesRegex(ValueError, "data directories may not be symlinks"):
                unlock_pdf.private_output_dir(root)

    @unittest.skipUnless(shutil.which("qpdf"), "qpdf not installed")
    def test_unlocks_with_password_file_and_cleans_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            staged = root / "inbox" / "staging" / "drive" / "id__2026-payroll.pdf"
            staged.parent.mkdir(parents=True)
            secrets = root / ".secrets" / "findash"
            secrets.parent.mkdir()
            secrets.write_text("[pdf-passwords]\n*-payroll.pdf=test-password\n")
            subprocess.run(
                [
                    shutil.which("qpdf"),
                    "--empty",
                    "--encrypt",
                    "test-password",
                    "owner-password",
                    "256",
                    "--",
                    str(staged),
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            old_root, old_decrypted = unlock_pdf.ROOT, unlock_pdf.DECRYPTED
            unlock_pdf.ROOT = root
            unlock_pdf.DECRYPTED = root / "data" / ".decrypted"
            try:
                output = unlock_pdf.unlock(str(staged))
                check = subprocess.run(
                    [shutil.which("qpdf"), "--requires-password", str(output)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                self.assertEqual(check.returncode, 2)  # qpdf: file is not encrypted
                unlock_pdf.delete_output(str(output))
                self.assertFalse(output.exists())
            finally:
                unlock_pdf.ROOT, unlock_pdf.DECRYPTED = old_root, old_decrypted


if __name__ == "__main__":
    unittest.main()
