"""Tests for privacy-safe, placeholder-aware config readiness."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import tempfile
import unittest


MODULE_PATH = Path(__file__).with_name("check_config.py")
SPEC = importlib.util.spec_from_file_location("check_config", MODULE_PATH)
assert SPEC and SPEC.loader
check_config = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(check_config)


class CheckConfigTests(unittest.TestCase):
    def test_missing_file_reports_every_integration_unconfigured(self):
        with tempfile.TemporaryDirectory() as tmp:
            status = check_config.configuration_status(Path(tmp) / "findash")
        self.assertFalse(status["secrets_file"])
        self.assertFalse(any(status["configured"].values()))

    def test_requires_complete_non_placeholder_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "findash"
            path.write_text(
                "\ufeff[drive]\nroot_folder_id=<folder id>\n"
                "[hapoalim]\nuser_code=user\npassword=real\n"
                "[cal]\nusername=user\npassword=changeme\n"
                "[pdf-passwords]\n*.pdf=<password>\n",
                encoding="utf-8",
            )
            status = check_config.configuration_status(path)["configured"]
        self.assertFalse(status["drive"])
        self.assertTrue(status["hapoalim"])
        self.assertFalse(status["cal"])
        self.assertFalse(status["pdf-passwords"])

    def test_refuses_symlinked_configuration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            target.write_text("[drive]\nroot_folder_id=id\n")
            link = root / "findash"
            link.symlink_to(target)
            with self.assertRaisesRegex(ValueError, "symlink"):
                check_config.configuration_status(link)


if __name__ == "__main__":
    unittest.main()
