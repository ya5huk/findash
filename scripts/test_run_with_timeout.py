"""Tests for the portable unattended-sync timeout helper."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest


MODULE_PATH = Path(__file__).with_name("run_with_timeout.py")
SPEC = importlib.util.spec_from_file_location("run_with_timeout", MODULE_PATH)
assert SPEC and SPEC.loader
run_with_timeout = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(run_with_timeout)


class RunWithTimeoutTests(unittest.TestCase):
    def test_captures_successful_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "out"
            status = run_with_timeout.run(
                [sys.executable, "-c", "print('done')"], output, 5
            )
            self.assertEqual(status, 0)
            self.assertEqual(output.read_text(), "done\n")

    def test_times_out_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "out"
            status = run_with_timeout.run(
                [sys.executable, "-c", "import time; time.sleep(30)"], output, 1
            )
            self.assertEqual(status, 124)
            self.assertIn("timed out after 1s", output.read_text())

    def test_reports_missing_command_in_capture(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "out"
            status = run_with_timeout.run(["/definitely/not/a/command"], output, 1)
            self.assertEqual(status, 127)
            self.assertIn("could not start command", output.read_text())

    def test_sigterm_stops_the_entire_command_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "out"
            child_pid = root / "child.pid"
            helper = subprocess.Popen(
                [
                    sys.executable,
                    str(MODULE_PATH),
                    "--seconds",
                    "30",
                    "--output",
                    str(output),
                    "--",
                    sys.executable,
                    "-c",
                    (
                        "import os,time,pathlib; "
                        f"pathlib.Path({str(child_pid)!r}).write_text(str(os.getpid())); "
                        "time.sleep(30)"
                    ),
                ]
            )
            self.addCleanup(lambda: helper.poll() is None and helper.kill())
            deadline = time.monotonic() + 5
            while not child_pid.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(child_pid.exists(), "child process never started")
            pid = int(child_pid.read_text())

            helper.send_signal(signal.SIGTERM)
            self.assertEqual(helper.wait(timeout=5), 128 + signal.SIGTERM)
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)
            self.assertIn("interrupted by SIGTERM", output.read_text())


if __name__ == "__main__":
    unittest.main()
