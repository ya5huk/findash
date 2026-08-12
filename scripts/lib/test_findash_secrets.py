"""Unit tests for the Python findash secret reader (used by send_telegram.sh).
Run: python3 scripts/lib/test_findash_secrets.py"""
import os
from pathlib import Path
import tempfile
import unittest

import findash_secrets as fs


class ParseIniTests(unittest.TestCase):
    def test_reads_keys_under_a_section(self):
        ini = fs.parse_ini("[telegram]\nbot_token=t\nchat_id=42\n")
        self.assertEqual(ini["telegram"], {"bot_token": "t", "chat_id": "42"})

    def test_ignores_blanks_and_comments(self):
        ini = fs.parse_ini("# c\n\n[telegram]\n; c2\nbot_token=t\n")
        self.assertEqual(ini["telegram"], {"bot_token": "t"})

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
        c = self._write("[telegram]\nbot_token=t\nchat_id=42\n")
        self.assertEqual(fs.read_section("telegram", c), {"bot_token": "t", "chat_id": "42"})

    def test_absent_section_returns_empty(self):
        c = self._write("[drive]\nroot_folder_id=x\n")
        self.assertEqual(fs.read_section("telegram", c), {})

    def test_missing_file_returns_empty(self):
        self.assertEqual(fs.read_section("telegram", "/no/such/findash"), {})

    def test_reads_a_bom_encoded_file(self):
        c = self._write("\ufeff[telegram]\nbot_token=t\n")
        self.assertEqual(fs.read_section("telegram", c), {"bot_token": "t"})

    def test_refuses_symlinked_file(self):
        target = Path(self._write("[telegram]\nbot_token=t\n"))
        link = target.with_name(target.name + "-link")
        link.symlink_to(target)
        self.addCleanup(link.unlink)
        with self.assertRaisesRegex(ValueError, "symlink"):
            fs.read_section("telegram", link)

    def test_refuses_symlinked_parent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            real = root / "real"
            real.mkdir()
            (real / "findash").write_text("[telegram]\nbot_token=t\n")
            linked = root / "linked"
            linked.symlink_to(real, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "symlink"):
                fs.read_section("telegram", linked / "findash")

    def test_refuses_oversized_file(self):
        c = self._write("x" * (fs.MAX_SECRETS_BYTES + 1))
        with self.assertRaisesRegex(ValueError, "too large"):
            fs.read_section("telegram", c)


if __name__ == "__main__":
    unittest.main()
