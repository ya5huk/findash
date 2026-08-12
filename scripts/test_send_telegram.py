"""Black-box tests for Telegram dry-run and dashboard freshness behavior."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest


SCRIPT = Path(__file__).with_name("send_telegram.sh")
HTML = """<!doctype html>
<div class="as-of-date">As of 2026-01-02</div>
<div class="headline-amount">₪1,234</div>
"""
NON_BMP = chr(0x1F600)


class SendTelegramTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "scripts").mkdir()
        (self.root / "output").mkdir()
        shutil.copy2(SCRIPT, self.root / "scripts" / "send_telegram.sh")
        self.dashboard = self.root / "output" / "dashboard.html"
        self.dashboard.write_text(HTML)

    def run_script(self, *args: str, env: dict[str, str] | None = None):
        merged = os.environ.copy()
        if env:
            merged.update(env)
        return subprocess.run(
            ["bash", str(self.root / "scripts" / "send_telegram.sh"), *args],
            text=True,
            capture_output=True,
            env=merged,
            timeout=5,
        )

    def test_dry_run_needs_no_secrets(self):
        result = self.run_script("--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("sendDocument finance-2026-01-02.html", result.stdout)
        self.assertNotIn("1,234", result.stdout)

    def test_old_dashboard_is_rejected(self):
        old = time.time() - 3600
        os.utime(self.dashboard, (old, old))
        result = self.run_script("--dry-run", "--max-age-seconds", "60")
        self.assertEqual(result.returncode, 1)
        self.assertIn("not fresh", result.stderr)

    def test_run_marker_cannot_be_bypassed_by_allow_stale(self):
        marker = int(time.time()) + 10
        result = self.run_script(
            "--dry-run",
            "--allow-stale",
            env={"FINDASH_DASHBOARD_NOT_BEFORE_EPOCH": str(marker)},
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("not fresh for this run", result.stderr)

    def test_real_send_path_parses_mocked_api_response(self):
        lib = self.root / "scripts" / "lib"
        lib.mkdir()
        (lib / "findash_secrets.py").write_text(
            "def read_section(section):\n"
            "    return {'bot_token': 'test-token', 'chat_id': 'test-chat'}\n"
            "def is_usable_secret(value): return bool(value) and not value.startswith('<')\n"
        )
        hooks = self.root / "test-hooks"
        hooks.mkdir()
        (hooks / "sitecustomize.py").write_text(
            "import io, urllib.request\n"
            "class Response(io.BytesIO):\n"
            "    def __enter__(self): return self\n"
            "    def __exit__(self, *args): self.close()\n"
            "def fake_urlopen(request, timeout=0):\n"
            "    return Response(b'{\"ok\":true,\"result\":{\"chat\":{\"id\":\"private-id\"}}}')\n"
            "urllib.request.urlopen = fake_urlopen\n"
        )
        result = self.run_script(env={"PYTHONPATH": str(hooks)})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Telegram delivery: Sent", result.stdout)
        self.assertNotIn("private-id", result.stdout)

    def test_missing_option_value_fails_instead_of_looping(self):
        result = self.run_script("--note")
        self.assertEqual(result.returncode, 2)
        self.assertIn("requires a value", result.stderr)

    def test_alert_chunks_non_bmp_text_without_echoing_it(self):
        private_marker = "private-alert-" + NON_BMP * 2049
        result = self.run_script("--dry-run", "--alert", private_marker)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("alert in 2 part(s)", result.stdout)
        self.assertIn("max 4096 UTF-16 units", result.stdout)
        self.assertNotIn("private-alert", result.stdout)
        self.assertNotIn(NON_BMP, result.stdout)

    def test_summary_chunks_non_bmp_text_by_rendered_utf16_units(self):
        data = self.root / "data"
        data.mkdir()
        (data / "last_sync_summary.md").write_text(NON_BMP * 2100)
        result = self.run_script("--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("sync summary in 2 part(s)", result.stdout)
        self.assertIn("max 4096 UTF-16 units", result.stdout)
        self.assertNotIn(NON_BMP, result.stdout)

    def test_caption_truncates_by_utf16_units_without_echoing_note(self):
        private_note = "private-caption-" + NON_BMP * 600
        result = self.run_script("--dry-run", "--note", private_note)
        self.assertEqual(result.returncode, 0, result.stderr)
        # A two-unit character cannot fill the final odd unit before the ellipsis.
        self.assertIn("caption 1023 UTF-16 units", result.stdout)
        self.assertNotIn("private-caption", result.stdout)
        self.assertNotIn(NON_BMP, result.stdout)

    def test_oversized_summary_is_rejected_without_echoing_content(self):
        data = self.root / "data"
        data.mkdir()
        private_marker = b"private-summary-marker"
        (data / "last_sync_summary.md").write_bytes(
            private_marker + b"x" * (256 * 1024)
        )
        result = self.run_script("--dry-run")
        self.assertEqual(result.returncode, 1)
        self.assertIn("file is too large", result.stdout)
        self.assertNotIn(private_marker.decode(), result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
