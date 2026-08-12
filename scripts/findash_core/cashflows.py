"""Normalized cash-flow analysis derived from canonical transactions."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
import calendar
import sqlite3
from typing import Optional

from findash_core.accounting import AccountingEngine
from findash_core.categories import flow_type_for
from findash_core.currencies import canonical_currency
from findash_core.models import ZERO


def _month_add(ym: str, delta: int) -> str:
    year, month = map(int, ym.split("-"))
    index = year * 12 + month - 1 + delta
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def month_range(first: str, last: str) -> list[str]:
    if first > last:
        return []
    months = []
    current = first
    while current <= last:
        months.append(current)
        current = _month_add(current, 1)
    return months


@dataclass
class MonthlyFlow:
    month: str
    expenses_minor: Decimal = ZERO
    job_income_minor: Decimal = ZERO
    other_income_minor: Decimal = ZERO
    dividends_minor: Decimal = ZERO
    refunds_minor: Decimal = ZERO
    taxes_minor: Decimal = ZERO

    @property
    def total_income_minor(self) -> Decimal:
        """Income counted by the dashboard.

        Salary, dividends, and other receipts are mutually exclusive buckets.
        Taxes remain available for audit/detail views but are not silently
        folded into the compact income-versus-spend summary.
        """

        return (
            self.job_income_minor
            + self.dividends_minor
            + self.other_income_minor
        )

    @property
    def net_minor(self) -> Decimal:
        return self.total_income_minor - self.expenses_minor


@dataclass(frozen=True)
class Expense:
    transaction_id: int
    account_id: int
    date: str
    category: str
    description: str
    counterparty: str
    reporting_amount_minor: Decimal


@dataclass
class CashFlowReport:
    reporting_currency: str
    coverage_start: Optional[str]
    transaction_coverage_start: Optional[str] = None
    observed_months: frozenset[str] = frozenset()
    months: list[MonthlyFlow] = field(default_factory=list)
    expenses: list[Expense] = field(default_factory=list)

    def last(self, count: int) -> list[MonthlyFlow]:
        return self.months[-count:]

    @staticmethod
    def average(rows: list[MonthlyFlow], field_name: str) -> Decimal:
        if not rows:
            return ZERO
        return sum((getattr(row, field_name) for row in rows), ZERO) / len(rows)


class CashFlowEngine:
    def __init__(self, con: sqlite3.Connection, reporting_currency: str = "ILS"):
        self.con = con
        self.con.row_factory = sqlite3.Row
        self.accounting = AccountingEngine(con, reporting_currency)
        self.reporting_currency = canonical_currency(reporting_currency)
        self._tx_columns = {
            row[1] for row in con.execute("PRAGMA table_info(transactions)")
        }

    def _all_transactions(self, as_of: str) -> list[dict]:
        rows: list[dict] = []
        for account in self.accounting.accounts(as_of, include_closed=True):
            for tx in self.accounting.transactions(account["id"], as_of):
                row = dict(tx)
                row["account_id"] = account["id"]
                # Canonical rows deliberately expose only calculation fields.
                row.setdefault("counterparty", "")
                row.setdefault("description", "")
                rows.append(row)
        rows.sort(key=lambda row: (row["date"], row["id"]))
        return rows

    def build(self, as_of: str) -> CashFlowReport:
        date.fromisoformat(as_of)
        transactions = self._all_transactions(as_of)

        payslip_rows = self.con.execute(
            """SELECT id, period_start, period_end, paid_on, net_minor, currency
               FROM payslips
               WHERE COALESCE(paid_on, period_end, period_start)<=?
               ORDER BY COALESCE(paid_on, period_end, period_start), id""",
            (as_of,),
        ).fetchall()

        classified_transactions = [
            (
                tx,
                flow_type_for(
                    tx.get("category"), tx.get("flow_type"), tx["amount_minor"]
                ),
            )
            for tx in transactions
        ]
        # Investment/transfer history may begin years before bank cash-flow
        # coverage. It must not manufacture zero-spend months in the averages.
        transaction_starts = [
            tx["date"][:7]
            for tx, flow_type in classified_transactions
            if flow_type in {"expense", "refund", "tax", "income", "other"}
        ]
        starts = list(transaction_starts)
        starts.extend((row["paid_on"] or row["period_end"] or row["period_start"])[:7] for row in payslip_rows)
        observed_months = {
            tx["date"][:7]
            for tx, flow_type in classified_transactions
            if flow_type in {"expense", "refund", "tax", "income", "other"}
        }
        observed_months.update(
            (row["paid_on"] or row["period_end"] or row["period_start"])[:7]
            for row in payslip_rows
        )
        last_month = as_of[:7]
        first_month = min(starts) if starts else last_month
        monthly = {ym: MonthlyFlow(ym) for ym in month_range(first_month, last_month)}

        salary_observations: list[tuple[str, tuple[str, str, int], Decimal]] = []
        expenses: list[Expense] = []
        for tx, flow_type in classified_transactions:
            if flow_type not in {"expense", "refund", "tax", "income", "other"}:
                continue
            month = tx["date"][:7]
            amount = self.accounting.fx.convert_minor(
                tx["amount_minor"], tx["currency"], self.reporting_currency, tx["date"]
            )
            category = tx.get("category") or "Uncategorized"
            category_key = category.strip().casefold()
            if flow_type == "expense" and amount < ZERO:
                monthly[month].expenses_minor += abs(amount)
                expenses.append(
                    Expense(
                        transaction_id=int(tx["id"]),
                        account_id=int(tx["account_id"]),
                        date=tx["date"],
                        category=category,
                        description=tx.get("description") or "",
                        counterparty=tx.get("counterparty") or "",
                        reporting_amount_minor=abs(amount),
                    )
                )
            elif flow_type == "refund" and amount > ZERO:
                monthly[month].refunds_minor += amount
                monthly[month].other_income_minor += amount
            elif flow_type == "tax" and amount < ZERO:
                monthly[month].taxes_minor += abs(amount)
            elif flow_type == "other" and amount > ZERO:
                # Non-income receipts such as a personal-loan repayment still
                # belong in cash flow, without pretending they are earnings.
                monthly[month].other_income_minor += amount
            elif flow_type == "income" and amount > ZERO:
                if category_key == "salary":
                    # Cross-source reconciliation is deliberately exact and
                    # one-to-one.  The key is the bank posting date, native
                    # currency, and native minor-unit amount; no date window,
                    # amount tolerance, or employer-name guessing is applied.
                    salary_observations.append(
                        (
                            month,
                            (
                                tx["date"],
                                canonical_currency(tx["currency"]),
                                int(tx["amount_minor"]),
                            ),
                            amount,
                        )
                    )
                else:
                    if category_key == "dividend":
                        monthly[month].dividends_minor += amount
                    else:
                        monthly[month].other_income_minor += amount

        payslip_observations: list[
            tuple[str, Optional[tuple[str, str, int]], Decimal]
        ] = []
        for row in payslip_rows:
            paid_date = row["paid_on"] or row["period_end"] or row["period_start"]
            month = paid_date[:7]
            currency = canonical_currency(row["currency"])
            amount = self.accounting.fx.convert_minor(
                row["net_minor"], currency, self.reporting_currency, paid_date
            )
            # A fallback period boundary locates an unmatched payslip in time,
            # but it is not evidence of the bank posting date. Only an explicit
            # paid_on participates in cross-source reconciliation.
            match_key = (
                (row["paid_on"], currency, int(row["net_minor"]))
                if row["paid_on"]
                else None
            )
            payslip_observations.append((month, match_key, amount))

        # Start with every independently deduplicated observation, then remove
        # exactly one copy for every proven cross-source mirror. A monthly max can
        # erase an unmatched second employer/bonus from each side; multiset matching
        # preserves those distinct observations.
        available_salary_matches = Counter(
            key for _, key, _ in salary_observations
        )
        for month, _, amount in salary_observations:
            monthly[month].job_income_minor += amount
        for month, key, amount in payslip_observations:
            monthly[month].job_income_minor += amount
            if key is not None and available_salary_matches[key] > 0:
                monthly[month].job_income_minor -= amount
                available_salary_matches[key] -= 1

        expenses.sort(key=lambda row: (row.date, row.transaction_id), reverse=True)
        return CashFlowReport(
            reporting_currency=self.reporting_currency,
            coverage_start=f"{first_month}-01" if starts else None,
            transaction_coverage_start=(
                f"{min(transaction_starts)}-01" if transaction_starts else None
            ),
            observed_months=frozenset(observed_months),
            months=list(monthly.values()),
            expenses=expenses,
        )


__all__ = [
    "CashFlowEngine",
    "CashFlowReport",
    "Expense",
    "MonthlyFlow",
    "month_range",
]
