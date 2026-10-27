"""Build a presentation-neutral dashboard model from normalized accounting state."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
import sqlite3
from typing import Optional

from findash_core.accounting import AccountingEngine
from findash_core.errors import AccountingError, MissingRateError
from findash_core.cashflows import CashFlowEngine, CashFlowReport
from findash_core.categories import flow_type_for
from findash_core.currencies import (
    canonical_currency,
    major_to_minor,
    minor_unit_factor,
)
from findash_core.dividends import (
    DividendEvent,
    DividendProvider,
    DividendSeries,
    event_from_minor_fraction,
    fetch_many,
    project_between,
)
from findash_core.models import AccountState, ZERO
from findash_core.movement_classification import (
    fx_conversion_documents,
    is_internal_fx_transaction,
)
from findash_core.dashboard_charts import DashboardChartsMixin
from findash_core.ledger.rates import conversion_cost


def major(amount_minor: Decimal | int, currency: str) -> float:
    return float(Decimal(amount_minor) / minor_unit_factor(currency))


class DashboardModelBuilder(DashboardChartsMixin):
    def __init__(
        self,
        con: sqlite3.Connection,
        reporting_currency: str = "ILS",
        *,
        dividend_provider: Optional[DividendProvider] = None,
    ):
        self.con = con
        self.con.row_factory = sqlite3.Row
        self.reporting_currency = canonical_currency(reporting_currency)
        self.accounting = AccountingEngine(con, self.reporting_currency)
        self.cashflows = CashFlowEngine(con, self.reporting_currency)
        self.dividend_provider = dividend_provider

    def build(self, as_of: str) -> dict:
        current = self.accounting.state_at(as_of)
        external_flows = self.accounting.external_flows(as_of, include_closed=True)
        flows = self.cashflows.build(as_of)
        recent_expenses = self._recent_expenses(flows, as_of)
        recorded_dividends = self._dividends(as_of)
        dividend_analysis = self._dividend_analysis(recorded_dividends, as_of)
        deposits = self._deposits(external_flows)
        return {
            "as_of": as_of,
            "reporting_currency": self.reporting_currency,
            "state": current,
            "overview": self._overview(current),
            "positions": self._positions(current),
            "cashflow": self._cashflow_summary(flows, dividend_analysis),
            "recent_expenses": recent_expenses,
            "recent_expenses_total": sum(
                row["amount"] for row in recent_expenses
            ),
            "deposits": deposits,
            "forex_activity": self._forex_activity(deposits, as_of),
            "trades": self._trades(as_of),
            "payslips": self._payslips(as_of),
            "dividends": recorded_dividends,
            "dividend_analysis": dividend_analysis,
            "diagnostics": self._diagnostics(),
            "charts": self._charts(
                current, flows, external_flows, dividend_analysis, as_of
            ),
        }

    def _overview(self, state: AccountState) -> dict:
        by_account: dict[tuple[str, int], dict] = {}
        for row in state.rows:
            key = (row.bucket, row.account_id)
            summary = by_account.setdefault(
                key,
                {
                    "bucket": row.bucket,
                    "account_id": row.account_id,
                    "name": row.account_name,
                    "institution": row.institution,
                    "value": ZERO,
                    "observed": [],
                    "methods": set(),
                    "warnings": set(),
                },
            )
            summary["value"] += row.reporting_value_minor
            if row.coverage.observed_through:
                summary["observed"].append(row.coverage.observed_through)
            summary["methods"].add(row.coverage.method)
            summary["warnings"].update(row.coverage.warnings)

        buckets: list[dict] = []
        ordered = ["Cash", "Locked", "Investments", "Liabilities", "Other"]
        for bucket in ordered:
            accounts = []
            for (name, _), summary in sorted(
                by_account.items(), key=lambda item: (item[0][0], item[1]["name"])
            ):
                if name != bucket:
                    continue
                accounts.append(
                    {
                        **summary,
                        "value": major(summary["value"], self.reporting_currency),
                        # One fresh sub-ledger must not hide another stale one.
                        "observed_through": min(summary["observed"], default=None),
                        "methods": sorted(summary["methods"]),
                        "warnings": sorted(summary["warnings"]),
                    }
                )
            if not accounts:
                continue
            buckets.append(
                {
                    "name": bucket,
                    "accounts": accounts,
                    "total": sum(row["value"] for row in accounts),
                }
            )
        total = major(state.total_minor, self.reporting_currency)
        bucket_total = sum(bucket["total"] for bucket in buckets)
        if abs(total - bucket_total) > 0.005:
            raise AccountingError("normalized state total does not match its buckets")
        return {"buckets": buckets, "total": total}

    def _positions(self, state: AccountState) -> list[dict]:
        position_state = {
            (row.account_id, row.security_id): row for row in state.positions
        }
        result = []
        for row in state.rows:
            if row.item_type != "security" or row.security_id is None:
                continue
            pos = position_state[(row.account_id, row.security_id)]
            market = major(row.reporting_value_minor, self.reporting_currency)
            cost = (
                major(pos.reporting_cost_basis_minor, self.reporting_currency)
                if pos.reporting_cost_basis_minor is not None
                else None
            )
            pnl = market - cost if cost is not None else None
            result.append(
                {
                    "account_id": row.account_id,
                    "security_id": row.security_id,
                    "account": row.account_name,
                    "ticker": row.ticker,
                    "quantity": float(pos.quantity),
                    "currency": row.currency,
                    "average_cost": (
                        major(pos.average_cost_minor, row.currency)
                        if pos.average_cost_minor is not None
                        else None
                    ),
                    "last_price": (
                        major(row.unit_price_minor, row.currency)
                        if row.unit_price_minor is not None
                        else None
                    ),
                    "market_native": major(row.native_value_minor, row.currency),
                    "market_reporting": market,
                    "cost_reporting": cost,
                    "pnl_reporting": pnl,
                    # Short-position basis is signed negative so market value and
                    # absolute P/L reconcile naturally. Percentage return still
                    # uses capital magnitude; dividing by the signed basis would
                    # invert profitable/loss-making shorts.
                    "pnl_pct": pnl / abs(cost) * 100 if cost not in (None, 0) else None,
                    "observed_through": row.coverage.observed_through,
                    "method": row.coverage.method,
                }
            )
        result.sort(key=lambda item: item["market_reporting"], reverse=True)
        return result

    def _cashflow_summary(
        self,
        report: CashFlowReport,
        dividend_analysis: Optional[dict] = None,
    ) -> dict:
        modeled_dividends = (
            dividend_analysis is not None
            and bool(dividend_analysis.get("source_enabled"))
        )
        modeled_by_month: dict[str, Decimal] = defaultdict(Decimal)
        if modeled_dividends:
            for row in dividend_analysis.get("modeled_history", []):
                modeled_by_month[str(row["date"])[:7]] += Decimal(
                    str(row["reporting_amount"])
                )

        def averages(rows) -> dict:

            def metric_average(field_name: str):
                if not rows:
                    return None
                return major(
                    report.average(rows, field_name), self.reporting_currency
                )

            recorded_dividends = metric_average("dividends_minor")
            expected_dividends = recorded_dividends
            if modeled_dividends:
                expected_dividends = (
                    float(
                        sum(
                            (modeled_by_month[row.month] for row in rows),
                            Decimal("0"),
                        )
                        / len(rows)
                    )
                    if rows
                    else None
                )

            cash_total_income = metric_average("total_income_minor")
            cash_net = metric_average("net_minor")
            total_income = cash_total_income
            net = cash_net
            if (
                modeled_dividends
                and expected_dividends is not None
                and recorded_dividends is not None
                and cash_total_income is not None
                and cash_net is not None
            ):
                # Until cash-activity coverage is reliable, the visible income
                # summary uses modeled gross entitlement. Keep the recorded cash
                # values alongside it for the future confirmation UI.
                dividend_adjustment = expected_dividends - recorded_dividends
                total_income += dividend_adjustment
                net += dividend_adjustment

            return {
                "months": len(rows),
                "expenses": metric_average("expenses_minor"),
                "total_income": total_income,
                "cash_total_income": cash_total_income,
                "job_income": metric_average("job_income_minor"),
                "other_income": metric_average("other_income_minor"),
                "dividends": expected_dividends,
                "recorded_dividends": recorded_dividends,
                "refunds": metric_average("refunds_minor"),
                "taxes": metric_average("taxes_minor"),
                "net": net,
                "cash_net": cash_net,
            }

        current_month = report.months[-1:]
        completed_months = report.months[:-1]
        all_time_months = [
            row
            for row in report.months
            if row.month in report.observed_months
        ]

        return {
            "coverage_start": report.coverage_start,
            "transaction_coverage_start": report.transaction_coverage_start,
            "dividends_basis": (
                "modeled_entitlement" if modeled_dividends else "recorded_cash"
            ),
            "dividends_partial": bool(
                modeled_dividends
                and dividend_analysis is not None
                and (
                    dividend_analysis.get("history_incomplete")
                    or dividend_analysis.get("unavailable_security_count")
                    or dividend_analysis.get("valuation_failure_count")
                )
            ),
            "one_month": averages(current_month),
            "three_month": averages(completed_months[-3:]),
            "twelve_month": averages(completed_months[-12:]),
            "all_time": averages(all_time_months),
        }

    def _recent_expenses(self, report: CashFlowReport, as_of: str) -> list[dict]:
        cutoff = (date.fromisoformat(as_of) - timedelta(days=29)).isoformat()
        return [
            {
                "date": row.date,
                "category": row.category,
                "merchant": row.counterparty or row.description or "—",
                "amount": major(row.reporting_amount_minor, self.reporting_currency),
            }
            for row in report.expenses
            if row.date >= cutoff
        ]

    def _deposits(self, flows) -> list[dict]:
        result = []
        for row in sorted(
            flows,
            key=lambda flow: (flow.date, flow.transaction_id or 0),
            reverse=True,
        ):
            currency = canonical_currency(row.currency)
            point = self.accounting.fx.latest(
                currency, self.reporting_currency, row.date
            )
            result.append(
                {
                    "date": row.date,
                    "account_id": row.account_id,
                    "amount": major(row.native_amount_minor, currency),
                    "currency": currency,
                    "reporting_amount": major(
                        row.reporting_amount_minor, self.reporting_currency
                    ),
                    "rate": float(point.rate),
                    "rate_date": point.date,
                    "rate_source": point.source,
                    # A valuation rate is not proven to be the rate at which an
                    # external deposit was acquired. Only linked FX-conversion
                    # evidence may be presented as an actual applied rate.
                    "rate_is_actual": False,
                    "kind": (
                        "brokerage_funding"
                        if row.native_amount_minor > 0
                        else "brokerage_withdrawal"
                    ),
                }
            )
        return result

    def _recorded_conversion_rows(
        self, as_of: str
    ) -> tuple[list[dict], set[int], set[int]]:
        """Linked conversions: the user's rate beside that day's market rate."""

        rows: list[dict] = []
        event_ids: set[int] = set()
        document_ids: set[int] = set()
        if not self.con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='fx_conversions'"
        ).fetchone():
            return rows, event_ids, document_ids
        conversions = self.con.execute(
            """SELECT conversion.*,
                      sell.currency AS sell_currency,sell.amount_minor AS sell_amount,
                      buy.currency AS buy_currency,buy.amount_minor AS buy_amount
                 FROM fx_conversions AS conversion
                 JOIN accounts AS account ON account.id=conversion.account_id
                 JOIN canonical_events AS sell ON sell.id=conversion.sell_event_id
                 JOIN canonical_events AS buy ON buy.id=conversion.buy_event_id
                WHERE account.kind='brokerage' AND conversion.converted_on<=?
                  AND sell.status='confirmed' AND buy.status='confirmed'
                ORDER BY conversion.converted_on,conversion.id""",
            (as_of,),
        ).fetchall()
        for conversion in conversions:
            legs = (int(conversion["sell_event_id"]), int(conversion["buy_event_id"]))
            event_ids.update(legs)
            document_ids.update(
                int(row[0])
                for row in self.con.execute(
                    """SELECT DISTINCT source.source_doc_id
                         FROM event_evidence AS evidence
                         JOIN source_events AS source
                           ON source.id=evidence.source_event_id
                        WHERE evidence.canonical_event_id IN (?,?)
                          AND source.source_doc_id IS NOT NULL""",
                    legs,
                )
            )
            sell_currency = canonical_currency(conversion["sell_currency"])
            buy_currency = canonical_currency(conversion["buy_currency"])
            bought_foreign = buy_currency != self.reporting_currency
            currency = buy_currency if bought_foreign else sell_currency
            amount_minor = _decimal(
                conversion["buy_amount"] if bought_foreign else conversion["sell_amount"]
            )
            reporting_side = (
                (-_decimal(conversion["sell_amount"]), sell_currency)
                if bought_foreign
                else (_decimal(conversion["buy_amount"]), buy_currency)
            )
            try:
                reporting_minor = self.accounting.fx.convert_minor(
                    reporting_side[0],
                    reporting_side[1],
                    self.reporting_currency,
                    conversion["converted_on"],
                )
            except MissingRateError:
                reporting_minor = None
            actual = Decimal(str(conversion["actual_rate"]))
            market = (
                Decimal(str(conversion["market_rate"]))
                if conversion["market_rate"] is not None
                else None
            )
            rows.append(
                {
                    "date": conversion["converted_on"],
                    "amount": major(amount_minor, currency),
                    "currency": currency,
                    "reporting_amount": (
                        major(reporting_minor, self.reporting_currency)
                        if reporting_minor is not None
                        else None
                    ),
                    "rate": float(actual),
                    "rate_date": conversion["converted_on"],
                    "rate_source": "conversion",
                    "rate_is_actual": True,
                    "market_rate": float(market) if market is not None else None,
                    "market_rate_date": conversion["market_rate_date"],
                    "markup_pct": (
                        float(
                            (
                                conversion_cost(
                                    actual,
                                    market,
                                    sell_currency=sell_currency,
                                    base_currency=conversion["base_currency"],
                                )
                                * 100
                            ).quantize(Decimal("0.01"))
                        )
                        if market is not None
                        else None
                    ),
                    "kind": "currency_purchase" if bought_foreign else "currency_sale",
                }
            )
        return rows, event_ids, document_ids

    def _currency_purchase_rows(self, as_of: str) -> list[dict]:
        documents = fx_conversion_documents(self.con, as_of=as_of)
        rows, linked_events, represented = self._recorded_conversion_rows(as_of)
        for account in self.accounting.accounts(as_of, include_closed=True):
            if account["kind"] != "brokerage":
                continue
            for transaction in self.accounting.transactions(account["id"], as_of):
                source_doc_id = transaction.get("source_doc_id")
                if transaction.get("is_canonical") and int(transaction["id"]) in linked_events:
                    continue
                if not is_internal_fx_transaction(transaction, documents):
                    continue
                amount_minor = _decimal(transaction["amount_minor"])
                currency = canonical_currency(transaction["currency"])
                if amount_minor <= ZERO or currency == self.reporting_currency:
                    continue
                point = self.accounting.fx.latest(
                    currency, self.reporting_currency, transaction["date"]
                )
                reporting_minor = self.accounting.fx.convert_minor(
                    amount_minor,
                    currency,
                    self.reporting_currency,
                    transaction["date"],
                )
                if source_doc_id is not None:
                    represented.add(int(source_doc_id))
                rows.append(
                    {
                        "date": transaction["date"],
                        "amount": major(amount_minor, currency),
                        "currency": currency,
                        "reporting_amount": major(
                            reporting_minor, self.reporting_currency
                        ),
                        "rate": float(point.rate),
                        "rate_date": point.date,
                        "rate_source": point.source,
                        "rate_is_actual": (
                            point.date == transaction["date"]
                            and point.source.strip().casefold() == "document"
                        ),
                        "kind": "currency_purchase",
                    }
                )

        # Older ingestion could retain a filed conversion document and its
        # authoritative rate without a cash leg. Keep that evidence visible,
        # but never invent an amount for it.
        for document_id, document in documents.items():
            if document_id in represented or not document["doc_date"]:
                continue
            rate_rows = self.con.execute(
                """SELECT base_currency,quote_currency
                     FROM fx_rates
                    WHERE date=? AND lower(source)='document'
                      AND (base_currency=? OR quote_currency=?)""",
                (
                    document["doc_date"],
                    self.reporting_currency,
                    self.reporting_currency,
                ),
            ).fetchall()
            currencies = sorted(
                {
                    canonical_currency(
                        row["quote_currency"]
                        if row["base_currency"] == self.reporting_currency
                        else row["base_currency"]
                    )
                    for row in rate_rows
                }
                - {self.reporting_currency}
            )
            if not currencies:
                rows.append(
                    {
                        "date": document["doc_date"],
                        "amount": None,
                        "currency": None,
                        "reporting_amount": None,
                        "rate": None,
                        "rate_date": None,
                        "rate_source": None,
                        "rate_is_actual": False,
                        "kind": "currency_purchase",
                    }
                )
                continue
            for currency in currencies:
                point = self.accounting.fx.latest(
                    currency, self.reporting_currency, document["doc_date"]
                )
                rows.append(
                    {
                        "date": document["doc_date"],
                        "amount": None,
                        "currency": currency,
                        "reporting_amount": None,
                        "rate": float(point.rate),
                        "rate_date": point.date,
                        "rate_source": point.source,
                        "rate_is_actual": (
                            point.date == document["doc_date"]
                            and point.source.strip().casefold() == "document"
                        ),
                        "kind": "currency_purchase",
                    }
                )
        return rows

    def _forex_activity(self, deposits: list[dict], as_of: str) -> dict:
        external_rows = [
            dict(row)
            for row in deposits
            if row["amount"] != 0
        ]
        rows = list(external_rows)
        rows.extend(self._currency_purchase_rows(as_of))
        rows.sort(
            key=lambda row: (
                row["date"],
                row["kind"],
                row["currency"] or "",
            ),
            reverse=True,
        )
        cumulative_external_reporting_amount = ZERO
        cumulative_external_usd_amount = ZERO
        cumulative_usd_purchased_amount = ZERO
        for row in reversed(rows):
            external_transfer = row["kind"] in {
                "brokerage_funding",
                "brokerage_withdrawal",
            }
            if external_transfer and row["reporting_amount"] is not None:
                cumulative_external_reporting_amount += Decimal(
                    str(row["reporting_amount"])
                )
            if (
                external_transfer
                and row["currency"] == "USD"
                and row["amount"] is not None
            ):
                cumulative_external_usd_amount += Decimal(str(row["amount"]))
            if (
                row["kind"] in {"currency_purchase", "currency_sale"}
                and row["currency"] == "USD"
                and row["amount"] is not None
            ):
                cumulative_usd_purchased_amount += Decimal(str(row["amount"]))
            row["cumulative_external_reporting_amount"] = float(
                cumulative_external_reporting_amount
            )
            row["cumulative_usd_purchased_amount"] = float(
                cumulative_usd_purchased_amount
            )
            # Compatibility aliases for older snapshot renderers.
            row["cumulative_reporting_amount"] = float(
                cumulative_external_reporting_amount
            )
            row["cumulative_usd_amount"] = float(cumulative_external_usd_amount)
        totals: dict[str, dict[str, Decimal | int]] = defaultdict(
            lambda: {
                "count": 0,
                "foreign_amount": ZERO,
                "reporting_amount": ZERO,
                "actual_rate_count": 0,
            }
        )
        for row in external_rows:
            if (
                row["currency"] is None
                or row["amount"] is None
                or row["reporting_amount"] is None
            ):
                continue
            summary = totals[row["currency"]]
            summary["count"] += 1
            summary["foreign_amount"] += Decimal(str(row["amount"]))
            summary["reporting_amount"] += Decimal(
                str(row["reporting_amount"])
            )
            summary["actual_rate_count"] += int(row["rate_is_actual"])

        summaries = []
        for currency, values in totals.items():
            foreign_amount = values["foreign_amount"]
            reporting_amount = values["reporting_amount"]
            summaries.append(
                {
                    "currency": currency,
                    "count": values["count"],
                    "foreign_amount": float(foreign_amount),
                    "reporting_amount": float(reporting_amount),
                    "weighted_rate": (
                        float(reporting_amount / foreign_amount)
                        if foreign_amount != ZERO
                        else None
                    ),
                    "actual_rate_count": values["actual_rate_count"],
                }
            )
        summaries.sort(
            key=lambda row: (-row["reporting_amount"], row["currency"])
        )
        return {
            "rows": rows,
            "summary": summaries,
            "event_count": len(rows),
            "external_event_count": len(external_rows),
            "activity_count": len(rows),
            "valued_count": sum(
                row["reporting_amount"] is not None for row in external_rows
            ),
            "funding_count": sum(
                row["kind"] == "brokerage_funding" for row in rows
            ),
            "withdrawal_count": sum(
                row["kind"] == "brokerage_withdrawal" for row in rows
            ),
            "purchase_count": sum(
                row["kind"] in {"currency_purchase", "currency_sale"} for row in rows
            ),
            "total_reporting_amount": float(
                sum(
                    (
                        Decimal(str(row["reporting_amount"]))
                        for row in external_rows
                        if row["reporting_amount"] is not None
                    ),
                    ZERO,
                )
            ),
            "net_external_reporting_amount": float(
                sum(
                    (
                        Decimal(str(row["reporting_amount"]))
                        for row in external_rows
                        if row["reporting_amount"] is not None
                    ),
                    ZERO,
                )
            ),
        }

    def _trades(self, as_of: str) -> list[dict]:
        result: list[dict] = []
        for account in self.accounting.accounts(as_of, include_closed=True):
            if account["kind"] != "brokerage":
                continue
            for row in self.accounting.trades(account["id"], as_of):
                currency = canonical_currency(row["currency"])
                result.append(
                    {
                        "date": row["date"],
                        "account": account["name"],
                        "ticker": row["ticker"],
                        "side": row["side"],
                        "shares": float(row["shares"]),
                        "price": major(_decimal(row["price_minor"]), currency),
                        "fees": major(_decimal(row["fees_minor"] or 0), currency),
                        "currency": currency,
                    }
                )
        result.sort(key=lambda row: row["date"], reverse=True)
        return result

    def _payslips(self, as_of: str) -> list[dict]:
        rows = self.con.execute(
            """SELECT period_start, period_end, paid_on, employer, gross_minor,
                      net_minor, income_tax_minor, social_security_minor,
                      health_insurance_minor, pension_employee_minor,
                      pension_employer_minor, study_fund_employee_minor,
                      study_fund_employer_minor, currency
                 FROM payslips
                WHERE COALESCE(paid_on,period_end,period_start)<=?
                ORDER BY COALESCE(paid_on,period_end,period_start) DESC""",
            (as_of,),
        ).fetchall()
        result = []
        for row in rows:
            paid = row["paid_on"] or row["period_end"] or row["period_start"]
            currency = canonical_currency(row["currency"])

            def reporting(field: str) -> float:
                return major(
                    self.accounting.fx.convert_minor(
                        row[field], currency, self.reporting_currency, paid
                    ),
                    self.reporting_currency,
                )

            result.append(
                {
                    "period": row["period_start"][:7],
                    "paid_on": paid,
                    "employer": row["employer"],
                    "gross": major(row["gross_minor"], currency),
                    "net": major(row["net_minor"], currency),
                    "currency": currency,
                    "gross_reporting": reporting("gross_minor"),
                    "net_reporting": reporting("net_minor"),
                    "income_tax_reporting": reporting("income_tax_minor"),
                    "social_security_reporting": reporting(
                        "social_security_minor"
                    ),
                    "health_insurance_reporting": reporting(
                        "health_insurance_minor"
                    ),
                    "pension_employee_reporting": reporting(
                        "pension_employee_minor"
                    ),
                    "pension_employer_reporting": reporting(
                        "pension_employer_minor"
                    ),
                    "study_fund_employee_reporting": reporting(
                        "study_fund_employee_minor"
                    ),
                    "study_fund_employer_reporting": reporting(
                        "study_fund_employer_minor"
                    ),
                }
            )
        return result

    def _dividends(self, as_of: str) -> list[dict]:
        result: list[dict] = []
        for account in self.accounting.accounts(as_of, include_closed=True):
            for tx in self.accounting.transactions(account["id"], as_of):
                category = str(tx.get("category") or "").strip().casefold()
                if category != "dividend" or tx["amount_minor"] <= 0:
                    continue
                currency = canonical_currency(tx["currency"])
                value = self.accounting.fx.convert_minor(
                    tx["amount_minor"], currency, self.reporting_currency, tx["date"]
                )
                result.append(
                    {
                        "date": tx["date"],
                        "payer": tx.get("counterparty") or tx.get("description") or "Dividend",
                        "amount": major(tx["amount_minor"], currency),
                        "currency": currency,
                        "reporting_amount": major(value, self.reporting_currency),
                    }
                )
        result.sort(key=lambda row: row["date"], reverse=True)
        return result

    def _dividend_analysis(self, recorded: list[dict], as_of: str) -> dict:
        """Keep modeled entitlement separate from recorded brokerage cash."""

        current_year = date.fromisoformat(as_of).year
        next_year = current_year + 1
        securities = [
            (int(row["id"]), str(row["ticker"]), canonical_currency(row["currency"]))
            for row in self.con.execute(
                """SELECT DISTINCT s.id, s.ticker, s.currency
                     FROM securities s
                    WHERE EXISTS (
                              SELECT 1
                                FROM trades t JOIN accounts a ON a.id=t.account_id
                               WHERE t.security_id=s.id AND a.kind='brokerage'
                          )
                       OR EXISTS (
                              SELECT 1
                                FROM positions p JOIN accounts a ON a.id=p.account_id
                               WHERE p.security_id=s.id AND a.kind='brokerage'
                          )
                       OR EXISTS (
                              SELECT 1
                                FROM canonical_events c
                                JOIN accounts a ON a.id=c.account_id
                               WHERE c.security_id=s.id
                                 AND c.event_type='trade'
                                 AND c.status='confirmed'
                                 AND a.kind='brokerage'
                          )
                    ORDER BY s.id"""
            )
        ]
        ticker_by_security = {
            security_id: ticker
            for security_id, ticker, _ in securities
        }
        distribution_table_exists = bool(
            self.con.execute(
                """SELECT 1 FROM sqlite_master
                    WHERE type='table' AND name='security_distributions'"""
            ).fetchone()
        )
        refresh_table_exists = bool(
            self.con.execute(
                """SELECT 1 FROM sqlite_master
                    WHERE type='table'
                      AND name='security_distribution_refreshes'"""
            ).fetchone()
        )
        series_by_security: dict[int, DividendSeries] = {}
        unavailable = 0
        market_source_enabled = False
        if self.dividend_provider is not None:
            series_by_security, unavailable = fetch_many(
                self.dividend_provider, securities
            )
        elif distribution_table_exists:
            refresh_status: dict[int, str] = {}
            if refresh_table_exists:
                selected = set(ticker_by_security)
                refresh_status = {
                    int(row["security_id"]): str(row["status"])
                    for row in self.con.execute(
                        """SELECT security_id,status
                             FROM security_distribution_refreshes
                            WHERE lower(source)='yahoo'"""
                    )
                    if int(row["security_id"]) in selected
                }
                market_source_enabled = bool(refresh_status)
                unavailable += sum(
                    refresh_status.get(security_id) in (None, "failed")
                    for security_id in selected
                )
            cached: dict[int, list[DividendEvent]] = defaultdict(list)
            currencies: dict[int, str] = {}
            for row in self.con.execute(
                """SELECT security_id,event_date,
                          amount_per_share_minor_numerator,
                          amount_per_share_minor_denominator,currency,source_key
                     FROM security_distributions
                    WHERE lower(source)='yahoo'
                    ORDER BY security_id,event_date,id"""
            ):
                security_id = int(row["security_id"])
                if security_id not in ticker_by_security:
                    # A stale cache row may outlive the holding evidence that
                    # originally selected it. It is reference data, not a reason
                    # to put the security back into the entitlement universe.
                    continue
                try:
                    event = event_from_minor_fraction(
                        event_date=str(row["event_date"]),
                        numerator=int(row["amount_per_share_minor_numerator"]),
                        denominator=int(
                            row["amount_per_share_minor_denominator"]
                        ),
                        currency=str(row["currency"]),
                        source_key=str(row["source_key"]),
                    )
                except (TypeError, ValueError):
                    unavailable += 1
                    continue
                cached[security_id].append(event)
                currencies[security_id] = event.currency
            series_by_security = {
                security_id: DividendSeries(tuple(events), currencies[security_id])
                for security_id, events in cached.items()
            }
            if not refresh_table_exists:
                # Compatibility with a pre-coverage cache: usable rows remain
                # visible, but empty securities cannot be distinguished.
                market_source_enabled = bool(series_by_security)

        quantity_cache: dict[str, dict[int, Decimal]] = {}

        def quantities_on(point: str) -> dict[int, Decimal]:
            if point not in quantity_cache:
                quantity_cache[point] = self.accounting.security_quantities_at(point)
            return quantity_cache[point]

        valuation_failures = 0

        def modeled_row(
            security_id: int,
            event: DividendEvent,
            quantity: Decimal,
            *,
            kind: str,
        ) -> Optional[dict]:
            nonlocal valuation_failures
            if quantity <= ZERO:
                return None
            native_minor = Decimal(
                major_to_minor(
                    quantity * event.amount_per_share_major,
                    event.currency,
                )
            )
            if native_minor <= ZERO:
                return None
            try:
                reporting_minor = self.accounting.fx.convert_minor(
                    native_minor,
                    event.currency,
                    self.reporting_currency,
                    event.event_date,
                ).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
            except MissingRateError:
                valuation_failures += 1
                return None
            return {
                "security_id": security_id,
                "date": event.event_date,
                "ticker": ticker_by_security[security_id],
                "quantity": float(quantity),
                "amount": major(native_minor, event.currency),
                "currency": event.currency,
                "reporting_amount": major(
                    reporting_minor, self.reporting_currency
                ),
                "kind": kind,
            }

        history: list[dict] = []
        upcoming: list[dict] = []
        current_quantities = quantities_on(as_of)
        end_next_year = f"{next_year:04d}-12-31"
        for security_id, series in series_by_security.items():
            events = series.events
            for event in events:
                if event.event_date <= as_of:
                    prior = (
                        date.fromisoformat(event.event_date) - timedelta(days=1)
                    ).isoformat()
                    row = modeled_row(
                        security_id,
                        event,
                        quantities_on(prior).get(security_id, ZERO),
                        kind="historical",
                    )
                    if row is not None:
                        history.append(row)
                elif event.event_date <= end_next_year:
                    row = modeled_row(
                        security_id,
                        event,
                        current_quantities.get(security_id, ZERO),
                        kind="declared",
                    )
                    if row is not None:
                        upcoming.append(row)

            for event in project_between(
                events, after=as_of, through=end_next_year
            ):
                row = modeled_row(
                    security_id,
                    event,
                    current_quantities.get(security_id, ZERO),
                    kind="projected",
                )
                if row is not None:
                    upcoming.append(row)

        # A provider can contain a declared event and a cadence projection on the
        # same date. Prefer the declared market row without summing both.
        upcoming_by_key: dict[tuple[int, str], dict] = {}
        for row in sorted(
            upcoming,
            key=lambda item: (
                item["date"],
                0 if item["kind"] == "projected" else 1,
            ),
        ):
            upcoming_by_key[(row["security_id"], row["date"])] = row
        history.sort(key=lambda row: (row["date"], row["ticker"]), reverse=True)
        upcoming = sorted(
            upcoming_by_key.values(), key=lambda row: (row["date"], row["ticker"])
        )

        modeled_by_year: dict[int, float] = defaultdict(float)
        for row in history + upcoming:
            modeled_by_year[int(row["date"][:4])] += row["reporting_amount"]
        recorded_by_year: dict[int, float] = defaultdict(float)
        for row in recorded:
            recorded_by_year[int(row["date"][:4])] += row["reporting_amount"]
        years = sorted(
            set(modeled_by_year)
            | set(recorded_by_year)
            | {current_year, next_year},
            reverse=True,
        )

        history_accounts = [
            (int(row["account_id"]), row["opened_on"])
            for row in self.con.execute(
                """SELECT DISTINCT a.id AS account_id,a.opened_on
                     FROM accounts a
                    WHERE a.kind='brokerage'
                      AND (
                          EXISTS (
                              SELECT 1 FROM trades t
                               WHERE t.account_id=a.id
                          )
                          OR EXISTS (
                              SELECT 1 FROM positions p
                               WHERE p.account_id=a.id
                          )
                          OR EXISTS (
                              SELECT 1 FROM canonical_events c
                               WHERE c.account_id=a.id
                                 AND c.event_type='trade'
                                 AND c.status='confirmed'
                          )
                      )"""
            )
        ]
        earliest_modeled = min(
            (row["date"] for row in history),
            default=as_of,
        )
        history_incomplete = any(
            self.accounting._latest_complete_account_snapshot_date(
                account_id,
                max(
                    earliest_modeled,
                    str(opened_on) if opened_on else earliest_modeled,
                ),
            )
            is None
            for account_id, opened_on in history_accounts
        )
        yearly = [
            {
                "year": str(year),
                "modeled": modeled_by_year.get(year),
                # Missing cash evidence is unknown, not proof of a zero receipt.
                "recorded": recorded_by_year.get(year),
                "is_current": year == current_year,
                "is_next": year == next_year,
                "partial": bool(
                    history_incomplete
                    or unavailable
                    or valuation_failures
                ),
            }
            for year in years
        ]
        return {
            "recorded": recorded,
            "modeled_history": history,
            "upcoming": upcoming,
            "yearly": yearly,
            "source_enabled": bool(
                self.dividend_provider is not None or market_source_enabled
            ),
            "unavailable_security_count": unavailable,
            "valuation_failure_count": valuation_failures,
            "history_incomplete": history_incomplete,
        }

    def _diagnostics(self) -> list[dict]:
        result = []
        for row in self.con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ):
            name = row[0]
            quoted_name = str(name).replace('"', '""')
            count = self.con.execute(
                f'SELECT COUNT(*) FROM "{quoted_name}"'
            ).fetchone()[0]
            result.append({"table": name, "rows": count})
        return result


def _decimal(value) -> Decimal:
    return Decimal(str(value))


__all__ = ["DashboardModelBuilder", "major"]
