"""Brokerage position and quantity reconstruction.

This mixin keeps the snapshot/trade mechanics separate from account valuation while
leaving :class:`AccountingEngine` as the public entry point.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
import sqlite3
from typing import Optional

from findash_core.currencies import canonical_currency
from findash_core.errors import AccountingError
from findash_core.market_data import _decimal
from findash_core.models import ZERO
from findash_core.portfolio import Holding


class BrokerageHoldingsMixin:
    """Reconstruct brokerage holdings from position anchors and canonical trades."""

    def _latest_complete_account_snapshot_date(
        self, account_id: int, as_of: str
    ) -> Optional[str]:
        """Return the latest explicit account-wide holdings+cash checkpoint."""

        complete_date_queries: list[str] = []
        complete_date_parameters: list[object] = []
        if "account_snapshot_complete" in self._position_evidence_columns:
            complete_date_queries.append(
                """SELECT MAX(position.as_of)
                     FROM position_evidence AS evidence
                    JOIN positions AS position
                       ON position.id=evidence.canonical_position_id
                    WHERE position.account_id=? AND position.as_of<=?
                      AND evidence.account_snapshot_complete=1"""
            )
            complete_date_parameters.extend((account_id, as_of))
        if "account_snapshot_complete" in self._balance_evidence_columns:
            complete_date_queries.append(
                """SELECT MAX(balance.as_of)
                     FROM balance_evidence AS evidence
                    JOIN balances AS balance
                       ON balance.id=evidence.canonical_balance_id
                    WHERE balance.account_id=? AND balance.as_of<=?
                      AND evidence.account_snapshot_complete=1"""
            )
            complete_date_parameters.extend((account_id, as_of))
        if not complete_date_queries:
            return None
        complete = self.con.execute(
            "SELECT MAX(as_of) FROM ("
            + " UNION ALL ".join(
                f"SELECT ({query}) AS as_of" for query in complete_date_queries
            )
            + ")",
            complete_date_parameters,
        ).fetchone()
        complete_date = str(complete[0]) if complete and complete[0] else None
        opened_on = self._account_history_start(account_id)
        if complete_date and opened_on and complete_date < opened_on:
            return None
        return complete_date

    def _position_snapshots(self, account_id: int, as_of: str) -> list[dict]:
        """Latest reported anchor for every security, not one account-wide date."""

        evidence_state = "0 AS has_account_snapshot_evidence, 0 AS account_snapshot_complete"
        if "account_snapshot_complete" in self._position_evidence_columns:
            evidence_state = """EXISTS (
                 SELECT 1 FROM position_evidence AS evidence
                  WHERE evidence.canonical_position_id=p.id
               ) AS has_account_snapshot_evidence,
               EXISTS (
                 SELECT 1 FROM position_evidence AS evidence
                  WHERE evidence.canonical_position_id=p.id
                    AND evidence.account_snapshot_complete=1
               ) AS account_snapshot_complete"""
        rows = self.con.execute(
            f"""SELECT p.*, s.ticker, {evidence_state} FROM positions p
               JOIN securities s ON s.id=p.security_id
               JOIN (
                    SELECT security_id, MAX(as_of) AS as_of
                    FROM positions
                    WHERE account_id=? AND as_of<=?
                    GROUP BY security_id
               ) latest ON latest.security_id=p.security_id AND latest.as_of=p.as_of
               WHERE p.account_id=?
               ORDER BY p.security_id""",
            (account_id, as_of, account_id),
        ).fetchall()
        opened_on = self._account_history_start(account_id)
        if opened_on:
            rows = [row for row in rows if str(row["as_of"]) >= opened_on]
        return [dict(row) for row in rows]

    def _carried_avg_cost(
        self, account_id: int, snap: dict, trades: list[dict]
    ) -> Optional[tuple[Decimal, str]]:
        """An earlier reported average cost that still holds at a cost-less anchor.

        Average cost only moves with trades, so a basis reported for the same
        quantity, with no trade and no differing snapshot in between, is still
        the basis. Returns the per-unit cost and the date it was reported.
        """

        security_id = int(snap["security_id"])
        quantity = _decimal(snap["quantity"])
        prior = self.con.execute(
            """SELECT as_of,quantity,avg_cost_minor,currency FROM positions
                WHERE account_id=? AND security_id=? AND as_of<?
                  AND avg_cost_minor IS NOT NULL
                ORDER BY as_of DESC LIMIT 1""",
            (account_id, security_id, snap["as_of"]),
        ).fetchone()
        if prior is None:
            return None
        prior_date = str(prior["as_of"])
        opened_on = self._account_history_start(account_id)
        if (
            (opened_on and prior_date < opened_on)
            or _decimal(prior["quantity"]) != quantity
            or canonical_currency(prior["currency"])
            != canonical_currency(snap["currency"])
        ):
            return None
        quantity_changed_between = self.con.execute(
            """SELECT 1 FROM positions
                WHERE account_id=? AND security_id=? AND as_of>? AND as_of<?
                  AND quantity<>?
                LIMIT 1""",
            (account_id, security_id, prior_date, snap["as_of"], float(quantity)),
        ).fetchone()
        traded_between = any(
            int(trade["security_id"]) == security_id
            and prior_date < trade["date"] <= snap["as_of"]
            for trade in trades
        )
        if quantity_changed_between or traded_between:
            return None
        return _decimal(prior["avg_cost_minor"]), prior_date

    def _holdings(self, account: sqlite3.Row, as_of: str) -> list[Holding]:
        snapshots = self._position_snapshots(account["id"], as_of)
        complete_account_date = self._latest_complete_account_snapshot_date(
            int(account["id"]), as_of
        )
        trades = self.trades(account["id"], as_of)
        holdings: dict[int, Holding] = {}
        for snap in snapshots:
            # A newer reviewed account-wide snapshot says every omitted security
            # was zero at that checkpoint. An older per-security row must not
            # survive that explicit closure; any later trade will reopen it from
            # a zero basis through the normal trade loop below.
            if complete_account_date and snap["as_of"] < complete_account_date:
                continue
            anchor_date = snap["as_of"]
            quantity = _decimal(snap["quantity"])
            currency = canonical_currency(snap["currency"])
            cost_date = anchor_date
            if quantity == ZERO:
                # An explicit zero closes a complete snapshot segment. Its basis is
                # known zero even when a per-share average is naturally absent.
                native_cost: Optional[Decimal] = ZERO
            elif snap["avg_cost_minor"] is not None:
                native_cost = quantity * _decimal(snap["avg_cost_minor"])
            else:
                carried = self._carried_avg_cost(int(account["id"]), snap, trades)
                if carried is None:
                    native_cost = None
                else:
                    native_cost = quantity * carried[0]
                    # Convert at the date the basis was reported, so carrying it
                    # forward does not restate the reporting-currency cost.
                    cost_date = carried[1]
            reporting_cost = (
                self._reporting_value(native_cost, currency, cost_date)
                if native_cost is not None
                else None
            )
            warnings = []
            if (
                snap.get("has_account_snapshot_evidence")
                and not snap.get("account_snapshot_complete")
            ):
                warnings.append("account snapshot is not marked complete")
            holdings[snap["security_id"]] = Holding(
                security_id=snap["security_id"],
                ticker=snap["ticker"],
                currency=currency,
                quantity=quantity,
                cost_minor=native_cost,
                reporting_cost_minor=reporting_cost,
                anchor_date=anchor_date,
                observed_through=anchor_date,
                warnings=warnings,
                snapshot_price_minor=(
                    _decimal(snap["market_price_minor"])
                    if snap["market_price_minor"] is not None
                    else None
                ),
                snapshot_value_minor=(
                    _decimal(snap["market_value_minor"])
                    if snap["market_value_minor"] is not None
                    else None
                ),
            )

        for trade in trades:
            security_id = int(trade["security_id"])
            currency = canonical_currency(trade["currency"])
            security_currency = canonical_currency(trade["security_currency"])
            if currency != security_currency:
                raise AccountingError(
                    "trade currency does not match the security currency"
                )
            holding = holdings.setdefault(
                security_id,
                Holding(
                    security_id=security_id,
                    ticker=trade["ticker"],
                    currency=currency,
                    cost_minor=ZERO,
                    reporting_cost_minor=ZERO,
                    anchor_date=complete_account_date,
                    observed_through=complete_account_date,
                    warnings=(
                        []
                        if complete_account_date
                        else [
                            "no opening position snapshot; trade history may be partial"
                        ]
                    ),
                ),
            )
            if holding.currency != currency:
                raise AccountingError(
                    "trade currency does not match the existing holding currency"
                )
            if holding.anchor_date and trade["date"] <= holding.anchor_date:
                continue
            shares = _decimal(trade["shares"])
            price = _decimal(trade["price_minor"])
            fees = _decimal(trade["fees_minor"] or 0)
            holding.apply_trade(
                side=trade["side"],
                shares=shares,
                price_minor=price,
                fees_minor=fees,
                reporting_trade_cost_minor=self._reporting_value(
                    shares * price, currency, trade["date"]
                ),
                reporting_fees_minor=self._reporting_value(
                    fees, currency, trade["date"]
                ),
                trade_date=trade["date"],
            )
        return [holding for holding in holdings.values() if holding.quantity != ZERO]

    def security_quantities_at(
        self,
        as_of: str,
        *,
        include_closed: bool = False,
        account_id: Optional[int] = None,
    ) -> dict[int, Decimal]:
        """Return held quantities without requiring prices or FX rates.

        Dividend entitlement depends on shares held at an event date, not on the
        market value of those shares. Reusing ``state_at`` for that question would
        incorrectly make quantity history disappear whenever a historical quote or
        FX rate is missing.

        Position snapshots anchor one security at a time. A reviewed complete
        account snapshot also anchors omitted securities at zero; later canonical
        or still-unmigrated legacy trades advance the quantity exactly once through
        :class:`EventReader`.
        """

        date.fromisoformat(as_of)
        totals: dict[int, Decimal] = {}
        for account in self.accounts(as_of, include_closed=include_closed):
            if account["kind"] != "brokerage" or (
                account_id is not None and int(account["id"]) != account_id
            ):
                continue
            snapshots = self._position_snapshots(int(account["id"]), as_of)
            complete_date = self._latest_complete_account_snapshot_date(
                int(account["id"]), as_of
            )
            quantities: dict[int, Decimal] = {}
            anchors: dict[int, Optional[str]] = {}
            for row in snapshots:
                security_id = int(row["security_id"])
                if complete_date and str(row["as_of"]) < complete_date:
                    # Preserve the zero anchor so only trades strictly after the
                    # complete checkpoint can reopen this omitted security.
                    quantities[security_id] = ZERO
                    anchors[security_id] = complete_date
                else:
                    quantities[security_id] = _decimal(row["quantity"])
                    anchors[security_id] = str(row["as_of"])
            for trade in self.trades(int(account["id"]), as_of):
                security_id = int(trade["security_id"])
                anchor = anchors.get(security_id, complete_date)
                if anchor and str(trade["date"]) <= anchor:
                    continue
                quantity = _decimal(trade["shares"])
                if trade["side"] == "sell":
                    quantity = -quantity
                quantities[security_id] = (
                    quantities.get(security_id, ZERO) + quantity
                )
            for security_id, quantity in quantities.items():
                totals[security_id] = totals.get(security_id, ZERO) + quantity
        return {
            security_id: quantity
            for security_id, quantity in totals.items()
            if quantity != ZERO
        }
