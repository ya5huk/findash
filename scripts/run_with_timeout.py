#!/usr/bin/env python3
"""Run one command with a portable timeout and terminate its process group."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import signal
import subprocess
import sys


class ForwardedSignal(Exception):
    def __init__(self, signum: int):
        self.signum = signum


def stop_process_group(process: subprocess.Popen[bytes], first_signal: int) -> None:
    """Forward a stop signal, then guarantee the command group is gone."""
    try:
        os.killpg(process.pid, first_signal)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.seconds < 1:
        parser.error("--seconds must be positive")
    if args.command and args.command[0] == "--":
        args.command = args.command[1:]
    if not args.command:
        parser.error("a command is required after --")
    return args


def run(command: list[str], output_path: Path, seconds: int) -> int:
    os.umask(0o077)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("wb") as output:
        os.chmod(output_path, 0o600)
        try:
            process = subprocess.Popen(
                command,
                stdout=output,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError:
            output.write(b"run_sync: could not start command\n")
            return 127

        previous_handlers = {
            signum: signal.getsignal(signum)
            for signum in (signal.SIGINT, signal.SIGTERM)
        }

        def forward_signal(signum, _frame):
            raise ForwardedSignal(signum)

        for signum in previous_handlers:
            signal.signal(signum, forward_signal)
        try:
            try:
                return process.wait(timeout=seconds)
            except subprocess.TimeoutExpired:
                output.write(f"\nrun_sync: attempt timed out after {seconds}s\n".encode())
                output.flush()
                stop_process_group(process, signal.SIGTERM)
                return 124
            except ForwardedSignal as exc:
                name = signal.Signals(exc.signum).name
                output.write(f"\nrun_sync: attempt interrupted by {name}\n".encode())
                output.flush()
                stop_process_group(process, exc.signum)
                return 128 + exc.signum
        finally:
            for signum, handler in previous_handlers.items():
                signal.signal(signum, handler)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    return run(args.command, args.output, args.seconds)


if __name__ == "__main__":
    raise SystemExit(main())
