"""HTML-only dashboard presentation.

No SQL, FX lookup, account routing, or financial calculation belongs here.  The
module formats the already-normalized model and fills the committed template.
"""

from __future__ import annotations

from datetime import date, timedelta
import html
from pathlib import Path
import re
from typing import Iterable, Optional

from findash_core.script_json import json_for_script


def h(value) -> str:
    return "" if value is None else html.escape(str(value))


def fmt_money(amount: float, decimals: Optional[int] = None) -> str:
    if decimals is None:
        decimals = 0 if abs(amount) >= 10_000 else 2
    sign = "-" if amount < 0 else ""
    return f"{sign}{abs(amount):,.{decimals}f}"


def money_td(
    amount: Optional[float],
    currency: str,
    decimals: Optional[int] = None,
    *,
    show_currency: bool = True,
) -> str:
    if amount is None:
        return '<td class="amount num muted">—</td>'
    cls = "amount num"
    if amount > 0:
        cls += " pos"
    elif amount < 0:
        cls += " neg"
    tag = (
        f'<span class="currency-tag">{h(currency)}</span>'
        if show_currency
        else ""
    )
    return f'<td class="{cls}">{h(fmt_money(amount, decimals))}{tag}</td>'


def ledger(
    headers: Iterable[tuple[str, str]],
    rows: Iterable[str],
    footer: str = "",
    *,
    table_class: str = "",
) -> str:
    head = "".join(
        f'<th class="{h(cls)}">{h(label)}</th>' if cls else f"<th>{h(label)}</th>"
        for label, cls in headers
    )
    body = "".join(rows)
    foot = f"<tfoot>{footer}</tfoot>" if footer else ""
    classes = f"ledger {table_class}".strip()
    return (
        f'<div class="table-wrap"><table class="{h(classes)}">'
        f"<thead><tr>{head}</tr></thead><tbody>{body}</tbody>{foot}</table></div>"
    )


def _overview(model: dict) -> str:
    rows: list[str] = []
    currency = model["reporting_currency"]
    for bucket in model["overview"]["buckets"]:
        rows.append(f'<tr class="category-row"><td colspan="4">{h(bucket["name"])}</td></tr>')
        for account in bucket["accounts"]:
            method = ", ".join(account["methods"])
            rows.append(
                f'<tr><td dir="auto">{h(account["name"])}</td>'
                f'<td class="muted">{h(account["institution"])} · {h(method)}</td>'
                f'{money_td(account["value"], currency)}'
                f'<td class="muted num">{h(account["observed_through"] or "—")}</td></tr>'
            )
        rows.append(
            f'<tr class="subtotal-row"><td>Total {h(bucket["name"].lower())}</td><td></td>'
            f'{money_td(bucket["total"], currency)}<td></td></tr>'
        )
    rows.append(
        f'<tr class="grand-total-row"><td>Net worth</td><td></td>'
        f'{money_td(model["overview"]["total"], currency)}<td></td></tr>'
    )
    return ledger(
        [("Account", ""), ("Source", ""), ("Value", "num"), ("Observed", "num")],
        rows,
    )


