"""Unit tests for the Python findash secret reader (used by send_telegram.sh).
Run: python3 scripts/lib/test_findash_secrets.py"""
import os
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


if __name__ == "__main__":
    unittest.main()
