"""Public-checkout checks for the deterministic Node installer."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).with_name("install_node_deps.sh")


class InstallNodeDepsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.scripts = self.root / "scripts"
        self.scripts.mkdir()
        self.installer = self.scripts / "install_node_deps.sh"
        shutil.copy2(SCRIPT, self.installer)
        (self.scripts / "package.json").write_text('{"private": true}\n')
        (self.scripts / "package-lock.json").write_text(
            '{"name": "findash-scripts", "lockfileVersion": 3}\n'
        )
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.log = self.root / "npm.log"
        fake_npm = self.bin / "npm"
        fake_npm.write_text(
            "#!/bin/sh\n"
            "printf '%s|%s\\n' \"$PWD\" \"$*\" >\"$FAKE_NPM_LOG\"\n"
        )
        fake_npm.chmod(0o755)
        self.env = os.environ.copy()
        self.env["PATH"] = f"{self.bin}{os.pathsep}{self.env['PATH']}"
        self.env["FAKE_NPM_LOG"] = str(self.log)

    def run_installer(self):
        return subprocess.run(
            [str(self.installer)],
            text=True,
            capture_output=True,
            env=self.env,
            timeout=5,
        )

    def test_runs_npm_ci_from_scripts_directory(self):
        result = self.run_installer()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.log.read_text(), f"{self.scripts}|ci\n")

    def test_missing_lockfile_fails_before_npm(self):
        (self.scripts / "package-lock.json").unlink()

        result = self.run_installer()

        self.assertEqual(result.returncode, 1)
        self.assertIn("package-lock.json missing or unsafe", result.stderr)
        self.assertFalse(self.log.exists())

    def test_symlinked_manifest_is_rejected(self):
        (self.scripts / "package.json").unlink()
        outside = self.root / "outside-package.json"
        outside.write_text("{}\n")
        (self.scripts / "package.json").symlink_to(outside)

        result = self.run_installer()

        self.assertEqual(result.returncode, 1)
        self.assertIn("package.json missing or unsafe", result.stderr)
        self.assertFalse(self.log.exists())


if __name__ == "__main__":
    unittest.main()
