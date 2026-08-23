from __future__ import annotations

from pathlib import Path
import sqlite3
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
SCHEMA = (ROOT / "scripts" / "init-db.sql").read_text(encoding="utf-8")

from findash_core.dashboard_model import DashboardModelBuilder  # noqa: E402


class DashboardModelTest(unittest.TestCase):
    def test_payslip_chart_groups_by_payroll_period_not_payment_month(self):
        con = sqlite3.connect(":memory:")
        self.addCleanup(con.close)
        con.executescript(SCHEMA)
        con.executemany(
            """INSERT INTO payslips(
                   period_start,period_end,paid_on,employer,gross_minor,net_minor,
                   income_tax_minor,social_security_minor,health_insurance_minor,
                   pension_employee_minor,pension_employer_minor,
                   study_fund_employee_minor,study_fund_employer_minor,currency
               ) VALUES(?,?,?,?,?,?,0,0,0,0,0,0,0,'ILS')""",
            [
                (
                    "2026-05-01", "2026-05-31", "2026-06-08",
                    "Employer", 100000, 70000,
                ),
                (
                    "2026-06-01", "2026-06-30", None,
                    "Employer", 200000, 150000,
                ),
            ],
        )

        chart = DashboardModelBuilder(con)._payslip_chart("2026-06-30")

        self.assertEqual(chart["months"], ["2026-05", "2026-06"])
        self.assertEqual(chart["gross"], [1000.0, 2000.0])
        self.assertEqual(chart["net"], [700.0, 1500.0])

    def test_net_worth_history_omits_isolated_pre_ledger_snapshot_run(self):
        con = sqlite3.connect(":memory:")
        self.addCleanup(con.close)
        con.executescript(SCHEMA)
        savings = con.execute(
            """INSERT INTO accounts(name,kind,institution,currency)
               VALUES('Savings','savings','Bank','ILS') RETURNING id"""
        ).fetchone()[0]
        brokerage = con.execute(
            """INSERT INTO accounts(name,kind,institution,currency)
               VALUES('Broker','brokerage','Broker','ILS') RETURNING id"""
        ).fetchone()[0]
        con.execute(
            """INSERT INTO balances(account_id,as_of,amount_minor,currency)
               VALUES(?,'2017-01-01',10000,'ILS')""",
            (savings,),
        )
        con.execute(
            """INSERT INTO transactions(
                   account_id,date,amount_minor,currency,category,flow_type
               ) VALUES(?,'2026-01-01',50000,'ILS','deposit','transfer')""",
            (brokerage,),
        )

        chart = DashboardModelBuilder(con)._net_worth_chart("2026-01-08")

        self.assertEqual(chart["dates"], ["2026-01-01", "2026-01-08"])
        self.assertEqual(chart["coverage_basis"], "post_evidence_gap")
        self.assertNotIn(None, chart["values"])

    def test_net_worth_keeps_continuous_bank_history_before_investing(self):
        con = sqlite3.connect(":memory:")
        self.addCleanup(con.close)
        con.executescript(SCHEMA)
        checking = con.execute(
            """INSERT INTO accounts(name,kind,institution,currency)
               VALUES('Checking','checking','Bank','ILS') RETURNING id"""
        ).fetchone()[0]
        brokerage = con.execute(
            """INSERT INTO accounts(name,kind,institution,currency)
               VALUES('Broker','brokerage','Broker','ILS') RETURNING id"""
        ).fetchone()[0]
        con.executemany(
            """INSERT INTO balances(account_id,as_of,amount_minor,currency)
               VALUES(?,?,10000,'ILS')""",
            [
                (checking, "2024-01-01"),
                (checking, "2025-01-01"),
            ],
        )
        con.execute(
            """INSERT INTO transactions(
                   account_id,date,amount_minor,currency,category,flow_type
               ) VALUES(?,'2026-01-01',50000,'ILS','deposit','transfer')""",
            (brokerage,),
        )

        chart = DashboardModelBuilder(con)._net_worth_chart("2026-01-01")

        self.assertEqual(chart["dates"][0], "2024-01-01")
        self.assertEqual(
            chart["coverage_basis"], "earliest_account_evidence"
        )

    def test_net_worth_ignores_evidence_before_account_opening(self):
        con = sqlite3.connect(":memory:")
        self.addCleanup(con.close)
        con.executescript(SCHEMA)
        checking = con.execute(
            """INSERT INTO accounts(
                   name,kind,institution,currency,opened_on
               ) VALUES('Checking','checking','Bank','ILS','2026-01-01')
               RETURNING id"""
        ).fetchone()[0]
        con.execute(
            """INSERT INTO balances(account_id,as_of,amount_minor,currency)
               VALUES(?,'2025-12-01',10000,'ILS')""",
            (checking,),
        )
        con.execute(
            """INSERT INTO transactions(
                   account_id,date,amount_minor,currency,category,flow_type
               ) VALUES(?,'2026-01-01',50000,'ILS','opening','transfer')""",
            (checking,),
        )

        chart = DashboardModelBuilder(con)._net_worth_chart("2026-01-08")

        self.assertEqual(chart["dates"], ["2026-01-01", "2026-01-08"])

    def test_net_worth_breaks_when_snapshot_adds_item_to_known_account(self):
        con = sqlite3.connect(":memory:")
        self.addCleanup(con.close)
        con.executescript(SCHEMA)
        brokerage = con.execute(
            """INSERT INTO accounts(name,kind,institution,currency)
               VALUES('Broker','brokerage','Broker','ILS') RETURNING id"""
        ).fetchone()[0]
        security = con.execute(
            """INSERT INTO securities(ticker,asset_class,currency)
               VALUES('HELD','stock','ILS') RETURNING id"""
        ).fetchone()[0]
        con.execute(
            """INSERT INTO transactions(
                   account_id,date,amount_minor,currency,category,flow_type
               ) VALUES(?,'2026-01-01',50000,'ILS','deposit','transfer')""",
            (brokerage,),
        )
        con.execute(
            """INSERT INTO positions(
                   account_id,security_id,as_of,quantity,market_price_minor,
                   market_value_minor,currency
               ) VALUES(? ,? ,'2026-01-08',10,10000,100000,'ILS')""",
            (brokerage, security),
        )

        chart = DashboardModelBuilder(con)._net_worth_chart("2026-01-08")

        self.assertEqual(chart["dates"], ["2026-01-01", "2026-01-08"])
        self.assertEqual(chart["coverageBreaks"], [False, True])

    def test_net_worth_breaks_on_material_snapshot_reanchor(self):
        con = sqlite3.connect(":memory:")
        self.addCleanup(con.close)
        con.executescript(SCHEMA)
        checking = con.execute(
            """INSERT INTO accounts(name,kind,institution,currency)
               VALUES('Checking','checking','Bank','ILS') RETURNING id"""
        ).fetchone()[0]
        con.executemany(
            """INSERT INTO balances(account_id,as_of,amount_minor,currency)
               VALUES(?,?,?,'ILS')""",
            [
                (checking, "2026-01-01", 10_000),
                (checking, "2026-01-08", 50_000),
            ],
        )

        chart = DashboardModelBuilder(con)._net_worth_chart("2026-01-08")

        self.assertEqual(chart["coverageBreaks"], [False, True])

    def test_net_worth_connects_non_material_new_account_coverage(self):
        con = sqlite3.connect(":memory:")
        self.addCleanup(con.close)
        con.executescript(SCHEMA)
        checking = con.execute(
            """INSERT INTO accounts(name,kind,institution,currency)
               VALUES('Checking','checking','Bank','ILS') RETURNING id"""
        ).fetchone()[0]
        savings = con.execute(
            """INSERT INTO accounts(
                   name,kind,institution,currency,opened_on
               ) VALUES('Savings','savings','Bank','ILS','2026-01-08')
               RETURNING id"""
        ).fetchone()[0]
        con.executemany(
            """INSERT INTO balances(account_id,as_of,amount_minor,currency)
               VALUES(?,?,?,'ILS')""",
            [
                (checking, "2026-01-01", 10_000),
                (checking, "2026-01-08", 5_000),
                (savings, "2026-01-08", 5_000),
            ],
        )

        chart = DashboardModelBuilder(con)._net_worth_chart("2026-01-08")

        self.assertEqual(chart["coverageBreaks"], [False, False])

    def test_benchmark_seeds_from_opening_portfolio_then_applies_later_flows(self):
        con = sqlite3.connect(":memory:")
        self.addCleanup(con.close)
        con.executescript(SCHEMA)
        account_id = con.execute(
            "INSERT INTO accounts(name,kind,institution,currency) VALUES(?,?,?,?)",
            ("Broker", "brokerage", "Broker", "ILS"),
        ).lastrowid
        held_id = con.execute(
            "INSERT INTO securities(ticker,name,asset_class,currency) VALUES(?,?,?,?)",
            ("HELD", "Held", "stock", "ILS"),
        ).lastrowid
        spy_id = con.execute(
            "INSERT INTO securities(ticker,name,asset_class,currency) VALUES(?,?,?,?)",
            ("SPY", "Benchmark", "benchmark", "ILS"),
        ).lastrowid
        con.execute(
            """INSERT INTO positions(
                   account_id,security_id,as_of,quantity,market_price_minor,
                   market_value_minor,currency
               ) VALUES(?,?,?,?,?,?,?)""",
            (account_id, held_id, "2026-01-01", 100, 1_000, 100_000, "ILS"),
        )
        con.executemany(
            "INSERT INTO prices(security_id,date,close_minor,currency) VALUES(?,?,?,?)",
            [
                (held_id, "2026-01-01", 1_000, "ILS"),
                (held_id, "2026-01-08", 1_000, "ILS"),
                (spy_id, "2026-01-01", 1_000, "ILS"),
                (spy_id, "2026-01-08", 1_000, "ILS"),
            ],
        )
        con.execute(
            """INSERT INTO transactions(
                   account_id,date,amount_minor,currency,category,flow_type
               ) VALUES(?,?,?,?,?,?)""",
            (account_id, "2026-01-08", 10_000, "ILS", "deposit", "transfer"),
        )

        chart = DashboardModelBuilder(con).build("2026-01-08")["charts"]["stocksVsSpy"]

        self.assertEqual(chart["dates"], ["2026-01-01", "2026-01-08"])
        self.assertEqual(chart["stocks"], [1000.0, 1100.0])
        self.assertEqual(chart["spy"], [1000.0, 1100.0])
        self.assertEqual(chart["cumulativeDeposits"], [0.0, 100.0])

    def test_benchmark_excludes_liquid_study_fund_from_brokerage_value(self):
        con = sqlite3.connect(":memory:")
        self.addCleanup(con.close)
        con.executescript(SCHEMA)
        brokerage_id = con.execute(
            "INSERT INTO accounts(name,kind,institution,currency) VALUES(?,?,?,?)",
            ("Broker", "brokerage", "Broker", "ILS"),
        ).lastrowid
        study_fund_id = con.execute(
            """INSERT INTO accounts(
                   name,kind,institution,currency,liquidity_date
               ) VALUES(?,?,?,?,?)""",
            ("Study fund", "study_fund", "Fund", "ILS", "2025-01-01"),
        ).lastrowid
        spy_id = con.execute(
            "INSERT INTO securities(ticker,name,asset_class,currency) VALUES(?,?,?,?)",
            ("SPY", "Benchmark", "benchmark", "ILS"),
        ).lastrowid
        con.executemany(
            "INSERT INTO balances(account_id,as_of,amount_minor,currency) VALUES(?,?,?,?)",
            [
                (brokerage_id, "2026-01-01", 100_000, "ILS"),
                (study_fund_id, "2026-01-01", 500_000, "ILS"),
            ],
        )
        con.execute(
            "INSERT INTO prices(security_id,date,close_minor,currency) VALUES(?,?,?,?)",
            (spy_id, "2026-01-01", 1_000, "ILS"),
        )

        chart = DashboardModelBuilder(con).build("2026-01-01")["charts"]["stocksVsSpy"]

        self.assertEqual(chart["stocks"], [1000.0])
        self.assertEqual(chart["spy"], [1000.0])

    def test_benchmark_does_not_seed_from_explicitly_partial_position_snapshot(self):
        con = sqlite3.connect(":memory:")
        self.addCleanup(con.close)
        con.executescript(SCHEMA)
        account_id = con.execute(
            "INSERT INTO accounts(name,kind,institution,currency) VALUES(?,?,?,?)",
            ("Broker", "brokerage", "Broker", "ILS"),
        ).lastrowid
        feed_id = con.execute(
            """INSERT INTO account_feeds(account_id,provider,feed_type)
               VALUES(?,?,'api')""",
            (account_id, "partial-provider"),
        ).lastrowid
        held_id = con.execute(
            "INSERT INTO securities(ticker,name,asset_class,currency) VALUES(?,?,?,?)",
            ("HELD", "Held", "stock", "ILS"),
        ).lastrowid
        spy_id = con.execute(
            "INSERT INTO securities(ticker,name,asset_class,currency) VALUES(?,?,?,?)",
            ("SPY", "Benchmark", "benchmark", "ILS"),
        ).lastrowid
        position_id = con.execute(
            """INSERT INTO positions(
                   account_id,security_id,as_of,quantity,market_price_minor,
                   market_value_minor,currency
               ) VALUES(?,?,?,?,?,?,?)""",
            (account_id, held_id, "2026-01-01", 100, 1_000, 100_000, "ILS"),
        ).lastrowid
        con.execute(
            """INSERT INTO position_evidence(
                   canonical_position_id,account_feed_id,source_key,
                   observed_quantity,observed_price_minor,observed_value_minor,
                   observed_currency,account_snapshot_complete
               ) VALUES(?,?,?,?,?,?,?,0)""",
            (position_id, feed_id, "partial-read", 100, 1_000, 100_000, "ILS"),
        )
        con.executemany(
            "INSERT INTO prices(security_id,date,close_minor,currency) VALUES(?,?,?,?)",
            [
                (held_id, "2026-01-01", 1_000, "ILS"),
                (spy_id, "2026-01-01", 1_000, "ILS"),
            ],
        )

        model = DashboardModelBuilder(con).build("2026-01-01")

        self.assertEqual(model["charts"]["stocksVsSpy"]["stocks"], [1000.0])
        self.assertEqual(model["charts"]["stocksVsSpy"]["spy"], [None])
        self.assertIn(
            "not marked complete",
            " ".join(model["state"].rows[0].coverage.warnings),
        )

    def test_complete_empty_snapshot_anchors_later_first_trade(self):
        con = sqlite3.connect(":memory:")
        self.addCleanup(con.close)
        con.executescript(SCHEMA)
        account_id = con.execute(
            "INSERT INTO accounts(name,kind,institution,currency) VALUES(?,?,?,?)",
            ("Broker", "brokerage", "Broker", "ILS"),
        ).lastrowid
        feed_id = con.execute(
            """INSERT INTO account_feeds(account_id,provider,feed_type)
               VALUES(?,?,'api')""",
            (account_id, "complete-provider"),
        ).lastrowid
        held_id = con.execute(
            "INSERT INTO securities(ticker,name,asset_class,currency) VALUES(?,?,?,?)",
            ("HELD", "Held", "stock", "ILS"),
        ).lastrowid
        spy_id = con.execute(
            "INSERT INTO securities(ticker,name,asset_class,currency) VALUES(?,?,?,?)",
            ("SPY", "Benchmark", "benchmark", "ILS"),
        ).lastrowid
        balance_id = con.execute(
            """INSERT INTO balances(
                   account_id,as_of,component,amount_minor,currency
               ) VALUES(?,?,?,?,?)""",
            (account_id, "2026-01-01", "cash_ils", 0, "ILS"),
        ).lastrowid
        con.execute(
            """INSERT INTO balance_evidence(
                   canonical_balance_id,account_feed_id,source_key,
                   observed_amount_minor,observed_currency,
                   account_snapshot_complete
               ) VALUES(?,?,?,?,?,1)""",
            (balance_id, feed_id, "complete-empty", 0, "ILS"),
        )
        con.execute(
            """INSERT INTO transactions(
                   account_id,date,amount_minor,currency,category,flow_type
               ) VALUES(?,?,?,?,?,?)""",
            (account_id, "2026-01-02", 10_000, "ILS", "deposit", "transfer"),
        )
        con.execute(
            """INSERT INTO trades(
                   account_id,security_id,date,side,shares,price_minor,
                   fees_minor,currency
               ) VALUES(?,?,?,?,?,?,?,?)""",
            (account_id, held_id, "2026-01-03", "buy", 10, 1_000, 0, "ILS"),
        )
        con.executemany(
            "INSERT INTO prices(security_id,date,close_minor,currency) VALUES(?,?,?,?)",
            [
                (held_id, "2026-01-08", 1_000, "ILS"),
                (spy_id, "2026-01-01", 1_000, "ILS"),
                (spy_id, "2026-01-08", 1_000, "ILS"),
            ],
        )

        model = DashboardModelBuilder(con).build("2026-01-08")

        chart = model["charts"]["stocksVsSpy"]
        self.assertEqual(chart["spy"], [None, 100.0])
        position = model["state"].positions[0]
        self.assertEqual(position.anchor_date, "2026-01-01")
        self.assertFalse(position.coverage.is_partial)

    def test_complete_empty_snapshot_anchors_later_cash_currency(self):
        con = sqlite3.connect(":memory:")
        self.addCleanup(con.close)
        con.executescript(SCHEMA)
        account_id = con.execute(
            "INSERT INTO accounts(name,kind,institution,currency) VALUES(?,?,?,?)",
            ("Broker", "brokerage", "Broker", "ILS"),
        ).lastrowid
        feed_id = con.execute(
            """INSERT INTO account_feeds(account_id,provider,feed_type)
               VALUES(?,?,'api')""",
            (account_id, "complete-provider"),
        ).lastrowid
        balance_id = con.execute(
            """INSERT INTO balances(
                   account_id,as_of,component,amount_minor,currency
               ) VALUES(?,?,?,?,?)""",
            (account_id, "2026-01-01", "cash_ils", 0, "ILS"),
        ).lastrowid
        con.execute(
            """INSERT INTO balance_evidence(
                   canonical_balance_id,account_feed_id,source_key,
                   observed_amount_minor,observed_currency,
                   account_snapshot_complete
               ) VALUES(?,?,?,?,?,1)""",
            (balance_id, feed_id, "complete-empty", 0, "ILS"),
        )
        held_id = con.execute(
            "INSERT INTO securities(ticker,name,asset_class,currency) VALUES(?,?,?,?)",
            ("HELD", "Held", "stock", "USD"),
        ).lastrowid
        spy_id = con.execute(
            "INSERT INTO securities(ticker,name,asset_class,currency) VALUES(?,?,?,?)",
            ("SPY", "Benchmark", "benchmark", "USD"),
        ).lastrowid
        con.execute(
            """INSERT INTO transactions(
                   account_id,date,amount_minor,currency,category,flow_type
               ) VALUES(?,?,?,?,?,?)""",
            (account_id, "2026-01-02", 10_000, "USD", "deposit", "transfer"),
        )
        con.execute(
            """INSERT INTO trades(
                   account_id,security_id,date,side,shares,price_minor,
                   fees_minor,currency
               ) VALUES(?,?,?,?,?,?,?,?)""",
            (account_id, held_id, "2026-01-03", "buy", 1, 10_000, 0, "USD"),
        )
        con.executemany(
            "INSERT INTO prices(security_id,date,close_minor,currency) VALUES(?,?,?,?)",
            [
                (held_id, "2026-01-08", 10_000, "USD"),
                (spy_id, "2026-01-01", 10_000, "USD"),
                (spy_id, "2026-01-08", 10_000, "USD"),
            ],
        )
        con.execute(
            "INSERT INTO fx_rates(date,base_currency,quote_currency,rate) VALUES(?,?,?,?)",
            ("2026-01-01", "USD", "ILS", 4),
        )

        model = DashboardModelBuilder(con).build("2026-01-08")

        self.assertEqual(
            model["charts"]["stocksVsSpy"],
            {
                "dates": ["2026-01-01", "2026-01-08"],
                "stocks": [0.0, 400.0],
                "spy": [None, 400.0],
                "cumulativeDeposits": [0.0, 400.0],
                "currency": "ILS",
            },
        )
        cash = next(
            row for row in model["state"].rows if row.item_key == "cash_usd"
        )
        self.assertEqual(cash.coverage.anchor_date, "2026-01-01")
        self.assertFalse(cash.coverage.is_partial)

    def test_benchmark_waits_for_positive_complete_investment_state(self):
        con = sqlite3.connect(":memory:")
        self.addCleanup(con.close)
        con.executescript(SCHEMA)
        account_id = con.execute(
            "INSERT INTO accounts(name,kind,institution,currency) VALUES(?,?,?,?)",
            ("Broker", "brokerage", "Broker", "ILS"),
        ).lastrowid
        held_id = con.execute(
            "INSERT INTO securities(ticker,name,asset_class,currency) VALUES(?,?,?,?)",
            ("HELD", "Held", "stock", "ILS"),
        ).lastrowid
        spy_id = con.execute(
            "INSERT INTO securities(ticker,name,asset_class,currency) VALUES(?,?,?,?)",
            ("SPY", "Benchmark", "benchmark", "ILS"),
        ).lastrowid
        # This legacy raw cash row overlaps raw trade history without an explicit
        # relationship. The conservative compatibility path derives trade cash,
        # producing a zero, partial state that must not seed the benchmark.
        con.execute(
            """INSERT INTO transactions(
                   account_id,date,amount_minor,currency,category,flow_type
               ) VALUES(?,?,?,?,?,?)""",
            (account_id, "2026-01-01", -100_000, "ILS", "securities_buy", "investment"),
        )
        con.execute(
            """INSERT INTO trades(
                   account_id,security_id,date,side,shares,price_minor,
                   fees_minor,currency
               ) VALUES(?,?,?,?,?,?,?,?)""",
            (account_id, held_id, "2026-01-01", "buy", 100, 1_000, 0, "ILS"),
        )
        con.execute(
            """INSERT INTO positions(
                   account_id,security_id,as_of,quantity,market_price_minor,
                   market_value_minor,currency
               ) VALUES(?,?,?,?,?,?,?)""",
            (account_id, held_id, "2026-01-08", 100, 1_000, 100_000, "ILS"),
        )
        con.execute(
            """INSERT INTO balances(
                   account_id,as_of,component,amount_minor,currency
               ) VALUES(?,?,?,?,?)""",
            (account_id, "2026-01-08", "cash_ils", 0, "ILS"),
        )
        con.executemany(
            "INSERT INTO prices(security_id,date,close_minor,currency) VALUES(?,?,?,?)",
            [
                (held_id, "2026-01-01", 1_000, "ILS"),
                (held_id, "2026-01-08", 1_000, "ILS"),
                (spy_id, "2026-01-01", 1_000, "ILS"),
                (spy_id, "2026-01-08", 1_000, "ILS"),
            ],
        )

        chart = DashboardModelBuilder(con).build("2026-01-08")["charts"]["stocksVsSpy"]

        self.assertEqual(chart["stocks"], [0.0, 1000.0])
        self.assertEqual(chart["spy"], [None, 1000.0])

    def test_benchmark_can_seed_from_dated_whole_account_snapshot(self):
        con = sqlite3.connect(":memory:")
        self.addCleanup(con.close)
        con.executescript(SCHEMA)
        account_id = con.execute(
            "INSERT INTO accounts(name,kind,institution,currency) VALUES(?,?,?,?)",
            ("Broker", "brokerage", "Broker", "ILS"),
        ).lastrowid
        spy_id = con.execute(
            "INSERT INTO securities(ticker,name,asset_class,currency) VALUES(?,?,?,?)",
            ("SPY", "Benchmark", "benchmark", "ILS"),
        ).lastrowid
        con.execute(
            "INSERT INTO balances(account_id,as_of,amount_minor,currency) VALUES(?,?,?,?)",
            (account_id, "2026-01-01", 100_000, "ILS"),
        )
        con.executemany(
            "INSERT INTO prices(security_id,date,close_minor,currency) VALUES(?,?,?,?)",
            [
                (spy_id, "2026-01-01", 1_000, "ILS"),
                (spy_id, "2026-01-08", 1_100, "ILS"),
            ],
        )

        chart = DashboardModelBuilder(con).build("2026-01-08")["charts"]["stocksVsSpy"]

        self.assertEqual(chart["stocks"], [1000.0, 1000.0])
        self.assertEqual(chart["spy"], [1000.0, 1100.0])

    def test_benchmark_axis_includes_later_aggregate_seed_date(self):
        con = sqlite3.connect(":memory:")
        self.addCleanup(con.close)
        con.executescript(SCHEMA)
        account_id = con.execute(
            "INSERT INTO accounts(name,kind,institution,currency) VALUES(?,?,?,?)",
            ("Broker", "brokerage", "Broker", "ILS"),
        ).lastrowid
        spy_id = con.execute(
            "INSERT INTO securities(ticker,name,asset_class,currency) VALUES(?,?,?,?)",
            ("SPY", "Benchmark", "benchmark", "ILS"),
        ).lastrowid
        # This earlier flow makes the weekly axis start before the aggregate date.
        con.execute(
            """INSERT INTO transactions(
                   account_id,date,amount_minor,currency,category,flow_type
               ) VALUES(?,?,?,?,?,?)""",
            (account_id, "2026-01-01", 10_000, "ILS", "deposit", "transfer"),
        )
        con.execute(
            "INSERT INTO balances(account_id,as_of,amount_minor,currency) VALUES(?,?,?,?)",
            (account_id, "2026-01-03", 100_000, "ILS"),
        )
        con.executemany(
            "INSERT INTO prices(security_id,date,close_minor,currency) VALUES(?,?,?,?)",
            [
                (spy_id, "2026-01-03", 1_000, "ILS"),
                (spy_id, "2026-01-08", 1_100, "ILS"),
            ],
        )

        chart = DashboardModelBuilder(con).build("2026-01-08")["charts"]["stocksVsSpy"]

        self.assertEqual(chart["dates"], ["2026-01-01", "2026-01-03", "2026-01-08"])
        self.assertIsNone(chart["spy"][0])
        self.assertEqual(chart["spy"][1:], [1000.0, 1100.0])

    def test_overview_freshness_uses_oldest_account_dependency(self):
        con = sqlite3.connect(":memory:")
        self.addCleanup(con.close)
        con.executescript(SCHEMA)
        account_id = con.execute(
            "INSERT INTO accounts(name,kind,institution,currency) VALUES(?,?,?,?)",
            ("Multi-currency", "checking", "Bank", "ILS"),
        ).lastrowid
        con.executemany(
            "INSERT INTO balances(account_id,as_of,amount_minor,currency) VALUES(?,?,?,?)",
            [
                (account_id, "2026-01-01", 10_000, "ILS"),
                (account_id, "2026-01-10", 10_000, "USD"),
            ],
        )
        con.execute(
            "INSERT INTO fx_rates(date,base_currency,quote_currency,rate) VALUES(?,?,?,?)",
            ("2026-01-10", "USD", "ILS", 4),
        )

        model = DashboardModelBuilder(con).build("2026-01-10")
        shown = model["overview"]["buckets"][0]["accounts"][0]

        self.assertEqual(shown["observed_through"], "2026-01-01")

    def test_fx_chart_prefers_document_rate_across_quote_orientation(self):
        con = sqlite3.connect(":memory:")
        self.addCleanup(con.close)
        con.executescript(SCHEMA)
        con.executemany(
            """INSERT INTO fx_rates(
                   date,base_currency,quote_currency,rate,source
               ) VALUES(?,?,?,?,?)""",
            [
                ("2026-01-02", "USD", "ILS", 3.5, "yahoo"),
                ("2026-01-02", "ILS", "USD", 0.25, "document"),
            ],
        )

        chart = DashboardModelBuilder(con)._fx_chart([])

        self.assertEqual(chart["dates"], ["2026-01-02"])
        self.assertEqual(chart["usd"], [4.0])
        self.assertEqual(
            chart["series"],
            [{"currency": "USD", "values": [4.0]}],
        )

    def test_forex_activity_separates_funding_from_currency_purchases(self):
        con = sqlite3.connect(":memory:")
        self.addCleanup(con.close)
        con.executescript(SCHEMA)
        account_id = con.execute(
            """INSERT INTO accounts(name,kind,institution,currency)
               VALUES('Sample brokerage','brokerage','Broker','USD') RETURNING id"""
        ).fetchone()[0]
        conversion_doc = con.execute(
            """INSERT INTO documents(
                   drive_id,drive_path,filename,doc_type,doc_date
               ) VALUES('fx-1','fx-conversions/2026/sample.png','sample.png',
                        'fx_conversion','2026-01-04') RETURNING id"""
        ).fetchone()[0]
        con.execute(
            """INSERT INTO documents(
                   drive_id,drive_path,filename,doc_type,doc_date
               ) VALUES('fx-2','fx-conversions/2025/older.png','older.png',
                        'fx_conversion','2025-12-31')"""
        )
        con.executemany(
            """INSERT INTO transactions(
                   account_id,date,amount_minor,currency,category,flow_type,
                   source_doc_id
               ) VALUES(?,?,?,?,?,?,?)""",
            [
                (account_id, "2026-01-02", 10_000, "USD", "deposit", "transfer", None),
                (account_id, "2026-01-03", 20_000, "USD", "deposit", "transfer", None),
                (account_id, "2026-01-04", 5_000, "USD", "deposit", "transfer", conversion_doc),
                (account_id, "2026-01-04", -5_000, "USD", "withdrawal", "transfer", None),
            ],
        )
        con.executemany(
            """INSERT INTO fx_rates(
                   date,base_currency,quote_currency,rate,source
               ) VALUES(?,?,?,?,?)""",
            [
                ("2025-12-31", "USD", "ILS", 3.4, "document"),
                ("2026-01-02", "USD", "ILS", 3.5, "document"),
                ("2026-01-03", "USD", "ILS", 3.6, "yahoo"),
                ("2026-01-04", "USD", "ILS", 3.7, "document"),
            ],
        )
        builder = DashboardModelBuilder(con)

        external = builder.accounting.external_flows("2026-01-04")
        deposits = builder._deposits(external)
        forex = builder._forex_activity(deposits, "2026-01-04")

        self.assertEqual(len(external), 3)
        self.assertEqual([row["date"] for row in forex["rows"]], [
            "2026-01-04",
            "2026-01-04",
            "2026-01-03",
            "2026-01-02",
            "2025-12-31",
        ])
        self.assertEqual(forex["rows"][0]["kind"], "currency_purchase")
        self.assertTrue(forex["rows"][0]["rate_is_actual"])
        self.assertEqual(forex["rows"][1]["kind"], "brokerage_withdrawal")
        self.assertEqual(forex["rows"][2]["rate_source"], "yahoo")
        self.assertFalse(forex["rows"][2]["rate_is_actual"])
        self.assertEqual(forex["rows"][4]["kind"], "currency_purchase")
        self.assertIsNone(forex["rows"][4]["amount"])
        self.assertEqual(forex["event_count"], 5)
        self.assertEqual(forex["external_event_count"], 3)
        self.assertEqual(forex["activity_count"], 5)
        self.assertEqual(forex["valued_count"], 3)
        self.assertEqual(forex["funding_count"], 2)
        self.assertEqual(forex["withdrawal_count"], 1)
        self.assertEqual(forex["purchase_count"], 2)
        self.assertEqual(forex["total_reporting_amount"], 885.0)
        self.assertEqual(forex["net_external_reporting_amount"], 885.0)
        self.assertEqual(
            [
                row["cumulative_reporting_amount"]
                for row in forex["rows"]
            ],
            [885.0, 885.0, 1070.0, 350.0, 0.0],
        )
        self.assertEqual(
            [row["cumulative_usd_amount"] for row in forex["rows"]],
            [250.0, 250.0, 300.0, 100.0, 0.0],
        )
        self.assertEqual(
            [
                row["cumulative_external_reporting_amount"]
                for row in forex["rows"]
            ],
            [885.0, 885.0, 1070.0, 350.0, 0.0],
        )
        self.assertEqual(
            [row["cumulative_usd_purchased_amount"] for row in forex["rows"]],
            [50.0, 0.0, 0.0, 0.0, 0.0],
        )
        self.assertEqual(
            forex["summary"],
            [
                {
                    "currency": "USD",
                    "count": 3,
                    "foreign_amount": 250.0,
                    "reporting_amount": 885.0,
                    "weighted_rate": 885.0 / 250.0,
                    "actual_rate_count": 0,
                }
            ],
        )

    def test_internal_fx_semantics_do_not_depend_on_vault_folder(self):
        con = sqlite3.connect(":memory:")
        self.addCleanup(con.close)
        con.executescript(SCHEMA)
        account_id = con.execute(
            """INSERT INTO accounts(name,kind,institution,currency)
               VALUES('Sample brokerage','brokerage','Broker','USD') RETURNING id"""
        ).fetchone()[0]
        conversion_doc = con.execute(
            """INSERT INTO documents(
                   drive_id,drive_path,filename,doc_type,doc_date
               ) VALUES('fx-typed','investments/2026/sample.png','sample.png',
                        'fx_conversion','2026-01-03') RETURNING id"""
        ).fetchone()[0]
        con.executemany(
            """INSERT INTO transactions(
                   account_id,date,amount_minor,currency,category,flow_type,
                   source_doc_id
               ) VALUES(?,?,?,?,?,?,?)""",
            [
                (
                    account_id, "2026-01-02", 10_000, "USD", "fx",
                    "transfer", None,
                ),
                (
                    account_id, "2026-01-03", 20_000, "USD", "deposit",
                    None, conversion_doc,
                ),
            ],
        )
        con.execute(
            """INSERT INTO fx_rates(date,base_currency,quote_currency,rate,source)
               VALUES('2026-01-02','USD','ILS',3.5,'document')"""
        )

        builder = DashboardModelBuilder(con)
        external = builder.accounting.external_flows("2026-01-03")
        forex = builder._forex_activity([], "2026-01-03")

        self.assertEqual(external, [])
        self.assertEqual(forex["purchase_count"], 2)
        self.assertEqual(forex["external_event_count"], 0)
        self.assertTrue(
            all(row["kind"] == "currency_purchase" for row in forex["rows"])
        )

    def test_dividend_chart_keeps_non_payment_calendar_months(self):
        con = sqlite3.connect(":memory:")
        self.addCleanup(con.close)
        con.executescript(SCHEMA)
        account_id = con.execute(
            "INSERT INTO accounts(name,kind,institution,currency) VALUES(?,?,?,?)",
            ("Checking", "checking", "Bank", "ILS"),
        ).lastrowid
        con.executemany(
            """INSERT INTO transactions(
                   account_id,date,amount_minor,currency,category,flow_type,
                   counterparty
               ) VALUES(?,?,?,?,?,?,?)""",
            [
                (account_id, "2026-01-10", 1_000, "ILS", "dividend", "income", "Fund"),
                (account_id, "2026-04-10", 2_000, "ILS", "dividend", "income", "Fund"),
            ],
        )

        chart = DashboardModelBuilder(con)._dividend_chart("2026-04-30")

        self.assertEqual(chart["months"], ["2026-01", "2026-02", "2026-03", "2026-04"])
        self.assertEqual(chart["series"], [{"ticker": "Fund", "data": [10.0, 0, 0, 20.0]}])

    def test_profitable_short_has_positive_percentage_return(self):
        con = sqlite3.connect(":memory:")
        self.addCleanup(con.close)
        con.executescript(SCHEMA)
        account_id = con.execute(
            "INSERT INTO accounts(name,kind,institution,currency) VALUES(?,?,?,?)",
            ("Broker", "brokerage", "Broker", "ILS"),
        ).lastrowid
        security_id = con.execute(
            "INSERT INTO securities(ticker,name,asset_class,currency) VALUES(?,?,?,?)",
            ("SHORT", "Short", "stock", "ILS"),
        ).lastrowid
        con.execute(
            """INSERT INTO trades(
                   account_id,security_id,date,side,shares,price_minor,
                   fees_minor,currency
               ) VALUES(?,?,?,?,?,?,?,?)""",
            (account_id, security_id, "2026-01-01", "sell", 10, 10_000, 0, "ILS"),
        )
        con.execute(
            "INSERT INTO prices(security_id,date,close_minor,currency) VALUES(?,?,?,?)",
            (security_id, "2026-01-02", 8_000, "ILS"),
        )
        builder = DashboardModelBuilder(con)

        positions = builder._positions(builder.accounting.state_at("2026-01-02"))

        self.assertEqual(positions[0]["pnl_reporting"], 200.0)
        self.assertEqual(positions[0]["pnl_pct"], 20.0)


if __name__ == "__main__":
    unittest.main()
