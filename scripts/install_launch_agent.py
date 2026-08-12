#!/usr/bin/env python3
"""Safely render the public macOS LaunchAgent template for this checkout.

This writes the plist but deliberately does not load or restart the job.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import plistlib
import shutil
import stat
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "scripts" / "com.findash.daily.plist"
COMMANDS = ("claude", "node", "python3", "rclone", "qpdf", "curl")
STANDARD_PATHS = ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin")


def discover_executables() -> dict[str, str]:
    found = {name: path for name in COMMANDS if (path := shutil.which(name))}
    if "claude" not in found:
        raise RuntimeError("claude is not on PATH; install or activate it before scheduling")
    return found


def executable_path(executables: dict[str, str]) -> str:
    directories: list[str] = []

    def add(path: Path) -> None:
        value = str(path)
        if value and ":" not in value and value not in directories:
            directories.append(value)

    for command in COMMANDS:
        candidate = executables.get(command)
        if not candidate:
            continue
        path = Path(candidate).expanduser().absolute()
        add(path.parent)
        try:
            add(path.resolve(strict=True).parent)
        except OSError:
            pass
    for value in STANDARD_PATHS:
        add(Path(value))
    return ":".join(directories)


def build_launch_agent(
    project_root: Path,
    home: Path,
    executables: dict[str, str],
    *,
    hour: int = 10,
    minute: int = 0,
    template: Path = TEMPLATE,
) -> dict:
    project_root = Path(project_root).resolve()
    home = Path(home).resolve()
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError("schedule must be a valid local hour and minute")
    runner = project_root / "scripts" / "run_sync.sh"
    if not runner.is_file() or runner.is_symlink():
        raise ValueError("scripts/run_sync.sh is missing or unsafe")
    claude = executables.get("claude")
    if not claude:
        raise RuntimeError("claude is not on PATH; install or activate it before scheduling")
    claude_path = Path(claude).expanduser().absolute()
    if not claude_path.is_file():
        raise RuntimeError("the discovered claude executable no longer exists")

    with Path(template).open("rb") as stream:
        config = plistlib.load(stream)
    if config.get("Label") != "com.findash.daily":
        raise ValueError("unexpected LaunchAgent template label")

    log = home / "Library" / "Logs" / "findash" / "daily.log"
    config["ProgramArguments"] = ["/bin/bash", str(runner)]
    config["WorkingDirectory"] = str(project_root)
    config["EnvironmentVariables"] = {
        "CLAUDE_BIN": str(claude_path),
        "HOME": str(home),
        "PATH": executable_path(executables),
    }
    config["StartCalendarInterval"] = {"Hour": hour, "Minute": minute}
    config["StandardOutPath"] = str(log)
    config["StandardErrorPath"] = str(log)
    return config


def secure_directory(path: Path, root: Path) -> None:
    """Create a private directory without traversing symlinked components."""
    path = Path(os.path.abspath(path))
    root = Path(os.path.abspath(root))
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise ValueError("LaunchAgent directory must stay under the home directory") from exc

    checked: list[Path] = []
    current = root
    for part in relative.parts:
        current /= part
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            try:
                current.mkdir(mode=0o700)
            except FileExistsError:
                pass
            except OSError as exc:
                raise ValueError("LaunchAgent directory could not be created safely") from exc
            try:
                info = os.lstat(current)
            except OSError as exc:
                raise ValueError("LaunchAgent directory could not be verified") from exc
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError("LaunchAgent directory path contains a symlink or special file")
        checked.append(current)

    # Re-check the chain after creation so an unsafe replacement is not silently
    # accepted between component checks.
    for component in checked:
        try:
            info = os.lstat(component)
        except OSError as exc:
            raise ValueError("LaunchAgent directory could not be verified") from exc
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError("LaunchAgent directory path contains a symlink or special file")

    final_info = os.lstat(path)
    if final_info.st_uid != os.geteuid():
        raise ValueError("LaunchAgent destination directory is not owned by the current user")
    path.chmod(0o700)


def secure_log_file(path: Path) -> None:
    """Create or validate the private regular log without following links."""
    path = Path(path)
    try:
        initial = os.lstat(path)
    except FileNotFoundError:
        initial = None

    if initial is not None and (
        not stat.S_ISREG(initial.st_mode)
        or initial.st_nlink != 1
        or initial.st_uid != os.geteuid()
    ):
        raise ValueError("LaunchAgent log must be an owned, unlinked regular file")

    flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    if initial is None:
        flags |= os.O_CREAT | os.O_EXCL
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise ValueError("LaunchAgent log could not be opened safely") from exc

    try:
        opened = os.fstat(descriptor)
        current = os.lstat(path)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or opened.st_uid != os.geteuid()
            or not stat.S_ISREG(current.st_mode)
            or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
        ):
            raise ValueError("LaunchAgent log changed or is not a safe regular file")
        os.fchmod(descriptor, 0o600)
    finally:
        os.close(descriptor)


def write_launch_agent(config: dict, home: Path) -> Path:
    home = Path(home).resolve()
    log_dir = home / "Library" / "Logs" / "findash"
    agent_dir = home / "Library" / "LaunchAgents"
    secure_directory(log_dir, home)
    secure_log_file(log_dir / "daily.log")
    secure_directory(agent_dir, home)
    destination = agent_dir / "com.findash.daily.plist"
    if destination.is_symlink():
        raise ValueError("LaunchAgent destination may not be a symlink")

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".com.findash.daily.", suffix=".plist", dir=agent_dir
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            plistlib.dump(config, stream, fmt=plistlib.FMT_XML, sort_keys=False)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, destination)
        destination.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hour", type=int, default=10, help="local hour, 0-23 (default: 10)")
    parser.add_argument("--minute", type=int, default=0, help="minute, 0-59 (default: 0)")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    os.umask(0o077)
    try:
        config = build_launch_agent(
            ROOT,
            Path.home(),
            discover_executables(),
            hour=args.hour,
            minute=args.minute,
        )
        write_launch_agent(config, Path.home())
    except OSError:
        print("LaunchAgent install failed: a required local path is missing or unsafe", file=sys.stderr)
        return 1
    except (RuntimeError, ValueError, plistlib.InvalidFileException) as exc:
        print(f"LaunchAgent install failed: {exc}", file=sys.stderr)
        return 1
    print("LaunchAgent installed: ~/Library/LaunchAgents/com.findash.daily.plist")
    print("Load it when ready with the launchctl bootstrap command in docs/setup.md.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
