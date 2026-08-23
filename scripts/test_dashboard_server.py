"""Tests for the private live-dashboard API process."""

from contextlib import redirect_stderr
import io
import subprocess
import unittest
from unittest.mock import MagicMock, patch

import dashboard_server


class MarketRefreshServiceTest(unittest.TestCase):
    def test_refresh_runs_fixed_script_without_forwarding_private_output(self):
        service = dashboard_server.MarketRefreshService(1800)
        completed = MagicMock(returncode=0)

        with patch.object(
            dashboard_server.subprocess, "run", return_value=completed
        ) as run:
            service._refresh()

        args, kwargs = run.call_args
        self.assertEqual(
            args[0],
            [
                dashboard_server.sys.executable,
                str(dashboard_server.REFRESH_SCRIPT),
                "--range",
                "1mo",
            ],
        )
        self.assertIs(kwargs["stdout"], subprocess.DEVNULL)
        self.assertIs(kwargs["stderr"], subprocess.DEVNULL)
        self.assertEqual(service.status()["last_ok"], True)

    def test_interval_below_one_minute_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "at least 60 seconds"):
            dashboard_server.MarketRefreshService(59)


class DashboardRequestHandlerTest(unittest.TestCase):
    def test_client_disconnect_is_reported_without_raising(self):
        handler = object.__new__(dashboard_server.DashboardRequestHandler)
        handler.close_connection = False
        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock(side_effect=BrokenPipeError)
        handler.wfile = MagicMock()
        output = io.StringIO()

        with redirect_stderr(output):
            handler._json({"ok": True})

        self.assertTrue(handler.close_connection)
        self.assertIn("browser disconnected", output.getvalue())
        handler.wfile.write.assert_not_called()

    def test_response_write_errors_do_not_trigger_a_second_response(self):
        handler = object.__new__(dashboard_server.DashboardRequestHandler)
        handler.path = "/api/dashboard"
        handler.database = MagicMock()
        handler.refresh_service = MagicMock()
        handler._json = MagicMock(side_effect=OSError("write failed"))

        with patch.object(
            dashboard_server,
            "build_live_dashboard_snapshot",
            return_value={},
        ):
            with self.assertRaisesRegex(OSError, "write failed"):
                handler.do_GET()

        handler._json.assert_called_once()


class ServerArgumentsTest(unittest.TestCase):
    def test_non_localhost_binding_is_rejected_before_database_access(self):
        with patch.object(dashboard_server, "connect_db") as connect:
            with redirect_stderr(io.StringIO()):
                self.assertEqual(dashboard_server.main(["--host", "0.0.0.0"]), 2)
        connect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
