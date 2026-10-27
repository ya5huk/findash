"""Behavior tests for provider quote normalization in refresh_prices."""

import io
from decimal import Decimal
from pathlib import Path
import sqlite3
import sys
import unittest
from contextlib import redirect_stdout
from unittest.mock import MagicMock, patch

import refresh_prices
from findash_core.dividends import DividendEvent, DividendSeries


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = (ROOT / "scripts" / "init-db.sql").read_text(encoding="utf-8")


class RefreshSecurityTests(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.addCleanup(self.con.close)
        self.cur = self.con.cursor()
        self.cur.execute(
            """CREATE TABLE prices (
                   security_id INTEGER NOT NULL,
                   date TEXT NOT NULL,
                   close_minor INTEGER NOT NULL,
                   currency TEXT NOT NULL,
                   PRIMARY KEY (security_id, date)
               )"""
        )
        self.cur.execute(
            """CREATE TABLE security_price_observations (
                   id INTEGER PRIMARY KEY,
                   security_id INTEGER NOT NULL,
                   observed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                   price_minor INTEGER NOT NULL,
                   currency TEXT NOT NULL,
                   source TEXT NOT NULL,
                   UNIQUE(security_id, observed_at, source)
               )"""
        )

    def _refresh(self, db_currency, yahoo_currency, close):
        history = ([("2026-07-20", close)], yahoo_currency)
        with patch.object(
            refresh_prices,
            "fetch_yahoo_history_with_currency",
            return_value=history,
        ):
            result = refresh_prices.refresh_security(
                self.cur, 1, "TEST", db_currency, "1mo"
            )
        row = self.cur.execute(
            "SELECT close_minor, currency FROM prices"
        ).fetchone()
        return result, row

    def test_usd_major_quote_is_stored_as_cents(self):
        result, row = self._refresh("USD", "USD", 123.456)
        self.assertEqual(result, (1, True))
        self.assertEqual(row, (12346, "USD"))

    def test_lse_quote_is_stored_as_pence_and_canonical_gbp(self):
        result, row = self._refresh("GBP", "GBp", 1075.4)
        self.assertEqual(result, (1, True))
        self.assertEqual(row, (1075, "GBP"))

    def test_tase_quote_is_stored_as_agorot_and_canonical_ils(self):
        result, row = self._refresh("ILA", "ILA", 8450)
        self.assertEqual(result, (1, True))
        self.assertEqual(row, (8450, "ILS"))

    def test_generic_major_currency_uses_iso_minor_scale(self):
        result, row = self._refresh("EUR", "EUR", 42.75)
        self.assertEqual(result, (1, True))
        self.assertEqual(row, (4275, "EUR"))

    def test_latest_quote_is_appended_for_live_dashboard(self):
        self._refresh("USD", "USD", 123.456)
        self.assertEqual(
            self.cur.execute(
                """SELECT security_id,price_minor,currency,source
                     FROM security_price_observations"""
            ).fetchone(),
            (1, 12346, "USD", "yahoo"),
        )

    def test_currency_mismatch_fails_without_writing(self):
        result, row = self._refresh("EUR", "USD", 42.75)
        self.assertEqual(result, (0, False))
        self.assertIsNone(row)

    def test_missing_yahoo_currency_fails_without_writing(self):
        result, row = self._refresh("USD", None, 42.75)
        self.assertEqual(result, (0, False))
        self.assertIsNone(row)


class RefreshIntradayTests(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.addCleanup(self.con.close)
        self.con.executescript(SCHEMA)
        self.cur = self.con.cursor()
        self.cur.execute(
            """INSERT INTO securities(ticker,name,asset_class,currency)
               VALUES('TEST','Test','stock','USD')"""
        )

    def _refresh(self, points, periods, yahoo_currency="USD"):
        with patch.object(
            refresh_prices,
            "fetch_yahoo_intraday",
            return_value=(points, yahoo_currency, periods),
        ):
            return refresh_prices.refresh_security_intraday(
                self.cur, 1, "TEST", "USD"
            )

    def test_session_points_are_backfilled_at_provider_timestamps(self):
        result = self._refresh(
            [(1_775_050_200, 100.5), (1_775_050_500, 101.0)],
            {
                "pre": {"start": 1_775_030_400, "end": 1_775_050_200},
                "regular": {"start": 1_775_050_200, "end": 1_775_073_600},
                "post": {"start": 1_775_073_600, "end": 1_775_088_000},
            },
        )

        self.assertEqual(result, (2, True))
        rows = self.cur.execute(
            """SELECT observed_at,price_minor,currency,source
                 FROM security_price_observations ORDER BY observed_at"""
        ).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][1], 10050)
        self.assertEqual(rows[0][2], "USD")
        self.assertEqual(rows[0][3], "yahoo-intraday")

    def test_refetching_the_same_session_is_idempotent(self):
        periods = {
            "regular": {"start": 1_775_050_200, "end": 1_775_073_600},
        }
        first = self._refresh([(1_775_050_200, 100.5)], periods)
        second = self._refresh([(1_775_050_200, 100.5)], periods)

        self.assertEqual(first, (1, True))
        self.assertEqual(second, (0, True))
        self.assertEqual(
            self.cur.execute(
                "SELECT COUNT(*) FROM security_price_observations"
            ).fetchone()[0],
            1,
        )

    def test_session_bounds_are_stored_in_meta(self):
        self._refresh(
            [(1_775_050_200, 100.5)],
            {
                "pre": {"start": 1_775_030_400, "end": 1_775_050_200},
                "regular": {"start": 1_775_050_200, "end": 1_775_073_600},
                "post": {"start": 1_775_073_600, "end": 1_775_088_000},
            },
        )
        stored = self.cur.execute(
            "SELECT value FROM meta WHERE key='market_session:1'"
        ).fetchone()
        self.assertIsNotNone(stored)
        import json as json_module

        bounds = json_module.loads(stored[0])
        self.assertEqual(bounds["regular_start"][:10], bounds["date"])
        self.assertLess(bounds["pre_start"], bounds["regular_start"])
        self.assertLess(bounds["regular_end"], bounds["post_end"])

    def test_failed_fetch_writes_nothing(self):
        with patch.object(
            refresh_prices, "fetch_yahoo_intraday", return_value=None
        ):
            result = refresh_prices.refresh_security_intraday(
                self.cur, 1, "TEST", "USD"
            )
        self.assertEqual(result, (0, False))
        self.assertEqual(
            self.cur.execute(
                "SELECT COUNT(*) FROM security_price_observations"
            ).fetchone()[0],
            0,
        )