def _flow_summary(model: dict) -> str:
    summary = model["cashflow"]
    currency = model["reporting_currency"]
    current_month = (
        "Jan", "Feb", "Mar", "Apr", "May", "Jun",
        "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
    )[date.fromisoformat(model["as_of"]).month - 1]
    windows = [
        summary["one_month"],
        summary["three_month"],
        summary["twelve_month"],
        summary["all_time"],
    ]
    metrics = [
        ("Spend", "expenses", -1),
        ("Total income", "total_income", 1),
        ("Job", "job_income", 1),
        ("Other income", "other_income", 1),
        ("Dividends", "dividends", 1),
        ("Net", "net", 1),
    ]
    rows = [
        (
            f'<tr class="flow-{h(key.replace("_", "-"))}"><td>{h(label)}</td>'
            + "".join(
                money_td(
                    values[key] * sign if values[key] is not None else None,
                    currency,
                    0,
                    show_currency=False,
                )
                for values in windows
            )
            + "</tr>"
        )
        for label, key, sign in metrics
    ]
    income_coverage = summary["coverage_start"] or "none"
    transaction_coverage = summary["transaction_coverage_start"] or "none"
    if summary["dividends_basis"] == "modeled_entitlement":
        dividend_note = (
            "Dividends use modeled gross entitlement from distribution history; "
            "recorded cash is retained separately for future confirmation."
        )
        if summary["dividends_partial"]:
            dividend_note += " Modeled coverage is partial."
    else:
        dividend_note = (
            "Modeled distribution coverage is unavailable; dividends currently "
            "use recorded cash."
        )
    return ledger(
        [
            ("Average per month", ""),
            (current_month, "num"),
            ("3M", "num"),
            ("12M", "num"),
            ("All time", "num"),
        ],
        rows,
        footer=(
            '<tr><td colspan="5" class="muted">'
            f'Income coverage: {h(income_coverage)}; transaction coverage: '
            f'{h(transaction_coverage)}. {h(dividend_note)} Salary, dividends, '
            'and other receipts are separate buckets. The all-time '
            f'average uses {summary["all_time"]["months"]} observed month'
            f'{"s" if summary["all_time"]["months"] != 1 else ""}, so '
            'missing-history gaps are not treated as zero activity.'
            '</td></tr>'
        ),
        table_class="flow-summary",
    )


def _positions(model: dict) -> str:
    if not model["positions"]:
        return '<p class="footnote"><em>No valued positions yet.</em></p>'
    currency = model["reporting_currency"]
    rows = []
    for row in model["positions"]:
        pnl = row["pnl_reporting"]
        pct = row["pnl_pct"]
        rows.append(
            f'<tr><td>{h(row["ticker"])}</td><td dir="auto">{h(row["account"])}</td>'
            f'<td class="num">{row["quantity"]:,.6g}</td>'
            f'{money_td(row["average_cost"] or 0, row["currency"], 2) if row["average_cost"] is not None else "<td class=\"num muted\">—</td>"}'
            f'{money_td(row["last_price"] or 0, row["currency"], 2) if row["last_price"] is not None else "<td class=\"num muted\">—</td>"}'
            f'{money_td(row["market_reporting"], currency)}'
            f'{money_td(pnl, currency) if pnl is not None else "<td class=\"num muted\">—</td>"}'
            f'<td class="num">{h(f"{pct:+.2f}%" if pct is not None else "—")}</td></tr>'
        )
    return ledger(
        [
            ("Ticker", ""), ("Account", ""), ("Qty", "num"),
            ("Avg cost", "num"), ("Price", "num"), ("Market value", "num"),
            ("P/L", "num"), ("P/L %", "num"),
        ],
        rows,
        table_class="positions-table",
    )


def _deposits(model: dict) -> str:
    if not model["deposits"]:
        return '<p class="footnote"><em>No brokerage funding flows ingested.</em></p>'
    currency = model["reporting_currency"]
    rows = [
        f'<tr><td class="num">{h(row["date"])}</td>'
        f'{money_td(row["amount"], row["currency"], 2)}'
        f'{money_td(row["reporting_amount"], currency, 2)}</tr>'
        for row in model["deposits"]
    ]
    net = sum(row["reporting_amount"] for row in model["deposits"])
    return ledger(
        [("Date", "num"), ("Native flow", "num"), (f"{currency} equivalent", "num")],
        rows,
        footer=f'<tr><td>Net external funding</td><td></td>{money_td(net, currency)}</tr>',
    )


