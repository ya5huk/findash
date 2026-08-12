from __future__ import annotations

from decimal import Decimal
from pathlib import Path
import sqlite3
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
SCHEMA = (ROOT / "scripts" / "init-db.sql").read_text(encoding="utf-8")

from findash_core.accounting import AccountingEngine  # noqa: E402
from findash_core.dashboard_model import DashboardModelBuilder  # noqa: E402
from findash_core.dividends import (  # noqa: E402
    DividendEvent,
    DividendSeries,
    project_between,
    yahoo_symbol_for,
)


class DividendModelTests(unittest.TestCase):
    def open_database(self) -> sqlite3.Connection:
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.executescript(SCHEMA)
        return connection

    def test_quantity_history_does_not_require_prices_or_fx(self):
        connection = self.open_database()
        account = connection.execute(
            """INSERT INTO accounts(name,kind,institution,currency)
               VALUES('Broker','brokerage','Broker','USD') RETURNING id"""
        ).fetchone()[0]
        security = connection.execute(
            """INSERT INTO securities(ticker,asset_class,currency)
               VALUES('HOLD','stock','USD') RETURNING id"""
        ).fetchone()[0]
        connection.executemany(
            """INSERT INTO trades(
                   account_id,security_id,date,side,shares,price_minor,currency
               ) VALUES(?,?,?,?,?,?,?)""",
            [
                (account, security, "2026-01-01", "buy", 10, 1_000, "USD"),
                (account, security, "2026-02-01", "buy", 5, 1_100, "USD"),
            ],
        )
        engine = AccountingEngine(connection)

        self.assertEqual(
            engine.security_quantities_at("2026-01-31")[security],
            Decimal("10.0"),
        )
        self.assertEqual(
            engine.security_quantities_at("2026-02-01")[security],
            Decimal("15.0"),
        )

    def test_complete_snapshot_removes_omitted_security_from_entitlement(self):
        connection = self.open_database()
        account = connection.execute(
            """INSERT INTO accounts(name,kind,institution,currency)
               VALUES('Broker','brokerage','Broker','ILS') RETURNING id"""
        ).fetchone()[0]
        old_security = connection.execute(
            """INSERT INTO securities(ticker,asset_class,currency)
               VALUES('OLD','stock','ILS') RETURNING id"""
        ).fetchone()[0]
        current_security = connection.execute(
            """INSERT INTO securities(ticker,asset_class,currency)
               VALUES('CURRENT','stock','ILS') RETURNING id"""
        ).fetchone()[0]
        feed = connection.execute(
            """INSERT INTO account_feeds(account_id,provider,feed_type)
               VALUES(?,'complete-test','api') RETURNING id""",
            (account,),
        ).fetchone()[0]
        connection.execute(
            """INSERT INTO positions(
                   account_id,security_id,as_of,quantity,currency
               ) VALUES(?,?,? ,5,'ILS')""",
            (account, old_security, "2026-01-01"),
        )
        current_position = connection.execute(
            """INSERT INTO positions(
                   account_id,security_id,as_of,quantity,currency
               ) VALUES(?,?,? ,2,'ILS') RETURNING id""",
            (account, current_security, "2026-02-01"),
        ).fetchone()[0]
        connection.execute(
            """INSERT INTO position_evidence(
                   canonical_position_id,account_feed_id,source_key,
                   observed_quantity,observed_currency,
                   account_snapshot_complete
               ) VALUES(?,?,?,2,'ILS',1)""",
            (current_position, feed, "complete:2026-02-01"),
        )

        quantities = AccountingEngine(connection).security_quantities_at(
            "2026-02-01"
        )

        self.assertNotIn(old_security, quantities)
        self.assertEqual(quantities[current_security], Decimal("2"))

    def test_modeled_history_uses_pre_event_shares_and_keeps_cash_separate(self):
        connection = self.open_database()
        account = connection.execute(
            """INSERT INTO accounts(name,kind,institution,currency)
               VALUES('Broker','brokerage','Broker','ILS') RETURNING id"""
        ).fetchone()[0]
        security = connection.execute(
            """INSERT INTO securities(ticker,asset_class,currency)
               VALUES('HOLD','stock','ILS') RETURNING id"""
        ).fetchone()[0]
        connection.executemany(
            """INSERT INTO trades(
                   account_id,security_id,date,side,shares,price_minor,currency
               ) VALUES(?,?,?,?,?,?,?)""",
            [
                (account, security, "2026-01-01", "buy", 10, 1_000, "ILS"),
                # A later buy must not rewrite the February or April history.
                (account, security, "2026-05-01", "buy", 5, 1_100, "ILS"),
            ],
        )
        series = DividendSeries(
            (
                DividendEvent("2026-02-01", Decimal("1"), "ILS"),
                DividendEvent("2026-04-01", Decimal("1"), "ILS"),
                DividendEvent("2026-06-01", Decimal("1"), "ILS"),
            ),
            "ILS",
        )

        def provider(ticker: str, currency: str) -> DividendSeries | None:
            return series if (ticker, currency) == ("HOLD", "ILS") else None

        recorded = [
            {
                "date": "2026-03-01",
                "payer": "Recorded payer",
                "amount": 7.0,
                "currency": "ILS",
                "reporting_amount": 7.0,
            }
        ]
        analysis = DashboardModelBuilder(
            connection, dividend_provider=provider
        )._dividend_analysis(recorded, "2026-07-01")

        february = next(
            row
            for row in analysis["modeled_history"]
            if row["date"] == "2026-02-01"
        )
        self.assertEqual(february["quantity"], 10.0)
        self.assertEqual(february["reporting_amount"], 10.0)
        self.assertEqual(analysis["recorded"], recorded)
        self.assertEqual(len(analysis["modeled_history"]), 3)
        next_year = next(row for row in analysis["yearly"] if row["is_next"])
        self.assertIsNotNone(next_year["modeled"])
        self.assertGreater(next_year["modeled"], 0)
        self.assertIsNone(next_year["recorded"])

        chart = DashboardModelBuilder(connection)._dividend_chart(
            "2026-07-01", analysis
        )
        current = next(
            row for row in chart["yearViews"] if row["year"] == "2026"
        )
        holding = next(
            row for row in current["series"] if row["ticker"] == "HOLD"
        )
        self.assertEqual(
            [row["year"] for row in chart["yearViews"]],
            ["2023", "2024", "2025", "2026", "2027"],
        )
        # February and April used ten shares; only June used the later fifteen.
        self.assertEqual(holding["data"][1], 10.0)
        self.assertEqual(holding["data"][3], 10.0)
        self.assertEqual(holding["data"][5], 15.0)
        self.assertGreater(current["total"], 35.0)

    def test_monthly_dividend_views_group_after_top_five_as_other(self):
        connection = self.open_database()
        modeled = [
            {
                "date": "2026-02-01",
                "ticker": f"DIV{index}",
                "reporting_amount": float(70 - index * 10),
            }
            for index in range(7)
        ]
        analysis = {
            "modeled_history": modeled,
            "recorded": [],
            "upcoming": [],
            "source_enabled": True,
            "history_incomplete": False,
            "unavailable_security_count": 0,
            "valuation_failure_count": 0,
        }

        chart = DashboardModelBuilder(connection)._dividend_chart(
            "2026-07-01", analysis
        )
        current = next(
            row for row in chart["yearViews"] if row["year"] == "2026"
        )

        self.assertEqual(
            [row["ticker"] for row in current["series"]],
            ["DIV0", "DIV1", "DIV2", "DIV3", "DIV4", "Other"],
        )
        self.assertEqual(current["series"][-1]["data"][1], 30.0)

    def test_new_purchase_only_increases_distributions_after_purchase_date(self):
        connection = self.open_database()
        account = connection.execute(
            """INSERT INTO accounts(name,kind,institution,currency)
               VALUES('Broker','brokerage','Broker','ILS') RETURNING id"""
        ).fetchone()[0]
        security = connection.execute(
            """INSERT INTO securities(ticker,asset_class,currency)
               VALUES('GROW','stock','ILS') RETURNING id"""
        ).fetchone()[0]
        connection.executemany(
            """INSERT INTO trades(
                   account_id,security_id,date,side,shares,price_minor,currency
               ) VALUES(?,?,?,?,?,?,?)""",
            [
                (account, security, "2026-01-01", "buy", 10, 1_000, "ILS"),
                (account, security, "2026-05-01", "buy", 5, 1_100, "ILS"),
            ],
        )
        series = DividendSeries(
            (
                DividendEvent("2026-02-01", Decimal("1"), "ILS"),
                DividendEvent("2026-04-01", Decimal("1"), "ILS"),
                DividendEvent("2026-06-01", Decimal("1"), "ILS"),
            ),
            "ILS",
        )

        def provider(ticker: str, currency: str) -> DividendSeries | None:
            return series if (ticker, currency) == ("GROW", "ILS") else None

        builder = DashboardModelBuilder(connection, dividend_provider=provider)
        before_analysis = builder._dividend_analysis([], "2026-04-30")
        after_analysis = builder._dividend_analysis([], "2026-05-01")
        before = next(
            row
            for row in builder._dividend_chart(
                "2026-04-30", before_analysis
            )["yearViews"]
            if row["year"] == "2026"
        )
        after_chart = builder._dividend_chart("2026-05-01", after_analysis)
        after = next(
            row for row in after_chart["yearViews"] if row["year"] == "2026"
        )
        before_grow = next(
            row for row in before["series"] if row["ticker"] == "GROW"
        )
        after_grow = next(
            row for row in after["series"] if row["ticker"] == "GROW"
        )

        self.assertEqual(before["historical"], 20.0)
        self.assertEqual(after["historical"], 20.0)
        self.assertEqual(before_grow["data"][1:4:2], [10.0, 10.0])
        self.assertEqual(after_grow["data"][1:4:2], [10.0, 10.0])
        self.assertEqual(before_grow["data"][5], 10.0)
        self.assertEqual(after_grow["data"][5], 15.0)
        self.assertGreater(after["forecast"], before["forecast"])

        before_next = next(
            row
            for row in builder._dividend_chart(
                "2026-04-30", before_analysis
            )["yearViews"]
            if row["year"] == "2027"
        )
        after_next = next(
            row for row in after_chart["yearViews"] if row["year"] == "2027"
        )
        self.assertGreater(after_next["total"], before_next["total"])

    def test_projection_rejects_lapsed_series_and_path_like_tickers(self):
        events = (
            DividendEvent("2020-01-01", Decimal("1"), "USD"),
            DividendEvent("2020-04-01", Decimal("1"), "USD"),
        )

        self.assertEqual(
            project_between(
                events, after="2026-01-01", through="2027-12-31"
            ),
            [],
        )
        self.assertIsNone(yahoo_symbol_for("../../private"))

    def test_builder_reads_cached_distributions_without_a_network_provider(self):
        connection = self.open_database()
        account = connection.execute(
            """INSERT INTO accounts(name,kind,institution,currency)
               VALUES('Broker','brokerage','Broker','ILS') RETURNING id"""
        ).fetchone()[0]
        security = connection.execute(
            """INSERT INTO securities(ticker,asset_class,currency)
               VALUES('CACHE','stock','ILS') RETURNING id"""
        ).fetchone()[0]
        connection.execute(
            """INSERT INTO trades(
                   account_id,security_id,date,side,shares,price_minor,currency
               ) VALUES(?,?,?,'buy',10,1000,'ILS')""",
            (account, security, "2026-01-01"),
        )
        connection.executemany(
            """INSERT INTO security_distributions(
                   security_id,event_date,
                   amount_per_share_minor_numerator,
                   amount_per_share_minor_denominator,currency,source,source_key
               ) VALUES(?,?,100,1,'ILS','yahoo',?)""",
            [
                (security, "2026-02-01", "one"),
                (security, "2026-04-01", "two"),
                (security, "2026-06-01", "three"),
            ],
        )
        connection.execute(
            """INSERT INTO security_distribution_refreshes(
                   security_id,source,status
               ) VALUES(?,'yahoo','success')""",
            (security,),
        )

        analysis = DashboardModelBuilder(connection)._dividend_analysis(
            [], "2026-07-01"
        )

        self.assertTrue(analysis["source_enabled"])
        self.assertEqual(len(analysis["modeled_history"]), 3)
        self.assertTrue(analysis["upcoming"])

    def test_cached_distribution_supports_canonical_only_trade_history(self):
        connection = self.open_database()
        account = connection.execute(
            """INSERT INTO accounts(name,kind,institution,currency)
               VALUES('Broker','brokerage','Broker','ILS') RETURNING id"""
        ).fetchone()[0]
        security = connection.execute(
            """INSERT INTO securities(ticker,asset_class,currency)
               VALUES('CANON','stock','ILS') RETURNING id"""
        ).fetchone()[0]
        connection.execute(
            """INSERT INTO canonical_events(
                   account_id,event_type,occurred_on,currency,status,
                   security_id,side,quantity,price_minor,fees_minor
               ) VALUES(?,'trade','2026-01-01','ILS','confirmed',
                        ?,'buy',10,1000,0)""",
            (account, security),
        )
        connection.execute(
            """INSERT INTO security_distributions(
                   security_id,event_date,
                   amount_per_share_minor_numerator,
                   amount_per_share_minor_denominator,currency,source,source_key
               ) VALUES(?,'2026-02-01',100,1,'ILS','yahoo','canonical-event')""",
            (security,),
        )
        connection.execute(
            """INSERT INTO security_distribution_refreshes(
                   security_id,source,status
               ) VALUES(?,'yahoo','success')""",
            (security,),
        )

        analysis = DashboardModelBuilder(connection)._dividend_analysis(
            [], "2026-03-01"
        )

        self.assertEqual(len(analysis["modeled_history"]), 1)
        self.assertEqual(analysis["modeled_history"][0]["ticker"], "CANON")

    def test_confirmed_empty_refresh_is_not_reported_as_failure(self):
        connection = self.open_database()
        account = connection.execute(
            """INSERT INTO accounts(name,kind,institution,currency)
               VALUES('Broker','brokerage','Broker','ILS') RETURNING id"""
        ).fetchone()[0]
        security = connection.execute(
            """INSERT INTO securities(ticker,asset_class,currency)
               VALUES('NO-DIV','stock','ILS') RETURNING id"""
        ).fetchone()[0]
        connection.execute(
            """INSERT INTO trades(
                   account_id,security_id,date,side,shares,price_minor,currency
               ) VALUES(?,?,'2026-01-01','buy',10,1000,'ILS')""",
            (account, security),
        )
        connection.execute(
            """INSERT INTO security_distribution_refreshes(
                   security_id,source,status
               ) VALUES(?,'yahoo','empty')""",
            (security,),
        )

        analysis = DashboardModelBuilder(connection)._dividend_analysis(
            [], "2026-03-01"
        )

        self.assertTrue(analysis["source_enabled"])
        self.assertEqual(analysis["unavailable_security_count"], 0)
        self.assertEqual(analysis["modeled_history"], [])


if __name__ == "__main__":
    unittest.main()
