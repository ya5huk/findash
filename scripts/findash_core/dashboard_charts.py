"""Chart-series builders for the normalized dashboard model."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from typing import Optional

from findash_core.cashflows import CashFlowReport, month_range
from findash_core.currencies import minor_unit_factor
from findash_core.errors import AccountingError, MissingRateError
from findash_core.models import AccountState, ZERO


def _major(amount_minor: Decimal | int, currency: str) -> float:
    return float(Decimal(amount_minor) / minor_unit_factor(currency))


def _timeline(start: date, end: date, step_days: int = 1) -> list[str]:
    rows: list[str] = []
    cursor = start
    while cursor <= end:
        rows.append(cursor.isoformat())
        cursor += timedelta(days=step_days)
    if not rows or rows[-1] != end.isoformat():
        rows.append(end.isoformat())
    return rows


class DashboardChartsMixin:
    """Focused chart calculations mixed into ``DashboardModelBuilder``."""

    def _charts(
        self,
        current: AccountState,
        flows: CashFlowReport,
        external_flows,
        dividend_analysis: dict,
        as_of: str,
    ) -> dict:
        return {
            "netWorth": self._net_worth_chart(as_of, external_flows),
            "stocksVsSpy": self._benchmark_chart(external_flows, as_of),
            "payslips": self._payslip_chart(as_of),
            "expenses": self._expense_charts(flows, as_of),
            "dividends": self._dividend_chart(as_of, dividend_analysis),
        }

    def _net_worth_chart(self, as_of: str, external_flows=None) -> dict:
        evidence_dates = [
            str(row[0])
            for row in self.con.execute(
                """SELECT evidence_date FROM (
                       SELECT b.as_of AS evidence_date
                         FROM balances b JOIN accounts a ON a.id=b.account_id
                        WHERE a.opened_on IS NULL OR b.as_of>=a.opened_on
                       UNION
                       SELECT p.as_of
                         FROM positions p JOIN accounts a ON a.id=p.account_id
                        WHERE a.opened_on IS NULL OR p.as_of>=a.opened_on
                       UNION
                       SELECT t.date
                         FROM transactions t JOIN accounts a ON a.id=t.account_id
                        WHERE a.opened_on IS NULL OR t.date>=a.opened_on
                       UNION
                       SELECT t.date
                         FROM trades t JOIN accounts a ON a.id=t.account_id
                        WHERE a.opened_on IS NULL OR t.date>=a.opened_on
                       UNION
                       SELECT c.occurred_on
                         FROM canonical_events c JOIN accounts a ON a.id=c.account_id
                        WHERE c.status='confirmed'
                          AND (a.opened_on IS NULL OR c.occurred_on>=a.opened_on)
                   )
                   WHERE evidence_date<=?
                   ORDER BY evidence_date""",
                (as_of,),
            )
            if row[0]
        ]
        first = evidence_dates[0] if evidence_dates else None
        coverage_basis = "earliest_account_evidence"
        # A lone old statement cannot support a continuous daily line forever.
        # Start again after the last system-wide evidence gap longer than eighteen
        # months. Unlike a brokerage-specific rule, this preserves genuinely
        # continuous bank history for users who invested only later.
        for previous, current in zip(evidence_dates, evidence_dates[1:]):
            if (
                date.fromisoformat(current) - date.fromisoformat(previous)
            ).days > 548:
                first = current
                coverage_basis = "post_evidence_gap"
        if not first:
            return {
                "dates": [],
                "values": [],
                "breakdowns": [],
                "currency": self.reporting_currency,
            }
        end = date.fromisoformat(as_of)
        dates = _timeline(date.fromisoformat(first), end)
        values, breakdowns, kept_dates, coverage_breaks = [], [], [], []
        previous_items: dict[
            tuple[int, str, str, str], Optional[str]
        ] = {}
        previous_total: Optional[Decimal] = None
        for point in dates:
            try:
                state = self.accounting.state_at(point)
            except (AccountingError, MissingRateError):
                kept_dates.append(point)
                values.append(None)
                breakdowns.append([])
                coverage_breaks.append(False)
                previous_items = {}
                previous_total = None
                continue
            per_account: dict[str, Decimal] = defaultdict(lambda: ZERO)
            for row in state.rows:
                per_account[row.account_name] += row.reporting_value_minor
            item_anchors = {
                (row.account_id, row.item_type, row.item_key, row.currency):
                row.coverage.anchor_date
                for row in state.rows
            }
            new_snapshot_items = [
                row
                for row in state.rows
                if row.coverage.anchor_date is not None
                and row.reporting_value_minor != ZERO
                and (
                    (
                        row.account_id,
                        row.item_type,
                        row.item_key,
                        row.currency,
                    )
                    not in previous_items
                )
            ]
            reanchored_items = [
                row
                for row in state.rows
                if row.coverage.anchor_date is not None
                and row.reporting_value_minor != ZERO
                and (
                    row.account_id,
                    row.item_type,
                    row.item_key,
                    row.currency,
                )
                in previous_items
                and previous_items[
                        (
                            row.account_id,
                            row.item_type,
                            row.item_key,
                            row.currency,
                        )
                    ]
                    != row.coverage.anchor_date
            ]
            material_coverage_change = bool(
                (new_snapshot_items or reanchored_items)
                and previous_total is not None
                and abs(state.total_minor - previous_total)
                / max(abs(previous_total), Decimal("1"))
                > Decimal("0.10")
            )
            kept_dates.append(point)
            values.append(_major(state.total_minor, self.reporting_currency))
            coverage_breaks.append(
                bool(
                    previous_items
                    and material_coverage_change
                )
            )
            breakdowns.append(
                [
                    [name, _major(value, self.reporting_currency)]
                    for name, value in sorted(
                        per_account.items(), key=lambda item: item[1], reverse=True
                    )
                    if value != ZERO
                ]
            )
            previous_items = item_anchors
            previous_total = state.total_minor
        # A missing price or FX rate means the whole normalized state cannot be
        # valued at that point. Show the most recent continuous valuatable suffix
        # instead of surrounding a broken interval with apparently comparable
        # totals. Snapshot-introduced coverage changes inside that suffix remain
        # explicit line breaks.
        last_unvalued = max(
            (index for index, value in enumerate(values) if value is None),
            default=-1,
        )
        first_valid = last_unvalued + 1
        if first_valid:
            coverage_basis += "_continuous_valuation"
        return {
            "dates": kept_dates[first_valid:],
            "values": values[first_valid:],
            "breakdowns": breakdowns[first_valid:],
            "coverageBreaks": coverage_breaks[first_valid:],
            "currency": self.reporting_currency,
            "coverage_basis": coverage_basis,
        }

    def _benchmark_chart(self, external_flows, as_of: str) -> dict:
        flows = sorted(external_flows, key=lambda row: row.date)
        spy = self.con.execute(
            "SELECT id, currency FROM securities WHERE ticker='SPY'"
        ).fetchone()
        first = self.accounting.earliest_investment_date()
        if not spy or not first:
            return {
                "dates": [],
                "stocks": [],
                "spy": [],
                "cumulativeDeposits": [],
                "holdings": {},
                "currency": self.reporting_currency,
            }
        start = date.fromisoformat(first)
        dates = _timeline(start, date.fromisoformat(as_of))
        # An old whole-account brokerage total is exact only on its observation
        # date. Add those dates to the display axis so a prior partial event
        # cannot make the benchmark skip the only honest opening anchor.
        aggregate_dates = {
            str(row[0])
            for row in self.con.execute(
                """SELECT b.as_of
                     FROM balances b JOIN accounts a ON a.id=b.account_id
                    WHERE a.kind='brokerage' AND b.component IS NULL
                      AND b.as_of BETWEEN ? AND ?""",
                (first, as_of),
            )
        }
        dates = sorted(set(dates) | aggregate_dates)
        result = {
            "dates": [],
            "stocks": [],
            "spy": [],
            "cumulativeDeposits": [],
            "holdings": {},
            "currency": self.reporting_currency,
        }
        # Per-ticker market value of the held position in the security's own
        # currency, so a holding's history is not distorted by FX moves.
        holding_values: dict[str, list] = {}
        holding_currencies: dict[str, str] = {}
        seed_date: Optional[str] = None
        opening_spy_shares = ZERO
        for point in dates:
            try:
                state = self.accounting.state_at(point)
            except (AccountingError, MissingRateError):
                state = None
            # This chart is specifically a brokerage-vs-SPY comparison.  The
            # presentation-level Investments bucket can also contain a liquid
            # study fund, but its value and contributions are not part of the
            # brokerage funding series and must not be mixed into this line.
            investment_rows = (
                [row for row in state.rows if row.account_kind == "brokerage"]
                if state is not None
                else []
            )
            investments = (
                sum(
                    (row.reporting_value_minor for row in investment_rows),
                    ZERO,
                )
                if state is not None
                else None
            )

            point_index = len(result["dates"])
            per_ticker: dict[str, Decimal] = defaultdict(lambda: ZERO)
            if state is not None:
                for row in investment_rows:
                    if row.item_type == "security" and row.ticker:
                        per_ticker[row.ticker] += row.native_value_minor
                        holding_currencies[row.ticker] = row.currency
            for ticker, value in per_ticker.items():
                series = holding_values.setdefault(ticker, [None] * point_index)
                series.append(_major(value, holding_currencies[ticker]))
            for ticker, series in holding_values.items():
                if len(series) <= point_index:
                    series.append(None)

            def opening_row_is_complete(row) -> bool:
                if not row.coverage.is_partial:
                    return True
                # A whole-account aggregate is exact on its own observation
                # date even though carrying it forward is explicitly partial.
                # Historical users may have only these old snapshots, and the
                # benchmark needs only that dated opening value to seed.
                return (
                    row.coverage.method == "aggregate-snapshot"
                    and row.coverage.anchor_date == point
                )

            seedable_investments = bool(
                investments is not None
                and investments > ZERO
                and investment_rows
                and all(opening_row_is_complete(row) for row in investment_rows)
            )
            applicable = [flow for flow in flows if flow.date <= point]
            cumulative = sum(
                (flow.reporting_amount_minor for flow in applicable), ZERO
            )
            current_price = self.accounting.prices.latest(spy["id"], point)
            current_reporting_price: Optional[Decimal] = None
            if current_price:
                try:
                    current_reporting_price = self.accounting.fx.convert_minor(
                        current_price.close_minor,
                        current_price.currency,
                        self.reporting_currency,
                        point,
                    )
                except MissingRateError:
                    pass

            if (
                seed_date is None
                and seedable_investments
                and current_reporting_price not in (None, ZERO)
            ):
                seed_date = point
                opening_spy_shares = investments / current_reporting_price

            spy_shares = opening_spy_shares
            complete = True
            later_flows = (
                [flow for flow in applicable if seed_date and flow.date > seed_date]
                if seed_date
                else []
            )
            for flow in later_flows:
                price = self.accounting.prices.latest(spy["id"], flow.date)
                if not price:
                    complete = False
                    break
                try:
                    reporting_price = self.accounting.fx.convert_minor(
                        price.close_minor,
                        price.currency,
                        self.reporting_currency,
                        flow.date,
                    )
                except MissingRateError:
                    complete = False
                    break
                if reporting_price != ZERO:
                    spy_shares += flow.reporting_amount_minor / reporting_price
            spy_value: Optional[float] = None
            if complete and seed_date and current_reporting_price is not None:
                spy_value = _major(
                    spy_shares * current_reporting_price, self.reporting_currency
                )
            result["dates"].append(point)
            result["stocks"].append(
                _major(investments, self.reporting_currency)
                if investments is not None
                else None
            )
            result["spy"].append(spy_value)
            result["cumulativeDeposits"].append(
                _major(cumulative, self.reporting_currency)
            )
        # Only currently-held tickers are selectable in the UI; sold-out
        # histories would bloat the payload without a box to click.
        result["holdings"] = {
            ticker: {"currency": holding_currencies[ticker], "values": series}
            for ticker, series in holding_values.items()
            if series and series[-1]
        }
        return result

    def _payslip_chart(self, as_of: str) -> dict:
        by_period: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])
        for row in self._payslips(as_of):
            period = row["period"]
            by_period[period][0] += row["gross_reporting"]
            by_period[period][1] += row["net_reporting"]
        months = sorted(by_period)
        return {
            "months": months,
            "gross": [by_period[month][0] for month in months],
            "net": [by_period[month][1] for month in months],
            "currency": self.reporting_currency,
        }

    def _expense_charts(self, report: CashFlowReport, as_of: str) -> dict:
        end = date.fromisoformat(as_of)
        start = end - timedelta(days=29)
        recent_cutoff = start.isoformat()
        by_category: dict[str, list] = defaultdict(lambda: [ZERO, []])
        by_day: dict[str, list] = defaultdict(lambda: [ZERO, []])
        by_month_cat: dict[str, dict[str, Decimal]] = defaultdict(
            lambda: defaultdict(lambda: ZERO)
        )
        for row in report.expenses:
            amount = _major(
                row.reporting_amount_minor, self.reporting_currency
            )
            by_month_cat[row.date[:7]][row.category] += row.reporting_amount_minor
            if row.date >= recent_cutoff:
                merchant = row.counterparty or row.description or "—"
                by_category[row.category][0] += row.reporting_amount_minor
                by_category[row.category][1].append(
                    [row.date, merchant, amount]
                )
                by_day[row.date][0] += row.reporting_amount_minor
                by_day[row.date][1].append(
                    [merchant, row.category, amount]
                )
        categories = [
            {
                "category": category,
                "total": _major(value[0], self.reporting_currency),
                "count": len(value[1]),
                "txns": value[1],
            }
            for category, value in by_category.items()
        ]
        categories.sort(key=lambda row: row["total"], reverse=True)
        dates = [
            (start + timedelta(days=offset)).isoformat() for offset in range(30)
        ]
        monthly = []
        # The report already carries a continuous coverage calendar. Reuse it so
        # the all-time chart shows quiet months as zero instead of compressing time
        # to months that happened to contain an expense.
        transaction_start = (
            report.transaction_coverage_start[:7]
            if report.transaction_coverage_start
            else None
        )
        covered_months = (
            [row for row in report.months if row.month >= transaction_start]
            if transaction_start
            else []
        )
        for flow_month in covered_months:
            month = flow_month.month
            cats = by_month_cat.get(month, {})
            top = sorted(cats.items(), key=lambda item: item[1], reverse=True)
            monthly.append(
                {
                    "month": month,
                    "total": _major(
                        sum(cats.values(), ZERO), self.reporting_currency
                    ),
                    "top": [
                        [category, _major(value, self.reporting_currency)]
                        for category, value in top[:5]
                    ],
                    "income": self._monthly_income(flow_month),
                }
            )
        return {
            "currency": self.reporting_currency,
            "categories30": categories,
            "daily30": {
                "dates": dates,
                "totals": [
                    _major(by_day[d][0], self.reporting_currency)
                    for d in dates
                ],
                "txns": [by_day[d][1] for d in dates],
            },
            "monthlyAlltime": monthly,
        }

    def _monthly_income(self, flow_month) -> dict:
        # Refunds are carried inside other_income_minor; split them out so the
        # breakdown sums exactly to the counted total.
        currency = self.reporting_currency
        return {
            "total": _major(flow_month.total_income_minor, currency),
            "salary": _major(flow_month.job_income_minor, currency),
            "dividends": _major(flow_month.dividends_minor, currency),
            "refunds": _major(flow_month.refunds_minor, currency),
            "other": _major(
                flow_month.other_income_minor - flow_month.refunds_minor,
                currency,
            ),
        }

    def _dividend_chart(
        self, as_of: str, analysis: Optional[dict] = None
    ) -> dict:
        if analysis is None:
            recorded = self._dividends(as_of)
            analysis = self._dividend_analysis(recorded, as_of)
        modeled_rows = analysis["modeled_history"]
        upcoming_rows = analysis["upcoming"]
        point = date.fromisoformat(as_of)
        years = [str(year) for year in range(point.year - 3, point.year + 2)]
        window_rows = [
            row
            for row in modeled_rows + upcoming_rows
            if row["date"][:4] in years
        ]
        tickers = sorted({row["ticker"] for row in window_rows})
        ranked_tickers = sorted(
            tickers,
            key=lambda ticker: (
                -sum(
                    row["reporting_amount"]
                    for row in window_rows
                    if row["ticker"] == ticker
                ),
                ticker,
            ),
        )
        top_tickers = ranked_tickers[:5]
        partial = bool(
            analysis["history_incomplete"]
            or analysis["unavailable_security_count"]
            or analysis["valuation_failure_count"]
        )
        year_views = []
        for year in years:
            months = [f"{year}-{month:02d}" for month in range(1, 13)]
            monthly_totals = {
                (ticker, month): sum(
                    row["reporting_amount"]
                    for row in window_rows
                    if row["ticker"] == ticker and row["date"][:7] == month
                )
                for ticker in tickers
                for month in months
            }
            series = [
                {
                    "ticker": ticker,
                    "data": [monthly_totals[(ticker, month)] for month in months],
                }
                for ticker in top_tickers
            ]
            if len(ranked_tickers) > 5:
                series.append(
                    {
                        "ticker": "Other",
                        "data": [
                            sum(
                                monthly_totals[(ticker, month)]
                                for ticker in ranked_tickers[5:]
                            )
                            for month in months
                        ],
                    }
                )
            historical = sum(
                row["reporting_amount"]
                for row in modeled_rows
                if row["date"][:4] == year
            )
            forecast = sum(
                row["reporting_amount"]
                for row in upcoming_rows
                if row["date"][:4] == year
            )
            year_number = int(year)
            year_views.append(
                {
                    "year": year,
                    "months": months,
                    "series": series,
                    "total": (
                        historical + forecast
                        if analysis["source_enabled"]
                        else None
                    ),
                    "historical": historical,
                    "forecast": forecast,
                    "partial": partial,
                    "mode": (
                        "historical"
                        if year_number < point.year
                        else "current"
                        if year_number == point.year
                        else "forecast"
                    ),
                }
            )

        common = {
            "currentYear": str(point.year),
            "yearViews": year_views,
        }
        rows = modeled_rows
        basis = "modeled"
        name_key = "ticker"
        if not rows:
            rows = analysis["recorded"]
            basis = "recorded"
            name_key = "payer"
        if not rows:
            return {
                "months": [],
                "series": [],
                "currency": self.reporting_currency,
                "basis": basis,
                **common,
            }
        # Keep a real calendar axis. The client-side “1Y” view takes the last
        # twelve entries, so emitting only dividend-paying months would make a
        # sparse twelve-payment history look like one year even when it spans
        # several years.
        first_month = min(row["date"][:7] for row in rows)
        months = month_range(first_month, as_of[:7])
        payers = sorted({row[name_key] for row in rows})
        totals = {
            (payer, month): sum(
                row["reporting_amount"]
                for row in rows
                if row[name_key] == payer and row["date"][:7] == month
            )
            for payer in payers
            for month in months
        }
        ranked = sorted(
            payers,
            key=lambda payer: sum(
                totals[(payer, month)] for month in months
            ),
            reverse=True,
        )
        top = ranked[:5]
        series = [
            {
                "ticker": payer,
                "data": [totals[(payer, month)] for month in months],
            }
            for payer in top
        ]
        if len(ranked) > 5:
            series.append(
                {
                    "ticker": "Other",
                    "data": [
                        sum(
                            totals[(payer, month)]
                            for payer in ranked[5:]
                        )
                        for month in months
                    ],
                }
            )
        return {
            "months": months,
            "series": series,
            "currency": self.reporting_currency,
            "basis": basis,
            **common,
        }


__all__ = ["DashboardChartsMixin"]
