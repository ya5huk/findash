"""Black-box tests for unattended-run completion and locking contracts."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


HERE = Path(__file__).resolve().parent


class RunDailyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        scripts = self.root / "scripts"
        (scripts / "lib").mkdir(parents=True)
        (self.root / "data").mkdir()
        shutil.copy2(HERE / "run_daily.sh", scripts / "run_daily.sh")
        shutil.copy2(HERE / "run_with_timeout.py", scripts / "run_with_timeout.py")
        (scripts / "lib" / "findash_secrets.py").write_text(
            "def read_section(section): return {}\n"
            "def is_usable_secret(value): return bool(value) and not value.startswith('<')\n"
        )
        (scripts / "send_telegram.sh").write_text("#!/bin/sh\nexit 0\n")
        (scripts / "send_telegram.sh").chmod(0o755)

        self.bin = self.root / "bin"
        self.bin.mkdir()
        for name in ("curl", "sleep"):
            command = self.bin / name
            command.write_text("#!/bin/sh\nexit 0\n")
            command.chmod(0o755)
        self.claude = self.bin / "claude"
        self.env = os.environ.copy()
        self.env.update(
            {
                "PATH": f"{self.bin}{os.pathsep}{self.env['PATH']}",
                "CLAUDE_BIN": str(self.claude),
                "FINDASH_ATTEMPT_TIMEOUT_SECONDS": "60",
            }
        )

    def run_daily(self):
        return subprocess.run(
            ["bash", str(self.root / "scripts" / "run_daily.sh")],
            text=True,
            capture_output=True,
            env=self.env,
            timeout=10,
        )

    def test_exact_success_marker_completes_and_releases_lock(self):
        self.claude.write_text(
            "#!/bin/sh\n"
            "case \"$*\" in *'/findash:sync-finance-data'*) ;; *) exit 9 ;; esac\n"
            "printf 'FINDASH_SYNC_STATUS=ok\\n'\n"
        )
        self.claude.chmod(0o755)

        result = self.run_daily()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("attempt 1 succeeded", result.stdout)
        self.assertFalse((self.root / "data" / ".daily-sync.lock").exists())

    def test_missing_marker_fails_after_one_retry(self):
        self.claude.write_text("#!/bin/sh\nprintf 'ordinary prose only\\n'\n")
        self.claude.chmod(0o755)

        result = self.run_daily()

        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout.count("ordinary prose only"), 2)
        self.assertIn("attempt 2 also failed", result.stdout)

    def test_live_lock_owner_prevents_second_run(self):
        self.claude.write_text("#!/bin/sh\nprintf 'FINDASH_SYNC_STATUS=ok\\n'\n")
        self.claude.chmod(0o755)
        lock = self.root / "data" / ".daily-sync.lock"
        lock.mkdir()
        (lock / "pid").write_text(str(os.getpid()))

        result = self.run_daily()

        self.assertEqual(result.returncode, 75)
        self.assertIn("another daily sync is active", result.stdout)
        self.assertTrue(lock.exists())

    def test_symlinked_lock_owner_is_refused(self):
        self.claude.write_text("#!/bin/sh\nprintf 'FINDASH_SYNC_STATUS=ok\\n'\n")
        self.claude.chmod(0o755)
        lock = self.root / "data" / ".daily-sync.lock"
        lock.mkdir()
        outside = self.root / "outside-pid"
        outside.write_text(str(os.getpid()))
        (lock / "pid").symlink_to(outside)

        result = self.run_daily()

        self.assertEqual(result.returncode, 75)
        self.assertIn("lock owner is a symlink", result.stdout)
        self.assertEqual(outside.read_text(), str(os.getpid()))

    def test_capture_creation_failure_releases_lock(self):
        self.claude.write_text("#!/bin/sh\nprintf 'FINDASH_SYNC_STATUS=ok\\n'\n")
        self.claude.chmod(0o755)
        fake_mktemp = self.bin / "mktemp"
        fake_mktemp.write_text("#!/bin/sh\nexit 1\n")
        fake_mktemp.chmod(0o755)

        result = self.run_daily()

        self.assertEqual(result.returncode, 1)
        self.assertIn("could not create private capture file", result.stderr)
        self.assertFalse((self.root / "data" / ".daily-sync.lock").exists())


if __name__ == "__main__":
    unittest.main()
