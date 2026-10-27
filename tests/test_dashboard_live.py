from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
SCHEMA = (ROOT / "scripts" / "init-db.sql").read_text(encoding="utf-8")

from findash_core.dashboard_live import build_live_dashboard_snapshot  # noqa: E402


class LiveDashboardTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.database = Path(self.temporary.name) / "finance.db"
        connection = sqlite3.connect(self.database)
        self.addCleanup(connection.close)
        connection.executescript(SCHEMA)
        account_id = connection.execute(
            """INSERT INTO accounts(name,kind,institution,currency)
               VALUES('Sample brokerage','brokerage','Sample broker','USD')
               RETURNING id"""
        ).fetchone()[0]
        security_id = connection.execute(
            """INSERT INTO securities(ticker,name,asset_class,currency)
               VALUES('DEMO','Sample holding','stock','USD') RETURNING id"""
        ).fetchone()[0]
        connection.execute(
            """INSERT INTO positions(
                   account_id,security_id,as_of,quantity,avg_cost_minor,
                   market_price_minor,market_value_minor,currency
               ) VALUES(?,?,'2026-08-03',2,10000,15000,30000,'USD')""",
            (account_id, security_id),
        )
        connection.executemany(
            """INSERT INTO prices(security_id,date,close_minor,currency)
               VALUES(?,?,?,'USD')""",
            [
                (security_id, "2026-07-31", 12500),
                (security_id, "2026-08-03", 15000),
            ],
        )
        connection.execute(
            """INSERT INTO fx_rates(
                   date,base_currency,quote_currency,rate,source
               ) VALUES('2026-08-03','USD','ILS',3.5,'yahoo')"""
        )
        connection.execute(
            """INSERT INTO security_price_observations(
                   security_id,observed_at,price_minor,currency,source
               ) VALUES(?,'2026-08-03 10:00:00',20000,'USD','yahoo')""",
            (security_id,),
        )
        connection.execute(
            """INSERT INTO security_price_observations(
                   security_id,observed_at,price_minor,currency,source
               ) VALUES(?,'2026-08-03 09:30:00',18000,'USD','yahoo')""",
            (security_id,),
        )
        connection.execute(
            """INSERT INTO fx_rate_observations(
                   base_currency,quote_currency,observed_at,rate,source
               ) VALUES('USD','ILS','2026-08-03 10:01:00',4.0,'yahoo')"""
        )
        connection.execute(
            "INSERT INTO meta(key,value) VALUES('last_sync_at','2026-08-03T09:00:00Z')"
        )
        connection.commit()

    def test_live_quotes_overlay_current_values_without_rewriting_history(self):
        snapshot = build_live_dashboard_snapshot(self.database, "2026-08-03")

        self.assertEqual(snapshot["overview"]["cached_total"], 1050.0)
        self.assertEqual(snapshot["overview"]["total"], 1600.0)
        self.assertEqual(snapshot["overview"]["live_delta"], 550.0)
        self.assertEqual(snapshot["positions"][0]["cached_last_price"], 150.0)
        self.assertEqual(snapshot["positions"][0]["last_price"], 200.0)
        self.assertEqual(snapshot["positions"][0]["market_reporting"], 1600.0)
        self.assertEqual(snapshot["positions"][0]["valuation_source"], "live quote")
        self.assertEqual(snapshot["positions"][0]["market_usd"], 400.0)
        self.assertEqual(snapshot["positions"][0]["day_change_usd"], 150.0)
        self.assertEqual(snapshot["positions"][0]["day_change_pct"], 60.0)
        self.assertEqual(snapshot["positions"][0]["pnl_usd"], 200.0)
        self.assertEqual(snapshot["positions"][0]["pnl_usd_pct"], 100.0)
        self.assertEqual(snapshot["positions"][0]["session_date"], "2026-08-03")
        self.assertFalse(snapshot["positions"][0]["session_is_previous"])
        self.assertEqual(
            snapshot["positions"][0]["session_prices"],
            [125.0, 180.0, 200.0],
        )
        self.assertEqual(snapshot["market"]["fx"][0]["rate"], 4.0)
        self.assertEqual(
            snapshot["freshness"]["last_sync_at"], "2026-08-03T09:00:00Z"
        )

        connection = sqlite3.connect(self.database)
        self.addCleanup(connection.close)
        self.assertEqual(
            connection.execute(
                "SELECT close_minor FROM prices WHERE date='2026-08-03'"
            ).fetchone()[0],
            15000,
        )
        self.assertNotIn("state", snapshot)
        json.dumps(snapshot, allow_nan=False)

    def test_closed_market_uses_latest_completed_session_movement(self):
        snapshot = build_live_dashboard_snapshot(self.database, "2026-08-04")
        position = snapshot["positions"][0]

        self.assertEqual(position["session_date"], "2026-08-03")
        self.assertTrue(position["session_is_previous"])
        self.assertEqual(position["session_phase"], "previous")
        self.assertEqual(position["session_prices"], [125.0, 180.0, 200.0, 150.0])
        self.assertEqual(position["day_change_usd"], 50.0)
        self.assertEqual(position["day_change_pct"], 20.0)

    def _store_session_bounds(self, day: str) -> None:
        connection = sqlite3.connect(self.database)
        self.addCleanup(connection.close)
        security_id = connection.execute(
            "SELECT id FROM securities WHERE ticker='DEMO'"
        ).fetchone()[0]
        connection.execute(
            "INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)",
            (
                f"market_session:{security_id}",
                json.dumps(
                    {
                        "date": day,
                        "pre_start": f"{day} 08:00:00",
                        "regular_start": f"{day} 13:30:00",
                        "regular_end": f"{day} 20:00:00",
                        "post_end": f"{day} 23:59:00",
                    }
                ),
            ),
        )
        return connection, security_id

    def test_pre_market_observations_render_as_a_pre_session(self):
        connection, security_id = self._store_session_bounds("2026-08-04")
        connection.executemany(
            """INSERT INTO security_price_observations(
                   security_id,observed_at,price_minor,currency,source
               ) VALUES(?,?,?,'USD','yahoo-intraday')""",
            [
                (security_id, "2026-08-04 08:05:00", 15500),
                (security_id, "2026-08-04 09:00:00", 16000),
            ],
        )
        connection.commit()

        snapshot = build_live_dashboard_snapshot(self.database, "2026-08-04")
        position = snapshot["positions"][0]

        self.assertEqual(position["session_phase"], "pre")
        self.assertEqual(position["session_date"], "2026-08-04")
        self.assertFalse(position["session_is_previous"])
        # Previous close first, then the pre-market trades only.
        self.assertEqual(position["session_prices"], [150.0, 155.0, 160.0])
        self.assertEqual(position["day_change_pct"], (160 - 150) / 150 * 100)

    def test_regular_session_uses_only_observations_after_the_open(self):
        connection, security_id = self._store_session_bounds("2026-08-04")
        connection.executemany(
            """INSERT INTO security_price_observations(
                   security_id,observed_at,price_minor,currency,source
               ) VALUES(?,?,?,'USD','yahoo-intraday')""",
            [
                (security_id, "2026-08-04 08:05:00", 15500),
                (security_id, "2026-08-04 13:35:00", 17000),
                (security_id, "2026-08-04 14:00:00", 17500),
            ],
        )
        connection.commit()

        snapshot = build_live_dashboard_snapshot(self.database, "2026-08-04")
        position = snapshot["positions"][0]

        self.assertEqual(position["session_phase"], "regular")
        self.assertEqual(position["session_date"], "2026-08-04")
        # Yesterday's close, then the regular session only — the 08:05
        # pre-market trade stays out of the regular-session series.
        self.assertEqual(position["session_prices"], [150.0, 170.0, 175.0])

    def test_bounds_for_another_day_fall_back_to_previous_session(self):
        connection, _ = self._store_session_bounds("2026-08-03")
        connection.commit()

        snapshot = build_live_dashboard_snapshot(self.database, "2026-08-04")
        position = snapshot["positions"][0]

        self.assertEqual(position["session_phase"], "previous")
        self.assertTrue(position["session_is_previous"])


if __name__ == "__main__":
    unittest.main()