class YahooFetchTests(unittest.TestCase):
    def test_empty_provider_result_is_a_nonfatal_fetch_failure(self):
        response = MagicMock()
        response.__enter__.return_value = io.StringIO(
            '{"chart":{"result":[],"error":null}}'
        )
        with patch.object(
            refresh_prices.urllib.request, "urlopen", return_value=response
        ):
            self.assertIsNone(refresh_prices.fetch_yahoo_history("MISSING", "1mo"))

    def test_symbol_is_url_encoded_before_request(self):
        requested = []

        def fail(request, timeout):
            requested.append(request.full_url)
            raise refresh_prices.urllib.error.URLError("offline")

        with patch.object(
            refresh_prices.urllib.request, "urlopen", side_effect=fail
        ):
            self.assertIsNone(refresh_prices.fetch_yahoo_history("BAD/QUERY?", "1mo"))
        self.assertIn("BAD%2FQUERY%3F", requested[0])


class RefreshFxTests(unittest.TestCase):
    def test_document_source_is_preserved_case_insensitively(self):
        con = sqlite3.connect(":memory:")
        self.addCleanup(con.close)
        cur = con.cursor()
        cur.execute(
            """CREATE TABLE fx_rates (
                   date TEXT, base_currency TEXT, quote_currency TEXT,
                   rate REAL, source TEXT,
                   UNIQUE(date, base_currency, quote_currency)
               )"""
        )
        cur.execute(
            "INSERT INTO fx_rates VALUES('2026-07-20','USD','ILS',3.5,'Document')"
        )
        with patch.object(
            refresh_prices,
            "fetch_yahoo_history",
            return_value=[("2026-07-20", 3.9)],
        ):
            self.assertEqual(
                refresh_prices.refresh_fx(cur, "USD", "ILS", "1mo"),
                (0, True),
            )
        self.assertEqual(
            cur.execute("SELECT rate, source FROM fx_rates").fetchone(),
            (3.5, "Document"),
        )

    def test_latest_rate_is_appended_for_live_dashboard(self):
        con = sqlite3.connect(":memory:")
        self.addCleanup(con.close)
        cur = con.cursor()
        cur.executescript(
            """CREATE TABLE fx_rates (
                   date TEXT, base_currency TEXT, quote_currency TEXT,
                   rate REAL, source TEXT,
                   UNIQUE(date, base_currency, quote_currency)
               );
               CREATE TABLE fx_rate_observations (
                   id INTEGER PRIMARY KEY,
                   base_currency TEXT NOT NULL,
                   quote_currency TEXT NOT NULL,
                   observed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                   rate REAL NOT NULL,
                   source TEXT NOT NULL,
                   UNIQUE(base_currency, quote_currency, observed_at, source)
               );"""
        )
        with patch.object(
            refresh_prices,
            "fetch_yahoo_history",
            return_value=[("2026-07-19", 3.4), ("2026-07-20", 3.6)],
        ):
            self.assertEqual(
                refresh_prices.refresh_fx(cur, "USD", "ILS", "1mo"),
                (2, True),
            )
        self.assertEqual(
            cur.execute(
                """SELECT base_currency,quote_currency,rate,source
                     FROM fx_rate_observations"""
            ).fetchone(),
            ("USD", "ILS", 3.6, "yahoo"),
        )


