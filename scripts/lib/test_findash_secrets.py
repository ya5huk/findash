"""Unit tests for the Python findash secret reader (used by the findash scripts).
Run: python3 scripts/lib/test_findash_secrets.py"""
import os
from pathlib import Path
import tempfile
import unittest

import findash_secrets as fs


class ParseIniTests(unittest.TestCase):
    def test_reads_keys_under_a_section(self):
        ini = fs.parse_ini("[ibkr]\nbase_currency=t\naccount_ids=42\n")
        self.assertEqual(ini["ibkr"], {"base_currency": "t", "account_ids": "42"})

    def test_ignores_blanks_and_comments(self):
        ini = fs.parse_ini("# c\n\n[ibkr]\n; c2\nbase_currency=t\n")
        self.assertEqual(ini["ibkr"], {"base_currency": "t"})

    def test_splits_on_first_equals_only(self):
        ini = fs.parse_ini("[pdf-passwords]\npayslip=a=b=c\n")
        self.assertEqual(ini["pdf-passwords"]["payslip"], "a=b=c")

    def test_accepts_utf8_bom_before_first_section(self):
        ini = fs.parse_ini("\ufeff[drive]\nroot_folder_id=id\n")
        self.assertEqual(ini["drive"], {"root_folder_id": "id"})

    def test_identifies_setup_placeholders(self):
        for value in ("<your token>", "changeme", "YOUR_TOKEN", "replace-me"):
            with self.subTest(value=value):
                self.assertTrue(fs.is_placeholder_value(value))
                self.assertFalse(fs.is_usable_secret(value))
        self.assertTrue(fs.is_usable_secret("actual-value"))


class ReadSectionTests(unittest.TestCase):
    def _write(self, text):
        fd, path = tempfile.mkstemp()
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        self.addCleanup(os.remove, path)
        return path

    def test_reads_the_requested_section(self):
        c = self._write("[ibkr]\nbase_currency=t\naccount_ids=42\n")
        self.assertEqual(fs.read_section("ibkr", c), {"base_currency": "t", "account_ids": "42"})

    def test_absent_section_returns_empty(self):
        c = self._write("[drive]\nroot_folder_id=x\n")
        self.assertEqual(fs.read_section("ibkr", c), {})

    def test_missing_file_returns_empty(self):
        self.assertEqual(fs.read_section("ibkr", "/no/such/findash"), {})

    def test_reads_a_bom_encoded_file(self):
        c = self._write("\ufeff[ibkr]\nbase_currency=t\n")
        self.assertEqual(fs.read_section("ibkr", c), {"base_currency": "t"})

    def test_refuses_symlinked_file(self):
        target = Path(self._write("[ibkr]\nbase_currency=t\n"))
        link = target.with_name(target.name + "-link")
        link.symlink_to(target)
        self.addCleanup(link.unlink)
        with self.assertRaisesRegex(ValueError, "symlink"):
            fs.read_section("ibkr", link)

    def test_refuses_symlinked_parent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            real = root / "real"
            real.mkdir()
            (real / "findash").write_text("[ibkr]\nbase_currency=t\n")
            linked = root / "linked"
            linked.symlink_to(real, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "symlink"):
                fs.read_section("ibkr", linked / "findash")

    def test_refuses_oversized_file(self):
        c = self._write("x" * (fs.MAX_SECRETS_BYTES + 1))
        with self.assertRaisesRegex(ValueError, "too large"):
            fs.read_section("ibkr", c)


if __name__ == "__main__":
    unittest.main()
