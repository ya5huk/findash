"""Tests for safe, path-independent LaunchAgent generation."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import plistlib
import stat
import tempfile
import unittest


MODULE_PATH = Path(__file__).with_name("install_launch_agent.py")
SPEC = importlib.util.spec_from_file_location("install_launch_agent", MODULE_PATH)
assert SPEC and SPEC.loader
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


class InstallLaunchAgentTests(unittest.TestCase):
    def fixture(self, base: Path):
        root = base / "repo & <portable>"
        runner = root / "scripts" / "run_sync.sh"
        runner.parent.mkdir(parents=True)
        runner.write_text("#!/bin/bash\n")
        home = base / "home & person"
        bin_dir = home / ".nvm" / "versions" / "node" / "v22.13.0" / "bin"
        bin_dir.mkdir(parents=True)
        executables = {}
        for name in ("claude", "node", "python3"):
            path = bin_dir / name
            path.write_text("#!/bin/sh\n")
            path.chmod(0o755)
            executables[name] = str(path)
        return root, home, executables

    def test_special_paths_round_trip_through_plist(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, home, executables = self.fixture(Path(tmp))
            config = installer.build_launch_agent(root, home, executables)
            destination = installer.write_launch_agent(config, home)

            with destination.open("rb") as stream:
                loaded = plistlib.load(stream)
            self.assertEqual(loaded["WorkingDirectory"], str(root.resolve()))
            self.assertEqual(
                loaded["ProgramArguments"],
                ["/bin/bash", str(root.resolve() / "scripts" / "run_sync.sh")],
            )
            self.assertIn(str(Path(executables["node"]).parent), loaded["EnvironmentVariables"]["PATH"])
            self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(destination.parent.stat().st_mode), 0o700)
            self.assertEqual(
                stat.S_IMODE((home / "Library" / "Logs" / "findash").stat().st_mode),
                0o700,
            )
            log = home / "Library" / "Logs" / "findash" / "daily.log"
            self.assertTrue(log.is_file())
            self.assertFalse(log.is_symlink())
            self.assertEqual(stat.S_IMODE(log.stat().st_mode), 0o600)

    def test_existing_regular_log_is_preserved_and_hardened(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, home, executables = self.fixture(Path(tmp))
            log = home / "Library" / "Logs" / "findash" / "daily.log"
            log.parent.mkdir(parents=True)
            log.write_text("existing log\n")
            log.chmod(0o666)

            config = installer.build_launch_agent(root, home, executables)
            installer.write_launch_agent(config, home)

            self.assertEqual(log.read_text(), "existing log\n")
            self.assertEqual(stat.S_IMODE(log.stat().st_mode), 0o600)

    def test_symlinked_log_is_rejected_without_touching_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root, home, executables = self.fixture(base)
            victim = base / "victim"
            victim.write_text("keep\n")
            log = home / "Library" / "Logs" / "findash" / "daily.log"
            log.parent.mkdir(parents=True)
            log.symlink_to(victim)

            config = installer.build_launch_agent(root, home, executables)
            with self.assertRaisesRegex(ValueError, "regular file"):
                installer.write_launch_agent(config, home)

            self.assertEqual(victim.read_text(), "keep\n")

    def test_special_log_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, home, executables = self.fixture(Path(tmp))
            log = home / "Library" / "Logs" / "findash" / "daily.log"
            log.parent.mkdir(parents=True)
            os.mkfifo(log)

            config = installer.build_launch_agent(root, home, executables)
            with self.assertRaisesRegex(ValueError, "regular file"):
                installer.write_launch_agent(config, home)

    def test_hardlinked_log_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root, home, executables = self.fixture(base)
            victim = base / "victim"
            victim.write_text("keep\n")
            log = home / "Library" / "Logs" / "findash" / "daily.log"
            log.parent.mkdir(parents=True)
            os.link(victim, log)

            config = installer.build_launch_agent(root, home, executables)
            with self.assertRaisesRegex(ValueError, "regular file"):
                installer.write_launch_agent(config, home)

            self.assertEqual(victim.read_text(), "keep\n")

    def test_symlinked_or_special_log_parent_is_rejected(self):
        for kind in ("symlink", "file"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                base = Path(tmp)
                root, home, executables = self.fixture(base)
                logs = home / "Library" / "Logs"
                logs.mkdir(parents=True)
                log_parent = logs / "findash"
                if kind == "symlink":
                    target = base / "elsewhere"
                    target.mkdir()
                    log_parent.symlink_to(target, target_is_directory=True)
                else:
                    log_parent.write_text("not a directory")

                config = installer.build_launch_agent(root, home, executables)
                with self.assertRaisesRegex(ValueError, "symlink or special file"):
                    installer.write_launch_agent(config, home)

    def test_symlinked_log_ancestor_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root, home, executables = self.fixture(base)
            library = home / "Library"
            library.mkdir()
            outside = base / "outside-logs"
            outside.mkdir()
            (library / "Logs").symlink_to(outside, target_is_directory=True)

            config = installer.build_launch_agent(root, home, executables)
            with self.assertRaisesRegex(ValueError, "symlink or special file"):
                installer.write_launch_agent(config, home)

    def test_missing_claude_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, home, _ = self.fixture(Path(tmp))
            with self.assertRaisesRegex(RuntimeError, "claude is not on PATH"):
                installer.build_launch_agent(root, home, {})

    def test_invalid_schedule_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, home, executables = self.fixture(Path(tmp))
            with self.assertRaisesRegex(ValueError, "valid local"):
                installer.build_launch_agent(root, home, executables, hour=24)


if __name__ == "__main__":
    unittest.main()