def _trades(model: dict) -> str:
    if not model["trades"]:
        return '<p class="footnote"><em>No trades ingested.</em></p>'
    rows = [
        f'<tr><td class="num">{h(row["date"])}</td><td dir="auto">{h(row["account"])}</td>'
        f'<td>{h(row["ticker"])}</td><td>{h(row["side"])}</td>'
        f'<td class="num">{row["shares"]:,.6g}</td>'
        f'{money_td(row["price"], row["currency"], 2)}'
        f'{money_td(row["fees"], row["currency"], 2)}</tr>'
        for row in model["trades"]
    ]
    return ledger(
        [("Date", "num"), ("Account", ""), ("Ticker", ""), ("Side", ""), ("Shares", "num"), ("Price", "num"), ("Fees", "num")],
        rows,
    )


def _payslips(model: dict) -> str:
    if not model["payslips"]:
        return '<p class="footnote"><em>No payslips yet.</em></p>'
    currency = model["reporting_currency"]
    def stack(values: list[tuple[str, float]]) -> str:
        return (
            '<td class="num money-stack">'
            + "".join(
                f'<span><small>{h(label)}</small>{h(fmt_money(value, 0))}</span>'
                for label, value in values
            )
            + "</td>"
        )

    rows = []
    for row in model["payslips"][:24]:
        employer = str(row["employer"])
        rows.append(
            f'<tr><td class="num">{h(row["period"])}</td>'
            f'<td class="employer-cell" dir="auto" title="{h(employer)}">{h(employer)}</td>'
            f'{money_td(row["gross_reporting"], currency, 0, show_currency=False)}'
            f'{money_td(row["net_reporting"], currency, 0, show_currency=False)}'
            + stack(
                [
                    ("Tax", row["income_tax_reporting"]),
                    ("Bituah", row["social_security_reporting"]),
                    ("Health", row["health_insurance_reporting"]),
                ]
            )
            + stack(
                [
                    ("Employee", row["pension_employee_reporting"]),
                    ("Employer", row["pension_employer_reporting"]),
                ]
            )
            + stack(
                [
                    ("Employee", row["study_fund_employee_reporting"]),
                    ("Employer", row["study_fund_employer_reporting"]),
                ]
            )
            + f'<td class="num">{h(row["paid_on"])}</td></tr>'
        )
    return ledger(
        [
            ("Period", "num"),
            ("Employer", ""),
            (f"Gross ({currency})", "num"),
            (f"Net ({currency})", "num"),
            ("Deductions", "num"),
            ("Pension", "num"),
            ("Study fund", "num"),
            ("Paid", "num"),
        ],
        rows,
        table_class="payslips-table",
    )


def _expenses(model: dict) -> str:
    if not model["recent_expenses"]:
        return '<p class="footnote"><em>No consumer expenses in the last 30 days.</em></p>'
    currency = model["reporting_currency"]
    rows = []
    for row in model["recent_expenses"]:
        merchant = str(row["merchant"])
        rows.append(
            f'<tr><td class="num">{h(row["date"])}</td>'
            f'<td class="merchant-cell" dir="auto" title="{h(merchant)}">{h(merchant)}</td>'
            f'<td class="category-cell">{h(row["category"])}</td>'
            f'{money_td(-row["amount"], currency, 2)}</tr>'
        )
    count = len(rows)
    return ledger(
        [("Date", "num"), ("Merchant", ""), ("Category", ""), ("Amount", "num")],
        rows,
        footer=(
            f'<tr><td>{count} transaction{"s" if count != 1 else ""}</td>'
            f"<td></td><td></td>"
            f'{money_td(-model["recent_expenses_total"], currency, 2)}</tr>'
        ),
        table_class="expenses-table",
    )


def _dividend_table(rows: list[dict], currency: str) -> str:
    if not rows:
        return '<p class="footnote"><em>No recorded dividends.</em></p>'
    body = []
    for row in rows:
        payer = str(row["payer"])
        body.append(
            f'<tr><td class="num">{h(row["date"])}</td>'
            f'<td class="payer-cell" dir="auto" title="{h(payer)}">{h(payer)}</td>'
            f'{money_td(row["reporting_amount"], currency, 2)}</tr>'
        )
    return ledger(
        [("Date", "num"), ("Payer", ""), ("Recorded cash", "num")],
        body,
        table_class="dividends-table",
    )


