from __future__ import annotations

from decimal import Decimal
from pathlib import Path
import sqlite3
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from findash_core.cashflows import CashFlowEngine  # noqa: E402
from findash_core.dashboard_model import DashboardModelBuilder  # noqa: E402


def database() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript((ROOT / "scripts" / "init-db.sql").read_text())
    con.execute(
        "INSERT INTO accounts(name,kind,institution,currency) VALUES('Checking','checking','Bank','ILS')"
    )
    return con


class CashFlowEngineTest(unittest.TestCase):
    def open_database(self) -> sqlite3.Connection:
        con = database()
        self.addCleanup(con.close)
        return con

    def test_months_without_transactions_remain_in_average_denominator(self):
        con = self.open_database()
        con.executemany(
            """INSERT INTO transactions(account_id,date,amount_minor,currency,category)
               VALUES(1,?,?,?,?)""",
            [
                ("2026-01-05", -300_00, "ILS", "groceries"),
                ("2026-03-05", -300_00, "ILS", "groceries"),
            ],
        )

        report = CashFlowEngine(con).build("2026-03-31")

        self.assertEqual([row.month for row in report.months], ["2026-01", "2026-02", "2026-03"])
        self.assertEqual(
            report.average(report.months, "expenses_minor"), Decimal("20000")
        )
        summary = DashboardModelBuilder(con)._cashflow_summary(report)
        self.assertEqual(summary["one_month"]["months"], 1)
        self.assertEqual(summary["one_month"]["expenses"], 300.0)

    def test_trailing_windows_use_completed_calendar_months(self):
        con = self.open_database()
        months = [
            "2025-08", "2025-09", "2025-10", "2025-11", "2025-12",
            "2026-01", "2026-02", "2026-03", "2026-04", "2026-05",
            "2026-06", "2026-07",
        ]
        con.executemany(
            """INSERT INTO transactions(
                   account_id,date,amount_minor,currency,category,flow_type
               ) VALUES(1,?,?,?,?,?)""",
            [
                (f"{month}-05", -(index * 100_00), "ILS", "groceries", "expense")
                for index, month in enumerate(months, start=1)
            ]
            + [
                ("2026-08-05", -9_999_00, "ILS", "groceries", "expense")
            ],
        )

        report = CashFlowEngine(con).build("2026-08-06")
        summary = DashboardModelBuilder(con)._cashflow_summary(report)

        self.assertEqual(summary["one_month"]["expenses"], 9_999.0)
        self.assertEqual(summary["three_month"]["months"], 3)
        self.assertEqual(summary["three_month"]["expenses"], 1_100.0)
        self.assertEqual(summary["twelve_month"]["months"], 12)
        self.assertEqual(summary["twelve_month"]["expenses"], 650.0)

    def test_payslip_uses_payment_month_and_replaces_salary_deposit(self):
        con = self.open_database()
        con.execute(
            """INSERT INTO transactions(account_id,date,amount_minor,currency,category)
               VALUES(1,'2026-01-02',1000000,'ILS','salary')"""
        )
        con.execute(
            """INSERT INTO payslips(
                   period_start,period_end,paid_on,employer,gross_minor,net_minor,currency
               ) VALUES('2025-12-01','2025-12-31','2026-01-02','Employer',1500000,1000000,'ILS')"""
        )

        report = CashFlowEngine(con).build("2026-01-31")

        self.assertEqual(len(report.months), 1)
        self.assertEqual(report.months[0].job_income_minor, Decimal("1000000"))

    def test_one_payslip_does_not_erase_additional_salary_income(self):
        con = self.open_database()
        con.executemany(
            """INSERT INTO transactions(account_id,date,amount_minor,currency,category)
               VALUES(1,?,?,?,?)""",
            [
                ("2026-01-02", 100_000, "ILS", "salary"),
                ("2026-01-03", 50_000, "ILS", "salary"),
            ],
        )
        con.execute(
            """INSERT INTO payslips(
                   period_start,period_end,paid_on,employer,gross_minor,net_minor,currency
               ) VALUES('2025-12-01','2025-12-31','2026-01-02','Employer A',
                        150000,100000,'ILS')"""
        )

        report = CashFlowEngine(con).build("2026-01-31")

        self.assertEqual(report.months[0].job_income_minor, Decimal("150000"))

    def test_cross_source_salary_matching_preserves_unmatched_rows_on_both_sides(self):
        con = self.open_database()
        con.executemany(
            """INSERT INTO transactions(
                   account_id,date,amount_minor,currency,category,flow_type
               ) VALUES(1,?,?,?,?,?)""",
            [
                ("2026-01-02", 100_000, "ILS", "salary", "income"),
                ("2026-01-03", 60_000, "ILS", "salary", "income"),
            ],
        )
        con.executemany(
            """INSERT INTO payslips(
                   period_start,period_end,paid_on,employer,gross_minor,
                   net_minor,currency
               ) VALUES(?,?,?,?,?,?,?)""",
            [
                (
                    "2025-12-01",
                    "2025-12-31",
                    "2026-01-02",
                    "Employer A",
                    150_000,
                    100_000,
                    "ILS",
                ),
                (
                    "2026-01-01",
                    "2026-01-31",
                    "2026-01-04",
                    "Employer B",
                    80_000,
                    50_000,
                    "ILS",
                ),
            ],
        )

        report = CashFlowEngine(con).build("2026-01-31")

        # The exact A mirror counts once. Employer B's unmatched payslip and the
        # distinct unmatched bank credit both remain. A monthly max would return
        # only 160_000 here.
        self.assertEqual(report.months[0].job_income_minor, Decimal("210000"))

    def test_salary_reconciliation_never_uses_amount_or_date_tolerance(self):
        con = self.open_database()
        con.execute(
            """INSERT INTO transactions(
                   account_id,date,amount_minor,currency,category,flow_type
               ) VALUES(1,'2026-01-02',99999,'ILS','salary','income')"""
        )
        con.execute(
            """INSERT INTO payslips(
                   period_start,period_end,paid_on,employer,gross_minor,
                   net_minor,currency
               ) VALUES('2025-12-01','2025-12-31','2026-01-02',
                        'Employer',150000,100000,'ILS')"""
        )

        report = CashFlowEngine(con).build("2026-01-31")

        self.assertEqual(report.months[0].job_income_minor, Decimal("199999"))

    def test_all_time_expense_chart_keeps_quiet_covered_months(self):
        con = self.open_database()
        con.executemany(
            """INSERT INTO transactions(account_id,date,amount_minor,currency,category)
               VALUES(1,?,?,?,?)""",
            [
                ("2026-01-05", -300_00, "ILS", "groceries"),
                ("2026-03-05", -600_00, "ILS", "groceries"),
            ],
        )
        con.execute(
            """INSERT INTO transactions(
                   account_id,date,amount_minor,currency,category,flow_type
               ) VALUES(1,'2026-03-10',1000_00,'ILS','salary','income')"""
        )
        con.execute(
            """INSERT INTO transactions(
                   account_id,date,amount_minor,currency,category,flow_type
               ) VALUES(1,'2026-03-12',50_00,'ILS','refund','refund')"""
        )
        report = CashFlowEngine(con).build("2026-03-31")

        chart = DashboardModelBuilder(con)._expense_charts(report, "2026-03-31")

        no_income = {
            "total": 0.0,
            "salary": 0.0,
            "dividends": 0.0,
            "refunds": 0.0,
            "other": 0.0,
        }
        self.assertEqual(
            chart["monthlyAlltime"],
            [
                {
                    "month": "2026-01",
                    "total": 300.0,
                    "top": [["groceries", 300.0]],
                    "income": no_income,
                },
                {"month": "2026-02", "total": 0.0, "top": [], "income": no_income},
                {
                    "month": "2026-03",
                    "total": 600.0,
                    "top": [["groceries", 600.0]],
                    "income": {
                        "total": 1050.0,
                        "salary": 1000.0,
                        "dividends": 0.0,
                        "refunds": 50.0,
                        "other": 0.0,
                    },
                },
            ],
        )

    def test_payslip_only_month_does_not_claim_expense_coverage(self):
        con = self.open_database()
        con.execute(
            """INSERT INTO payslips(
                   period_start,period_end,paid_on,employer,gross_minor,
                   net_minor,currency
               ) VALUES('2025-12-01','2025-12-31','2026-01-02',
                        'Employer',150000,100000,'ILS')"""
        )
        con.execute(
            """INSERT INTO transactions(account_id,date,amount_minor,currency,category)
               VALUES(1,'2026-02-05',-30000,'ILS','groceries')"""
        )
        report = CashFlowEngine(con).build("2026-03-31")

        chart = DashboardModelBuilder(con)._expense_charts(report, "2026-03-31")

        self.assertEqual(
            [row["month"] for row in chart["monthlyAlltime"]],
            ["2026-02", "2026-03"],
        )
        self.assertEqual(
            [row["total"] for row in chart["monthlyAlltime"]],
            [300.0, 0.0],
        )

    def test_cashflow_summary_keeps_income_spend_and_net_on_one_denominator(self):
        con = self.open_database()
        con.execute(
            """INSERT INTO payslips(
                   period_start,period_end,paid_on,employer,gross_minor,
                   net_minor,currency
               ) VALUES('2025-12-01','2025-12-31','2026-01-02',
                        'Employer',150000,90000,'ILS')"""
        )
        con.execute(
            """INSERT INTO transactions(
                   account_id,date,amount_minor,currency,category,flow_type
               ) VALUES(1,'2026-02-05',-30000,'ILS','groceries','expense')"""
        )
        report = CashFlowEngine(con).build("2026-03-31")

        summary = DashboardModelBuilder(con)._cashflow_summary(report)

        self.assertEqual(summary["coverage_start"], "2026-01-01")
        self.assertEqual(summary["transaction_coverage_start"], "2026-02-01")
        self.assertEqual(summary["all_time"]["job_income"], 450.0)
        self.assertEqual(summary["all_time"]["expenses"], 150.0)
        self.assertEqual(summary["all_time"]["total_income"], 450.0)
        self.assertEqual(summary["all_time"]["net"], 300.0)

    def test_investment_transfer_history_does_not_widen_cashflow_coverage(self):
        con = self.open_database()
        con.executemany(
            """INSERT INTO transactions(
                   account_id,date,amount_minor,currency,category,flow_type
               ) VALUES(1,?,?,?,?,?)""",
            [
                ("2025-01-05", 100_000, "USD", "deposit", "transfer"),
                ("2026-02-05", -30_000, "ILS", "groceries", "expense"),
            ],
        )
        report = CashFlowEngine(con).build("2026-03-31")

        summary = DashboardModelBuilder(con)._cashflow_summary(report)

        self.assertEqual(report.transaction_coverage_start, "2026-02-01")
        self.assertEqual(summary["all_time"]["expenses"], 300.0)

    def test_all_time_average_does_not_turn_missing_history_into_zero_months(self):
        con = self.open_database()
        con.executemany(
            """INSERT INTO transactions(
                   account_id,date,amount_minor,currency,category,flow_type
               ) VALUES(1,?,?,?,?,?)""",
            [
                ("2024-01-05", -10_000, "ILS", "groceries", "expense"),
                ("2026-01-05", -30_000, "ILS", "groceries", "expense"),
            ],
        )

        report = CashFlowEngine(con).build("2026-02-28")
        summary = DashboardModelBuilder(con)._cashflow_summary(report)

        self.assertEqual(len(report.months), 26)
        self.assertEqual(summary["all_time"]["months"], 2)
        self.assertEqual(summary["all_time"]["expenses"], 200.0)

    def test_tax_is_not_consumer_expense(self):
        con = self.open_database()
        con.executemany(
            """INSERT INTO transactions(account_id,date,amount_minor,currency,category)
               VALUES(1,?,?,?,?)""",
            [
                ("2026-01-05", -100_00, "ILS", "tax"),
                ("2026-01-06", -20_00, "ILS", "groceries"),
            ],
        )

        report = CashFlowEngine(con).build("2026-01-31")

        self.assertEqual(report.months[0].expenses_minor, Decimal("2000"))
        self.assertEqual(report.months[0].taxes_minor, Decimal("10000"))
        self.assertEqual(len(report.expenses), 1)

    def test_cashflow_separates_dividends_from_other_positive_receipts(self):
        con = self.open_database()
        con.executemany(
            """INSERT INTO transactions(
                   account_id,date,amount_minor,currency,category,flow_type
               ) VALUES(1,'2026-01-05',?,?,?,?)""",
            [
                (10_000, "ILS", "dividend", "income"),
                (2_000, "ILS", "merchant_refund", "refund"),
                (8_000, "ILS", "loan_repayment", "other"),
                (9_000, "ILS", "brokerage_deposit", "transfer"),
                (-5_000, "ILS", "groceries", "expense"),
                (-1_000, "ILS", "income_tax", "tax"),
            ],
        )

        report = CashFlowEngine(con).build("2026-01-31")
        month = report.months[0]
        self.assertEqual(month.other_income_minor, Decimal("10000"))
        self.assertEqual(month.dividends_minor, Decimal("10000"))
        self.assertEqual(month.total_income_minor, Decimal("20000"))
        self.assertEqual(month.refunds_minor, Decimal("2000"))
        self.assertEqual(month.taxes_minor, Decimal("1000"))
        self.assertEqual(month.net_minor, Decimal("15000"))

        summary = DashboardModelBuilder(con)._cashflow_summary(report)["all_time"]
        self.assertEqual(summary["total_income"], 200.0)
        self.assertEqual(summary["other_income"], 100.0)
        self.assertEqual(summary["dividends"], 100.0)
        self.assertEqual(summary["refunds"], 20.0)
        self.assertEqual(summary["net"], 150.0)

    def test_cashflow_summary_uses_modeled_dividend_entitlement(self):
        con = self.open_database()
        con.executemany(
            """INSERT INTO transactions(
                   account_id,date,amount_minor,currency,category,flow_type
               ) VALUES(1,?,?,?,?,?)""",
            [
                ("2026-01-05", -600, "ILS", "groceries", "expense"),
                ("2026-02-05", 400, "ILS", "dividend", "income"),
            ],
        )
        report = CashFlowEngine(con).build("2026-04-30")
        analysis = {
            "source_enabled": True,
            "modeled_history": [
                {"date": "2026-02-01", "reporting_amount": 10.0}
            ],
            "history_incomplete": False,
            "unavailable_security_count": 0,
            "valuation_failure_count": 0,
        }

        summary = DashboardModelBuilder(con)._cashflow_summary(report, analysis)

        self.assertEqual(summary["dividends_basis"], "modeled_entitlement")
        self.assertFalse(summary["dividends_partial"])
        self.assertAlmostEqual(summary["three_month"]["dividends"], 10 / 3)
        self.assertAlmostEqual(
            summary["three_month"]["recorded_dividends"], 4 / 3
        )
        self.assertAlmostEqual(summary["three_month"]["total_income"], 10 / 3)
        self.assertAlmostEqual(
            summary["three_month"]["cash_total_income"], 4 / 3
        )
        self.assertAlmostEqual(summary["three_month"]["net"], (10 - 6) / 3)
        self.assertAlmostEqual(summary["three_month"]["cash_net"], (4 - 6) / 3)


if __name__ == "__main__":
    unittest.main()
