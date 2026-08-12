"""Black-box checks for the Drive wrapper's download boundary."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).with_name("drive.sh")


class DriveWrapperTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "scripts" / "lib").mkdir(parents=True)
        (self.root / "inbox" / "staging" / "drive").mkdir(parents=True)
        (self.root / "data").mkdir()
        shutil.copy2(SCRIPT, self.root / "scripts" / "drive.sh")
        (self.root / "scripts" / "lib" / "findash_secrets.py").write_text(
            "def read_section(section):\n"
            "    return {'root_folder_id': 'test-root'} if section == 'drive' else {}\n"
            "def is_usable_secret(value): return bool(value) and not value.startswith('<')\n"
        )
        (self.root / "rclone.conf").write_text("[gdrive]\ntype = drive\n")

        self.bin = self.root / "bin"
        self.bin.mkdir()
        fake = self.bin / "rclone"
        fake.write_text(
            "#!/bin/sh\n"
            "set -eu\n"
            "printf '%s\\n' \"$*\" >>\"$FAKE_RCLONE_LOG\"\n"
            "case \" $* \" in *' lsf '*) printf 'private-name\\n'; exit 0 ;; esac\n"
            "case \" $* \" in *' moveto '*) exit \"${FAKE_RCLONE_MOVE_EXIT:-0}\" ;; esac\n"
            "case \" $* \" in *' mkdir '*) exit 0 ;; esac\n"
            "while [ \"$1\" != copyto ]; do shift; done\n"
            "shift\n"
            "source=$1\n"
            "destination=$2\n"
            "case \"$destination\" in gdrive:*) exit 0 ;; esac\n"
            "printf 'downloaded:%s\\n' \"$source\" >\"$destination\"\n"
        )
        fake.chmod(0o755)
        self.log = self.root / "rclone.log"
        self.env = os.environ.copy()
        self.env["PATH"] = f"{self.bin}{os.pathsep}{self.env['PATH']}"
        self.env["FAKE_RCLONE_LOG"] = str(self.log)

    def run_pull(self, remote: str, destination: Path):
        return subprocess.run(
            [
                "bash",
                str(self.root / "scripts" / "drive.sh"),
                "pull",
                remote,
                str(destination),
            ],
            text=True,
            capture_output=True,
            env=self.env,
            timeout=5,
        )

    def test_probe_suppresses_remote_names(self):
        result = subprocess.run(
            ["bash", str(self.root / "scripts" / "drive.sh"), "probe"],
            text=True,
            capture_output=True,
            env=self.env,
            timeout=5,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "Drive probe: ok\n")
        self.assertNotIn("private-name", result.stdout)
        self.assertIn("lsf --max-depth 1 gdrive:", self.log.read_text())

    def test_backup_named_remote_can_be_downloaded_to_staging(self):
        target = self.root / "inbox" / "staging" / "drive" / "finance.db"

        result = self.run_pull("finance.db", target)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(target.read_text(), "downloaded:gdrive:finance.db\n")
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        self.assertIn("copyto gdrive:finance.db", self.log.read_text())

    def test_pull_cannot_write_outside_staging(self):
        target = self.root / "README.md"

        result = self.run_pull("statements/example.xlsx", target)

        self.assertEqual(result.returncode, 2)
        self.assertFalse(target.exists())
        self.assertFalse(self.log.exists())

    def test_pull_rejects_nested_or_symlink_targets(self):
        nested = self.root / "inbox" / "staging" / "drive" / "nested" / "file"
        result = self.run_pull("statements/example.xlsx", nested)
        self.assertEqual(result.returncode, 2)

        outside = self.root / "outside"
        outside.write_text("keep")
        link = self.root / "inbox" / "staging" / "drive" / "linked"
        link.symlink_to(outside)
        result = self.run_pull("statements/example.xlsx", link)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(outside.read_text(), "keep")
        self.assertFalse(self.log.exists())

    def test_remote_parent_traversal_is_rejected(self):
        target = self.root / "inbox" / "staging" / "drive" / "file"

        result = self.run_pull("backups/../finance.db", target)

        self.assertEqual(result.returncode, 2)
        self.assertFalse(target.exists())
        self.assertFalse(self.log.exists())

    def run_move(self, source: str, destination: str):
        return subprocess.run(
            [
                "bash",
                str(self.root / "scripts" / "drive.sh"),
                "move",
                source,
                destination,
            ],
            text=True,
            capture_output=True,
            env=self.env,
            timeout=5,
        )

    def test_move_uses_no_clobber_flags_for_every_destination_collision(self):
        result = self.run_move("dump/incoming.pdf", "other/2026/document-01.pdf")

        self.assertEqual(result.returncode, 0, result.stderr)
        call = self.log.read_text()
        self.assertIn("moveto --ignore-existing --error-on-no-transfer", call)

    def test_move_surfaces_rclone_collision_as_failure(self):
        self.env["FAKE_RCLONE_MOVE_EXIT"] = "9"

        result = self.run_move("dump/incoming.pdf", "other/2026/document-01.pdf")

        self.assertEqual(result.returncode, 9)

    def test_move_rejects_dump_root_nested_paths_and_extra_arguments(self):
        for source in ("dump/", "dump/subdir/file.pdf", "dump/subdir/"):
            with self.subTest(source=source):
                result = self.run_move(source, "other/2026/document-01.pdf")
                self.assertEqual(result.returncode, 2)
        result = subprocess.run(
            [
                "bash",
                str(self.root / "scripts" / "drive.sh"),
                "move",
                "dump/incoming.pdf",
                "other/2026/document-01.pdf",
                "unexpected",
            ],
            text=True,
            capture_output=True,
            env=self.env,
            timeout=5,
        )
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.log.exists())

    def test_move_accepts_only_managed_category_date_file_destinations(self):
        accepted = (
            "payslips/2026/2026-04-sample-payslip-01.pdf",
            "investments/2026/2026-04-01-broker-statement-01.pdf",
            "long-term-savings/2026/2026-04-01-pension-statement-01.pdf",
            "full-statements/2026-04/2026-04-30-bank-statement-01.xlsx",
            "fx-conversions/2026/2026-04-01-bank-fx-01.png",
            "other/2026/2026-04-01-unknown-01.pdf",
        )
        for destination in accepted:
            with self.subTest(destination=destination):
                result = self.run_move("dump/incoming.pdf", destination)
                self.assertEqual(result.returncode, 0, result.stderr)

        self.log.unlink()
        rejected = (
            "arbitrary/2026/file.pdf",
            "backups/finance-2026-01-01-000000.db",
            "investments/file.pdf",
            "investments/26/file.pdf",
            "investments/2026/nested/file.pdf",
            "full-statements/2026-13/file.xlsx",
        )
        for destination in rejected:
            with self.subTest(destination=destination):
                result = self.run_move("dump/incoming.pdf", destination)
                self.assertEqual(result.returncode, 2)
        self.assertFalse(self.log.exists())

    def test_commands_reject_surplus_arguments_before_calling_rclone(self):
        commands = (
            ("ls", "dump/", "extra"),
            ("ls", "-R", "dump/", "extra"),
            (
                "pull",
                "dump/incoming.pdf",
                str(self.root / "inbox" / "staging" / "drive" / "incoming.pdf"),
                "extra",
            ),
            ("delete", "backups/finance-2026-01-01-000000.db", "extra"),
        )
        for command in commands:
            with self.subTest(command=command):
                result = subprocess.run(
                    ["bash", str(self.root / "scripts" / "drive.sh"), *command],
                    text=True,
                    capture_output=True,
                    env=self.env,
                    timeout=5,
                )
                self.assertEqual(result.returncode, 2)
        self.assertFalse(self.log.exists())

    def test_push_accepts_only_a_direct_generated_snapshot(self):
        snapshot = self.root / "data" / ".finance-backup-abc123.db"
        snapshot.write_bytes(b"sqlite")
        result = subprocess.run(
            [
                "bash",
                str(self.root / "scripts" / "drive.sh"),
                "push",
                str(snapshot),
                "finance.db",
            ],
            text=True,
            capture_output=True,
            env=self.env,
            timeout=5,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

        self.log.unlink()
        nested = self.root / "data" / ".finance-backup-nested"
        nested.mkdir()
        disguised = nested / ".." / snapshot.name
        outside = self.root / ".finance-backup-outside.db"
        outside.write_bytes(b"private")
        for source in (disguised, outside):
            with self.subTest(source=source):
                result = subprocess.run(
                    [
                        "bash",
                        str(self.root / "scripts" / "drive.sh"),
                        "push",
                        str(source),
                        "finance.db",
                    ],
                    text=True,
                    capture_output=True,
                    env=self.env,
                    timeout=5,
                )
                self.assertEqual(result.returncode, 2)
        self.assertFalse(self.log.exists())

    def test_timestamped_backup_archives_are_never_overwritten(self):
        snapshot = self.root / "data" / ".finance-backup-abc123.db"
        snapshot.write_bytes(b"sqlite")
        result = subprocess.run(
            [
                "bash",
                str(self.root / "scripts" / "drive.sh"),
                "push",
                str(snapshot),
                "backups/finance-2026-01-01-000000.db",
            ],
            text=True,
            capture_output=True,
            env=self.env,
            timeout=5,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            "--ignore-existing --error-on-no-transfer",
            self.log.read_text(),
        )

    def test_symlinked_rclone_config_is_rejected_before_remote_access(self):
        target = self.root / "real-rclone.conf"
        target.write_text("[gdrive]\ntype = drive\n")
        (self.root / "rclone.conf").unlink()
        (self.root / "rclone.conf").symlink_to(target)

        result = subprocess.run(
            ["bash", str(self.root / "scripts" / "drive.sh"), "probe"],
            text=True,
            capture_output=True,
            env=self.env,
            timeout=5,
        )
        self.assertEqual(result.returncode, 3)
        self.assertIn("unsafe", result.stderr)
        self.assertFalse(self.log.exists())

    def test_ensure_layout_creates_only_the_fixed_public_folders(self):
        result = subprocess.run(
            ["bash", str(self.root / "scripts" / "drive.sh"), "ensure-layout"],
            text=True,
            capture_output=True,
            env=self.env,
            timeout=5,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "Drive layout: ready\n")
        calls = self.log.read_text().splitlines()
        mkdir_calls = [line for line in calls if " mkdir " in f" {line} "]
        self.assertEqual(len(mkdir_calls), 8)
        for folder in (
            "dump",
            "payslips",
            "investments",
            "long-term-savings",
            "full-statements",
            "fx-conversions",
            "other",
            "backups",
        ):
            self.assertTrue(any(line.endswith(f"mkdir gdrive:{folder}") for line in calls))


if __name__ == "__main__":
    unittest.main()