def _modeled_dividend_table(
    rows: list[dict], currency: str, *, empty_message: str
) -> str:
    if not rows:
        return f'<p class="footnote"><em>{h(empty_message)}</em></p>'
    body = []
    for row in rows:
        ticker = str(row["ticker"])
        body.append(
            f'<tr><td class="num">{h(row["date"])}</td>'
            f'<td class="holding-cell" title="{h(ticker)}">{h(ticker)}</td>'
            f'{money_td(row["reporting_amount"], currency, 2)}</tr>'
        )
    return ledger(
        [("Event date", "num"), ("Holding", ""), ("Est. gross", "num")],
        body,
        table_class="dividends-table",
    )


def _dividends_yearly(model: dict) -> str:
    currency = model["reporting_currency"]
    yearly = model["dividend_analysis"]["yearly"]
    if not yearly:
        return ""
    rows = []
    for row in yearly:
        classes = []
        if row["is_current"]:
            classes.append("dividend-year-current")
        if row["is_next"]:
            classes.append("dividend-est")
        attr = f' class="{h(" ".join(classes))}"' if classes else ""
        rows.append(
            f'<tr{attr}><td class="num">{h(row["year"])}</td>'
            f'{money_td(row["modeled"], currency, 2)}'
            f'{money_td(row["recorded"], currency, 2)}</tr>'
        )
    table = ledger(
        [
            ("Year", "num"),
            ("Modeled gross", "num"),
            ("Recorded cash", "num"),
        ],
        rows,
        table_class="dividends-year-table",
    )
    return (
        '<div class="dividend-yearly"><p class="chart-title">By year</p>'
        f"{table}</div>"
    )


def _upcoming_dividends(model: dict) -> str:
    rows = model["dividend_analysis"]["upcoming"]
    return _modeled_dividend_table(
        rows,
        model["reporting_currency"],
        empty_message="No upcoming distribution estimate is available.",
    )


def _dividend_footnote(model: dict) -> str:
    analysis = model["dividend_analysis"]
    parts = [
        "Modeled gross uses market distribution events and the shares held before "
        "each event date. Forecasts assume today’s holdings and recent cadence; "
        "recorded cash stays separate and no withholding tax is assumed."
    ]
    if analysis["history_incomplete"]:
        parts.append(
            "Opening position history is incomplete, so early modeled years may "
            "omit holdings from before tracking began."
        )
    unavailable = (
        analysis["unavailable_security_count"]
        + analysis["valuation_failure_count"]
    )
    if unavailable:
        parts.append(
            f"Market or FX data was unavailable for {unavailable} "
            f'item{"s" if unavailable != 1 else ""}.'
        )
    if not analysis["source_enabled"]:
        parts.append(
            "No cached market distribution events were available; run the "
            "market refresh before expecting modeled or upcoming rows."
        )
    return h(" ".join(parts))


def _diagnostics(model: dict) -> str:
    rows = [
        f'<tr><td>{h(row["table"])}</td><td class="num">{row["rows"]:,}</td></tr>'
        for row in model["diagnostics"]
    ]
    return (
        '<p class="footnote">Raw financial rows are intentionally excluded from the delivered dashboard.</p>'
        + ledger([("Table", ""), ("Rows", "num")], rows)
    )


