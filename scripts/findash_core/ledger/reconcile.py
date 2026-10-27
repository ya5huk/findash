"""Daily ledger reconciliation: expect, match, and list what is still open.

Mechanical matching only. Anything with more than one plausible explanation
stays open for Claude to resolve with intents.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from fractions import Fraction
import sqlite3
import statistics
from typing import Optional

from findash_core.accounting import AccountingEngine
from findash_core.currencies import minor_to_major
from findash_core.errors import AccountingError

from .engine import CashFacts, LedgerWriter
from .rates import (
    conversion_cost,
    implied_rate,
    market_rate,
    rate_orientation,
    within_band,
)


LOOKBACK_DAYS = 45
TRANSFER_ARRIVAL_DAYS = 7
DIVIDEND_PAYMENT_DAYS = 45
STALE_REVIEW_DAYS = 14
ESCALATE_AFTER_DAYS = 7
RAW_SQL_WINDOW_DAYS = 14
DEFAULT_WITHHOLDING = Decimal("0.25")
DIVIDEND_TOLERANCE = Decimal("0.02")


@dataclass
class OpenItem:
    kind: str
    age_days: int
    account_id: Optional[int] = None
    event_id: Optional[int] = None
    detail: dict = field(default_factory=dict)

    @property
    def escalate(self) -> bool:
        return self.kind == "raw_sql_used" or self.age_days > ESCALATE_AFTER_DAYS

    def as_dict(self) -> dict:
        return {**asdict(self), "escalate": self.escalate}


@dataclass(frozen=True)
class Residual:
    account_id: int
    currency: str
    start: str
    end: str
    amount_minor: int


@dataclass(frozen=True)
class Candidate:
    kind: str
    event_ids: tuple[int, ...]
    amount_minor: int = 0
    rate: Optional[Decimal] = None


def _days_between(start: str, end: str) -> int:
    return (date.fromisoformat(end) - date.fromisoformat(start)).days


def _shift(day: str, days: int) -> str:
    return (date.fromisoformat(day) + timedelta(days=days)).isoformat()


class Reconciler:
    def __init__(self, connection: sqlite3.Connection, *, today: str):
        self.con = connection
        self.con.row_factory = sqlite3.Row
        self.today = date.fromisoformat(today).isoformat()
        self.since = _shift(self.today, -LOOKBACK_DAYS)
        self.writer = LedgerWriter(connection)
        self.actions: Counter[str] = Counter()

    # ----------------------------------------------------------------- run

    def run(self) -> dict:
        self._fill_market_rates()
        self._expect_dividends()
        for residual in self._residuals():
            self._auto_resolve(residual)
        items = [item.as_dict() for item in self._open_items()]
        summary = ", ".join(f"{k}={v}" for k, v in sorted(self.actions.items())) or "no changes"
        self.con.execute(
            "INSERT INTO ledger_log(kind,summary) VALUES ('reconcile',?)",
            (f"{summary}; open={len(items)}",),
        )
        return {
            "today": self.today,
            "actions": dict(sorted(self.actions.items())),
            "open_items": items,
            "escalate_count": sum(item["escalate"] for item in items),
        }

    # -------------------------------------------------------- market rates

    def _fill_market_rates(self) -> None:
        for row in self.con.execute(
            """SELECT id,base_currency,quote_currency,converted_on
                 FROM fx_conversions WHERE market_rate IS NULL"""
        ).fetchall():
            market = market_rate(
                self.con, row["base_currency"], row["quote_currency"], row["converted_on"]
            )
            if market is None:
                continue
            self.con.execute(
                "UPDATE fx_conversions SET market_rate=?,market_rate_date=? WHERE id=?",
                (float(market.rate), market.date, row["id"]),
            )
            self.actions["market_rate_filled"] += 1

    # ----------------------------------------------------------- dividends

    def _withholding_rate(self, account_id: int) -> Decimal:
        rates = [
            Decimal(row[1]) / Decimal(row[0])
            for row in self.con.execute(
                """SELECT gross_minor,withholding_minor FROM dividend_events
                    WHERE account_id=? AND gross_minor>0 AND withholding_minor IS NOT NULL""",
                (account_id,),
            )
        ]
        return statistics.median(rates) if rates else DEFAULT_WITHHOLDING

    def _already_received(self, account_id: int, ex_date: str, expected: int, currency: str) -> bool:
        """A pre-engine dividend row for this payment is already on the books."""

        window_end = _shift(ex_date, DIVIDEND_PAYMENT_DAYS)
        tolerance = max(2, int(abs(expected) * DIVIDEND_TOLERANCE))
        engine = AccountingEngine(self.con)
        return any(
            str(tx["category"] or "").casefold() == "dividend"
            and tx["currency"] == currency
            and ex_date <= tx["date"] <= window_end
            and abs(int(tx["amount_minor"]) - expected) <= tolerance
            and not self.con.execute(
                "SELECT 1 FROM dividend_events WHERE cash_event_id=?", (tx["id"],)
            ).fetchone()
            for tx in engine.transactions(account_id, min(window_end, self.today))
        )

    def _expect_dividends(self) -> None:
        engine = AccountingEngine(self.con)
        distributions = self.con.execute(
            """SELECT d.id,d.security_id,d.event_date,d.currency,
                      d.amount_per_share_minor_numerator AS numerator,
                      d.amount_per_share_minor_denominator AS denominator
                 FROM security_distributions AS d
                WHERE d.event_date BETWEEN ? AND ?
                ORDER BY d.event_date,d.security_id,d.id""",
            (self.since, self.today),
        ).fetchall()
        for account in engine.accounts(self.today):
            if account["kind"] != "brokerage":
                continue
            withholding = self._withholding_rate(int(account["id"]))
            for row in distributions:
                if self.con.execute(
                    """SELECT 1 FROM dividend_events
                        WHERE account_id=? AND security_id=? AND ex_date=?""",
                    (account["id"], row["security_id"], row["event_date"]),
                ).fetchone():
                    continue
                held = engine.security_quantities_at(
                    _shift(row["event_date"], -1), account_id=int(account["id"])
                ).get(int(row["security_id"]))
                if not held or held <= 0:
                    continue
                per_share = Fraction(int(row["numerator"]), int(row["denominator"]))
                gross = Decimal(per_share.numerator) / Decimal(per_share.denominator) * held
                net = int((gross * (1 - withholding)).quantize(Decimal(1), rounding=ROUND_HALF_UP))
                if net <= 0 or self._already_received(
                    int(account["id"]), row["event_date"], net, row["currency"]
                ):
                    continue
                facts = CashFacts(
                    account=account,
                    occurred_on=row["event_date"],
                    amount_minor=net,
                    currency=row["currency"],
                    flow_type="income",
                    category="dividend",
                    component=f"cash_{row['currency'].lower()}",
                )
                event_id = self.writer.new_cash_event(
                    facts,
                    status="needs_review",
                    notes="Expected from shares held on the ex-date; confirmed when paid.",
                )
                self.con.execute(
                    """INSERT INTO dividend_events(
                           cash_event_id,account_id,security_id,ex_date,quantity,
                           expected_net_minor,distribution_id
                       ) VALUES (?,?,?,?,?,?,?)""",
                    (
                        event_id,
                        account["id"],
                        row["security_id"],
                        row["event_date"],
                        float(held),
                        net,
                        row["id"],
                    ),
                )
                self.actions["dividend_expected"] += 1

    # ----------------------------------------------------- cash residuals

    def _residuals(self) -> list[Residual]:
        engine = AccountingEngine(self.con)
        result: list[Residual] = []
        accounts = self.con.execute(
            "SELECT id FROM accounts WHERE kind='brokerage' ORDER BY id"
        ).fetchall()
        for account in accounts:
            snapshots = self.con.execute(
                """SELECT as_of,currency,amount_minor FROM balances
                    WHERE account_id=? AND component='cash_' || lower(currency)
                      AND as_of<=?
                    ORDER BY currency,as_of""",
                (account["id"], self.today),
            ).fetchall()
            by_currency: dict[str, list[sqlite3.Row]] = {}
            for row in snapshots:
                by_currency.setdefault(row["currency"], []).append(row)
            for currency, rows in by_currency.items():
                for before, after in zip(rows, rows[1:]):
                    if after["as_of"] < self.since:
                        continue
                    try:
                        movement, trades = engine.brokerage_cash_movement_between(
                            int(account["id"]), currency, before["as_of"], after["as_of"]
                        )
                    except AccountingError:
                        continue
                    residual = (
                        Decimal(after["amount_minor"]) - Decimal(before["amount_minor"]) - movement
                    )
                    if abs(residual) > max(1, trades):
                        result.append(
                            Residual(
                                account_id=int(account["id"]),
                                currency=currency,
                                start=before["as_of"],
                                end=after["as_of"],
                                amount_minor=int(residual.to_integral_value(ROUND_HALF_UP)),
                            )
                        )
        return result

    def _expected_inflows(self, residual: Residual) -> list[sqlite3.Row]:
        return self.con.execute(
            """SELECT event.* FROM canonical_events AS event
                 JOIN transfer_links AS link ON link.inflow_event_id=event.id
                WHERE event.account_id=? AND event.status='needs_review'
                  AND event.occurred_on<=?
                  AND event.occurred_on>=?
                  AND NOT EXISTS (
                    SELECT 1 FROM event_evidence WHERE canonical_event_id=event.id
                  )""",
            (
                residual.account_id,
                residual.end,
                _shift(residual.end, -TRANSFER_ARRIVAL_DAYS),
            ),
        ).fetchall()

    def _expected_dividends(self, residual: Residual) -> list[sqlite3.Row]:
        return self.con.execute(
            """SELECT event.*,dividend.ex_date FROM canonical_events AS event
                 JOIN dividend_events AS dividend ON dividend.cash_event_id=event.id
                WHERE event.account_id=? AND event.status='needs_review'
                  AND event.currency=? AND dividend.ex_date<=?
                  AND dividend.ex_date>=?""",
            (
                residual.account_id,
                residual.currency,
                residual.end,
                _shift(residual.end, -DIVIDEND_PAYMENT_DAYS),
            ),
        ).fetchall()

    def _candidates(self, residual: Residual) -> list[Candidate]:
        if residual.amount_minor <= 0:
            return []
        found: list[Candidate] = []
        for inflow in self._expected_inflows(residual):
            amount = int(inflow["amount_minor"])
            if inflow["currency"] == residual.currency:
                if abs(amount - residual.amount_minor) <= 1:
                    found.append(Candidate("transfer", (int(inflow["id"]),)))
                continue
            base, quote = rate_orientation(inflow["currency"], residual.currency)
            actual = implied_rate(
                amount, inflow["currency"], residual.amount_minor, residual.currency, base
            )
            market = market_rate(self.con, base, quote, inflow["occurred_on"])
            if market and within_band(
                conversion_cost(actual, market.rate, sell_currency=inflow["currency"], base_currency=base)
            ):
                found.append(
                    Candidate("transfer+fx", (int(inflow["id"]),), residual.amount_minor, actual)
                )
        dividends = self._expected_dividends(residual)
        tolerance = lambda expected: max(2, int(abs(expected) * DIVIDEND_TOLERANCE))  # noqa: E731
        for dividend in dividends:
            if abs(int(dividend["amount_minor"]) - residual.amount_minor) <= tolerance(
                int(dividend["amount_minor"])
            ):
                found.append(
                    Candidate("dividend", (int(dividend["id"]),), residual.amount_minor)
                )
        total = sum(int(row["amount_minor"]) for row in dividends)
        if len(dividends) > 1 and abs(total - residual.amount_minor) <= tolerance(total):
            found.append(
                Candidate("dividends", tuple(int(row["id"]) for row in dividends))
            )
        return found

    def _auto_resolve(self, residual: Residual) -> None:
        candidates = self._candidates(residual)
        if len(candidates) != 1:
            return
        candidate = candidates[0]
        # The cash first appears in this interval, so it cannot predate its start.
        first_possible = _shift(residual.start, 1)
        note = f"Confirmed by brokerage cash snapshots {residual.start} to {residual.end}."
        events = [self._event(event_id) for event_id in candidate.event_ids]
        if candidate.kind == "transfer+fx":
            (inflow,) = events
            self._confirm(int(inflow["id"]), inflow["occurred_on"], None, note)
            self._record_inferred_conversion(inflow, residual, candidate.rate, note)
        else:
            observed = candidate.amount_minor if candidate.kind == "dividend" else None
            for event in events:
                self._confirm(
                    int(event["id"]), max(event["occurred_on"], first_possible), observed, note
                )
        self.actions[f"confirmed_{candidate.kind.replace('+', '_')}"] += 1

    def _event(self, event_id: int) -> sqlite3.Row:
        return self.con.execute(
            "SELECT * FROM canonical_events WHERE id=?", (event_id,)
        ).fetchone()

    def _confirm(self, event_id: int, on_date: str, amount: Optional[int], note: str) -> None:
        self.con.execute(
            """UPDATE canonical_events
                  SET status='confirmed',occurred_on=?,
                      amount_minor=COALESCE(?,amount_minor),
                      notes=trim(COALESCE(notes,'') || ' ' || ?)
                WHERE id=?""",
            (on_date, amount, note, event_id),
        )

    def _record_inferred_conversion(
        self, inflow: sqlite3.Row, residual: Residual, rate: Decimal, note: str
    ) -> None:
        account = self.con.execute(
            "SELECT * FROM accounts WHERE id=?", (residual.account_id,)
        ).fetchone()
        converted_on = inflow["occurred_on"]
        legs = {}
        for name, signed, currency, on_date in (
            ("sell", -int(inflow["amount_minor"]), inflow["currency"], converted_on),
            (
                "buy",
                residual.amount_minor,
                residual.currency,
                max(converted_on, _shift(residual.start, 1)),
            ),
        ):
            legs[name] = self.writer.new_cash_event(
                CashFacts(
                    account=account,
                    occurred_on=on_date,
                    amount_minor=signed,
                    currency=currency,
                    flow_type="investment",
                    category="fx",
                    component=f"cash_{currency.lower()}",
                ),
                status="confirmed",
                notes=f"Conversion inferred from the funding transfer. {note}",
            )
        self.writer.record_conversion(
            account_id=residual.account_id,
            converted_on=converted_on,
            sell_event_id=legs["sell"],
            buy_event_id=legs["buy"],
            sell_currency=inflow["currency"],
            buy_currency=residual.currency,
            actual_rate=rate,
            match_method="reconcile:cash-snapshots",
        )

    # ---------------------------------------------------------- open items

    def _age(self, day: str) -> int:
        return max(0, _days_between(day, self.today))

    @staticmethod
    def _money(amount_minor: int, currency: str) -> str:
        return f"{minor_to_major(int(amount_minor), currency)} {currency}"

    def _open_items(self) -> list[OpenItem]:
        items: list[OpenItem] = []
        expected_ids: set[int] = set()
        for row in self.con.execute(
            """SELECT event.* FROM canonical_events AS event
                 JOIN transfer_links AS link
                   ON event.id IN (link.outflow_event_id, link.inflow_event_id)
                WHERE event.status='needs_review'
                  AND NOT EXISTS (
                    SELECT 1 FROM event_evidence WHERE canonical_event_id=event.id
                  )
                ORDER BY event.occurred_on,event.id"""
        ):
            expected_ids.add(int(row["id"]))
            items.append(
                OpenItem(
                    "expected_leg",
                    self._age(row["occurred_on"]),
                    int(row["account_id"]),
                    int(row["id"]),
                    {
                        "date": row["occurred_on"],
                        "amount": self._money(row["amount_minor"], row["currency"]),
                    },
                )
            )
        for residual in self._residuals():
            items.append(
                OpenItem(
                    "unexplained_cash",
                    self._age(residual.end),
                    residual.account_id,
                    None,
                    {
                        "from": residual.start,
                        "to": residual.end,
                        "amount": self._money(residual.amount_minor, residual.currency),
                    },
                )
            )
        for row in self.con.execute(
            """SELECT event.*,dividend.ex_date,security.ticker
                 FROM canonical_events AS event
                 JOIN dividend_events AS dividend ON dividend.cash_event_id=event.id
                 JOIN securities AS security ON security.id=dividend.security_id
                WHERE event.status='needs_review'
                ORDER BY dividend.ex_date,event.id"""
        ):
            expected_ids.add(int(row["id"]))
            overdue = _shift(row["ex_date"], DIVIDEND_PAYMENT_DAYS)
            if overdue < self.today:
                items.append(
                    OpenItem(
                        "dividend_overdue",
                        self._age(overdue),
                        int(row["account_id"]),
                        int(row["id"]),
                        {
                            "ticker": row["ticker"],
                            "ex_date": row["ex_date"],
                            "expected": self._money(row["amount_minor"], row["currency"]),
                        },
                    )
                )
        for row in self.con.execute(
            """SELECT * FROM canonical_events
                WHERE status='needs_review' AND occurred_on<=?
                ORDER BY occurred_on,id""",
            (_shift(self.today, -STALE_REVIEW_DAYS),),
        ):
            if int(row["id"]) in expected_ids:
                continue
            items.append(
                OpenItem(
                    "stale_review",
                    self._age(row["occurred_on"]),
                    int(row["account_id"]),
                    int(row["id"]),
                    {"date": row["occurred_on"], "category": row["category"]},
                )
            )
        for row in self.con.execute(
            "SELECT * FROM fx_conversions ORDER BY converted_on,id"
        ):
            sell_currency = self.con.execute(
                "SELECT currency FROM canonical_events WHERE id=?", (row["sell_event_id"],)
            ).fetchone()[0]
            if row["market_rate"] is None:
                items.append(
                    OpenItem(
                        "missing_market_rate",
                        self._age(row["converted_on"]),
                        int(row["account_id"]),
                        None,
                        {"conversion": int(row["id"]), "date": row["converted_on"]},
                    )
                )
                continue
            cost = conversion_cost(
                Decimal(str(row["actual_rate"])),
                Decimal(str(row["market_rate"])),
                sell_currency=sell_currency,
                base_currency=row["base_currency"],
            )
            if not within_band(cost):
                items.append(
                    OpenItem(
                        "fx_rate_outside_band",
                        self._age(row["converted_on"]),
                        int(row["account_id"]),
                        None,
                        {
                            "conversion": int(row["id"]),
                            "date": row["converted_on"],
                            "actual_rate": row["actual_rate"],
                            "market_rate": row["market_rate"],
                            "cost_pct": float((cost * 100).quantize(Decimal("0.01"))),
                        },
                    )
                )
        for row in self.con.execute(
            """SELECT id,applied_at,summary FROM ledger_log
                WHERE kind='raw_sql' AND applied_at>=?
                ORDER BY id""",
            (_shift(self.today, -RAW_SQL_WINDOW_DAYS),),
        ):
            items.append(
                OpenItem(
                    "raw_sql_used",
                    self._age(str(row["applied_at"])[:10]),
                    None,
                    None,
                    {"log_id": int(row["id"]), "reason": row["summary"]},
                )
            )
        return items


__all__ = ["OpenItem", "Reconciler"]
