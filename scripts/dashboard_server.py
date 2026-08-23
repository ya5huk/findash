#!/usr/bin/env python3
"""Private localhost API for the live FinDash React application."""

from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading

from findash_core.dashboard_live import build_live_dashboard_snapshot
from findash_core.db import connect_db
from findash_core.errors import AccountingError
from findash_core.migrations import MigrationError


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "finance.db"
REFRESH_SCRIPT = ROOT / "scripts" / "refresh_prices.py"


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


class MarketRefreshService:
    def __init__(self, interval_seconds: int = 1800, *, initial: bool = True):
        if interval_seconds < 60:
            raise ValueError("refresh interval must be at least 60 seconds")
        self.interval_seconds = interval_seconds
        self.initial = initial
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._status = {
            "running": False,
            "last_started_at": None,
            "last_finished_at": None,
            "last_ok": None,
            "interval_seconds": interval_seconds,
        }

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, name="findash-market-refresh", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=5)

    def request_refresh(self) -> None:
        self._wake.set()

    def status(self) -> dict:
        with self._lock:
            return dict(self._status)

    def _refresh(self) -> None:
        with self._lock:
            self._status.update(running=True, last_started_at=_utc_now())
        result = subprocess.run(
            [sys.executable, str(REFRESH_SCRIPT), "--range", "1mo"],
            cwd=ROOT,
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=300,
            check=False,
        )
        with self._lock:
            self._status.update(
                running=False,
                last_finished_at=_utc_now(),
                last_ok=result.returncode == 0,
            )

    def _run(self) -> None:
        if not self.initial:
            self._wake.wait(self.interval_seconds)
            self._wake.clear()
        while not self._stop.is_set():
            try:
                self._refresh()
            except (OSError, subprocess.SubprocessError):
                with self._lock:
                    self._status.update(
                        running=False,
                        last_finished_at=_utc_now(),
                        last_ok=False,
                    )
            if self._stop.is_set():
                return
            self._wake.wait(self.interval_seconds)
            self._wake.clear()


class DashboardRequestHandler(BaseHTTPRequestHandler):
    server_version = "FinDash/1"
    refresh_service: MarketRefreshService
    database: Path

    def log_message(self, format: str, *args) -> None:
        return

    def _json(self, payload: dict, status: HTTPStatus = HTTPStatus.OK) -> None:
        encoded = json.dumps(
            payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Cache-Control", "no-store, private")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(encoded)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            self.close_connection = True
            print(
                "Dashboard browser disconnected before the response completed; "
                "continuing.",
                file=sys.stderr,
            )

    def do_GET(self) -> None:
        if self.path == "/api/status":
            self._json({"refresh": self.refresh_service.status()})
            return
        if self.path != "/api/dashboard":
            self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            return
        try:
            payload = build_live_dashboard_snapshot(
                self.database, date.today().isoformat()
            )
        except (AccountingError, MigrationError, OSError, sqlite3.Error, ValueError):
            self._json(
                {"error": "dashboard data is temporarily unavailable"},
                HTTPStatus.SERVICE_UNAVAILABLE,
            )
            return
        payload["refresh"] = self.refresh_service.status()
        self._json(payload)

    def do_POST(self) -> None:
        if self.path != "/api/refresh":
            self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            return
        self.refresh_service.request_refresh()
        self._json({"accepted": True}, HTTPStatus.ACCEPTED)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--refresh-seconds", type=int, default=1800)
    parser.add_argument("--no-initial-refresh", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.host not in {"127.0.0.1", "::1", "localhost"}:
        print("Dashboard server must bind to localhost", file=sys.stderr)
        return 2
    if not 1 <= args.port <= 65535:
        print("Dashboard server port must be between 1 and 65535", file=sys.stderr)
        return 2
    try:
        connection = connect_db(DEFAULT_DB)
        connection.close()
        refresh = MarketRefreshService(
            args.refresh_seconds, initial=not args.no_initial_refresh
        )
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(f"Dashboard server could not start: {exc}", file=sys.stderr)
        return 1

    handler = type(
        "ConfiguredDashboardRequestHandler",
        (DashboardRequestHandler,),
        {"refresh_service": refresh, "database": DEFAULT_DB},
    )
    server = ThreadingHTTPServer((args.host, args.port), handler)
    server.daemon_threads = True
    refresh.start()
    print(f"Dashboard API: http://{args.host}:{args.port}")
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        refresh.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