def substitutions(model: dict, template_dir: Path) -> dict[str, str]:
    state = model["state"]
    currency = model["reporting_currency"]
    warnings = sorted({warning for row in state.rows for warning in row.coverage.warnings})
    stale = [
        row for row in state.rows
        if row.coverage.stale_days is not None and row.coverage.stale_days > 30
    ]
    notice_parts = []
    if warnings:
        notice_parts.append(f"{len(warnings)} coverage warning(s)")
    if stale:
        notice_parts.append(f"{len(stale)} value(s) observed over 30 days ago")
    drift_banner = (
        f'<p class="footnote"><strong>Data coverage:</strong> {h(" · ".join(notice_parts))}</p>'
        if notice_parts else ""
    )
    methods = sorted({row.coverage.method for row in state.rows})
    footnote = (
        "Values come from the normalized account ledger. "
        f"Methods in use: {', '.join(methods) if methods else 'no account data'}. "
        "Snapshots are anchors; only later canonical events advance them."
    )
    cutoff = (date.fromisoformat(model["as_of"]) - timedelta(days=365)).isoformat()
    modeled_history = model["dividend_analysis"]["modeled_history"]
    recent_modeled = [row for row in modeled_history if row["date"] >= cutoff]
    chart_data = model["charts"]
    return {
        "AS_OF_DATE": h(model["as_of"]),
        "NET_WORTH_ILS": f'{h(fmt_money(model["overview"]["total"]))} {h(currency)}',
        "DRIFT_BANNER": drift_banner,
        "OVERVIEW_TABLE": _overview(model),
        "OVERVIEW_FOOTNOTE": h(footnote),
        "FLOW_SUMMARY": _flow_summary(model),
        "POSITIONS_TABLE": _positions(model),
        "IBKR_POSITIONS_TABLE": "",
        "DEPOSITS_TABLE": _deposits(model),
        "TRADES_TABLE": _trades(model),
        "PAYSLIPS_TABLE": _payslips(model),
        "EXPENSES_TABLE": _expenses(model),
        "MTD_EXPENSE_NOTE": (
            '<p class="breakdown-note">Spent '
            f'<strong>{h(fmt_money(model["recent_expenses_total"], 0))} {h(currency)}</strong> '
            'over the last 30 days. Transfers, investments, and taxes are excluded.</p>'
        ),
        "DIVIDENDS_YEARLY_SUMMARY": _dividends_yearly(model),
        "DIVIDENDS_RECENT_5_TABLE": _modeled_dividend_table(
            modeled_history[:5],
            currency,
            empty_message="No modeled dividend history is available.",
        ),
        "DIVIDENDS_RECENT_1Y_TABLE": _modeled_dividend_table(
            recent_modeled,
            currency,
            empty_message="No modeled dividends in the last year.",
        ),
        "DIVIDENDS_RECORDED_TABLE": _dividend_table(
            model["dividends"], currency
        ),
        "DIVIDENDS_UPCOMING_TABLE": _upcoming_dividends(model),
        "DIVIDENDS_FOOTNOTE": _dividend_footnote(model),
        "SQLITE_TABS": _diagnostics(model),
        "CHART_DATA_JSON": json_for_script(chart_data),
        "STYLES_CSS": (template_dir / "styles.css").read_text(encoding="utf-8"),
        "CHARTS_JS_APP": (template_dir / "charts.js").read_text(encoding="utf-8"),
        "CHART_JS": (template_dir / "vendor" / "chart.umd.min.js").read_text(encoding="utf-8"),
        "CHART_ADAPTER_JS": (template_dir / "vendor" / "chartjs-adapter-date-fns.bundle.min.js").read_text(encoding="utf-8"),
        "FONTS_INLINE_CSS": (template_dir / "vendor" / "fonts-inline.css").read_text(encoding="utf-8"),
    }


def render(model: dict, template_dir: Path) -> str:
    output = (template_dir / "dashboard.html.tpl").read_text(encoding="utf-8")
    values = substitutions(model, template_dir)
    placeholder = re.compile(r"\{\{([A-Z][A-Z0-9_]*)\}\}")
    unresolved = sorted(
        {match.group(1) for match in placeholder.finditer(output)} - values.keys()
    )
    if unresolved:
        raise ValueError(f"unresolved dashboard placeholders: {unresolved}")
    # Substitute the template exactly once. Imported text may legitimately contain
    # characters such as ``{{CHARTS_JS_APP}}``; repeatedly calling ``replace`` would
    # interpret that data as a second-generation template directive.
    return placeholder.sub(lambda match: values[match.group(1)], output)


__all__ = ["fmt_money", "h", "ledger", "render", "substitutions"]