class RefreshDistributionTests(unittest.TestCase):
    def test_fractional_minor_per_share_amount_is_stored_exactly(self):
        con = sqlite3.connect(":memory:")
        self.addCleanup(con.close)
        cur = con.cursor()
        cur.execute(
            """CREATE TABLE security_distributions(
                   security_id INTEGER,
                   event_date TEXT,
                   amount_per_share_minor_numerator INTEGER,
                   amount_per_share_minor_denominator INTEGER,
                   currency TEXT,
                   source TEXT,
                   source_key TEXT,
                   observed_at TEXT DEFAULT CURRENT_TIMESTAMP,
                   UNIQUE(security_id,source,source_key)
               )"""
        )
        series = DividendSeries(
            (
                DividendEvent(
                    "2026-07-01",
                    Decimal("0.421"),
                    "USD",
                    source_key="event-1",
                ),
            ),
            "USD",
        )

        written = refresh_prices.store_distribution_series(cur, {1: series})

        self.assertEqual(written, 1)
        self.assertEqual(
            cur.execute(
                """SELECT amount_per_share_minor_numerator,
                          amount_per_share_minor_denominator,currency
                     FROM security_distributions"""
            ).fetchone(),
            (421, 10, "USD"),
        )

    def test_successful_series_replaces_stale_rows_and_records_coverage(self):
        con = sqlite3.connect(":memory:")
        self.addCleanup(con.close)
        con.executescript(SCHEMA)
        cur = con.cursor()
        security_id = cur.execute(
            """INSERT INTO securities(ticker,asset_class,currency)
               VALUES('DIST','stock','USD') RETURNING id"""
        ).fetchone()[0]
        cur.execute(
            """INSERT INTO security_distributions(
                   security_id,event_date,
                   amount_per_share_minor_numerator,
                   amount_per_share_minor_denominator,currency,source,source_key
               ) VALUES(?,'2025-01-01',100,1,'USD','yahoo','stale')""",
            (security_id,),
        )
        series = DividendSeries(
            (
                DividendEvent(
                    "2026-01-01",
                    Decimal("1"),
                    "USD",
                    source_key="current",
                ),
            ),
            "USD",
        )
        targets = [(security_id, "DIST", "USD")]

        refresh_prices.store_distribution_series(
            cur, {security_id: series}
        )
        refresh_prices.store_distribution_refreshes(
            cur, targets, {security_id: series}
        )

        self.assertEqual(
            cur.execute(
                """SELECT source_key FROM security_distributions
                    WHERE security_id=?""",
                (security_id,),
            ).fetchall(),
            [("current",)],
        )
        self.assertEqual(
            cur.execute(
                """SELECT status FROM security_distribution_refreshes
                    WHERE security_id=?""",
                (security_id,),
            ).fetchone()[0],
            "success",
        )

    def test_failed_refresh_retains_cache_and_records_failure(self):
        con = sqlite3.connect(":memory:")
        self.addCleanup(con.close)
        con.executescript(SCHEMA)
        cur = con.cursor()
        security_id = cur.execute(
            """INSERT INTO securities(ticker,asset_class,currency)
               VALUES('DIST','stock','USD') RETURNING id"""
        ).fetchone()[0]
        cur.execute(
            """INSERT INTO security_distributions(
                   security_id,event_date,
                   amount_per_share_minor_numerator,
                   amount_per_share_minor_denominator,currency,source,source_key
               ) VALUES(?,'2025-01-01',100,1,'USD','yahoo','known-good')""",
            (security_id,),
        )

        refresh_prices.store_distribution_refreshes(
            cur, [(security_id, "DIST", "USD")], {}
        )

        self.assertEqual(
            cur.execute(
                "SELECT COUNT(*) FROM security_distributions"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            cur.execute(
                "SELECT status FROM security_distribution_refreshes"
            ).fetchone()[0],
            "failed",
        )


class TargetSelectionTests(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.addCleanup(self.con.close)
        self.con.executescript(SCHEMA)
        self.cur = self.con.cursor()
        self.account_id = self.cur.execute(
            "INSERT INTO accounts(name,kind,institution,currency) VALUES(?,?,?,?)",
            ("Broker", "brokerage", "Broker", "USD"),
        ).lastrowid
        self.spy_id = self.cur.execute(
            "INSERT INTO securities(ticker,name,asset_class,currency) VALUES(?,?,?,?)",
            ("SPY", "Benchmark", "benchmark", "USD"),
        ).lastrowid

    def add_security(self, ticker: str) -> int:
        return self.cur.execute(
            "INSERT INTO securities(ticker,name,asset_class,currency) VALUES(?,?,?,?)",
            (ticker, ticker, "stock", "USD"),
        ).lastrowid

    def test_latest_position_is_selected_per_security(self):
        first = self.add_security("FIRST")
        second = self.add_security("SECOND")
        self.cur.executemany(
            """INSERT INTO positions(
                   account_id,security_id,as_of,quantity,currency
               ) VALUES(?,?,?,?,?)""",
            [
                (self.account_id, first, "2026-01-01", 2, "USD"),
                (self.account_id, second, "2026-02-01", 3, "USD"),
            ],
        )

        targets = refresh_prices.securities_to_fetch(self.cur, "1mo")

        self.assertEqual(
            {row["id"] for row in targets}, {first, second, self.spy_id}
        )

    def test_evidence_linked_raw_trade_does_not_reopen_closed_canonical_holding(self):
        sid = self.add_security("ROUNDTRIP")
        feed = self.cur.execute(
            "INSERT INTO account_feeds(account_id,provider,feed_type) VALUES(?,?,?)",
            (self.account_id, "provider", "api"),
        ).lastrowid
        source = self.cur.execute(
            """INSERT INTO source_events(
                   account_feed_id,source_key,event_type,occurred_on
               ) VALUES(?,?,?,?)""",
            (feed, "buy-1", "trade", "2026-01-01"),
        ).lastrowid
        buy = self.cur.execute(
            """INSERT INTO canonical_events(
                   account_id,event_type,occurred_on,currency,security_id,side,
                   quantity,price_minor,fees_minor
               ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (self.account_id, "trade", "2026-01-01", "USD", sid, "buy", 1, 100, 0),
        ).lastrowid
        self.cur.execute(
            """INSERT INTO event_evidence(
                   canonical_event_id,source_event_id,evidence_role,match_method
               ) VALUES(?,?,?,?)""",
            (buy, source, "primary", "provider id"),
        )
        self.cur.execute(
            """INSERT INTO trades(
                   account_id,security_id,date,side,shares,price_minor,fees_minor,
                   currency,source_event_id
               ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (self.account_id, sid, "2026-01-01", "buy", 1, 100, 0, "USD", source),
        )
        self.cur.execute(
            """INSERT INTO canonical_events(
                   account_id,event_type,occurred_on,currency,security_id,side,
                   quantity,price_minor,fees_minor
               ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (self.account_id, "trade", "2026-01-02", "USD", sid, "sell", 1, 100, 0),
        )

        targets = refresh_prices.securities_to_fetch(self.cur, "1mo")

        self.assertNotIn(sid, {row["id"] for row in targets})

    def test_existing_history_is_backfilled_to_opening_brokerage_evidence(self):
        self.cur.execute(
            "INSERT INTO balances(account_id,as_of,amount_minor,currency) VALUES(?,?,?,?)",
            (self.account_id, "2020-01-01", 10_000, "USD"),
        )
        self.cur.execute(
            "INSERT INTO prices(security_id,date,close_minor,currency) VALUES(?,?,?,?)",
            (self.spy_id, "2025-01-01", 10_000, "USD"),
        )

        target = next(
            row
            for row in refresh_prices.securities_to_fetch(self.cur, "1mo")
            if row["id"] == self.spy_id
        )

        self.assertEqual(target["range"], "max")

    def test_fx_history_backfills_to_earliest_foreign_fact(self):
        self.cur.execute(
            "INSERT INTO balances(account_id,as_of,amount_minor,currency) VALUES(?,?,?,?)",
            (self.account_id, "2020-01-01", 10_000, "USD"),
        )
        self.cur.execute(
            "INSERT INTO fx_rates(date,base_currency,quote_currency,rate) VALUES(?,?,?,?)",
            ("2025-01-01", "USD", "ILS", 3.5),
        )

        self.assertEqual(
            refresh_prices.fx_range_to_fetch(self.cur, "USD", "ILS", "1mo"),
            "max",
        )


class RefreshOutputPrivacyTests(unittest.TestCase):
    def test_main_reports_only_aggregate_counts(self):
        con = sqlite3.connect(":memory:")
        con.executescript(
            """
            CREATE TABLE securities (
                id INTEGER PRIMARY KEY,
                ticker TEXT UNIQUE,
                name TEXT,
                asset_class TEXT,
                currency TEXT
            );
            CREATE TABLE trades (currency TEXT);
            CREATE TABLE balances (currency TEXT);
            CREATE TABLE transactions (currency TEXT);
            CREATE TABLE positions (currency TEXT);
            CREATE TABLE payslips (currency TEXT);
            CREATE TABLE canonical_events (currency TEXT);
            INSERT INTO transactions(currency) VALUES ('EUR');
            """
        )
        private_ticker = "PRIVATE_TICKER"
        targets = [{
            "id": 7,
            "ticker": private_ticker,
            "currency": "USD",
            "range": "1mo",
        }]
        stdout = io.StringIO()
        with (
            patch.object(refresh_prices, "connect_db", return_value=con),
            patch.object(refresh_prices, "securities_to_fetch", return_value=targets),
            patch.object(
                refresh_prices,
                "distribution_securities_to_fetch",
                return_value=[],
            ),
            patch.object(refresh_prices, "refresh_security", return_value=(0, False)),
            patch.object(refresh_prices, "refresh_fx", return_value=(2, True)),
            patch.object(refresh_prices, "fx_range_to_fetch", return_value="1mo"),
            patch.object(refresh_prices.time, "sleep"),
            patch.object(sys, "argv", ["refresh_prices.py", "--range", "1mo"]),
            redirect_stdout(stdout),
        ):
            refresh_prices.main()

        output = stdout.getvalue()
        self.assertIn(
            "Prices: attempted=1 succeeded=0 failed=1 rows_written=0",
            output,
        )
        self.assertIn(
            "FX: attempted=2 succeeded=2 failed=0 rows_written=4",
            output,
        )
        self.assertIn(
            "Dividends: attempted=0 succeeded=0 failed=0 rows_written=0",
            output,
        )
        self.assertNotIn(private_ticker, output)
        self.assertNotIn("EUR->ILS", output)
        self.assertNotIn("USD->ILS", output)


if __name__ == "__main__":
    unittest.main()
