"""Deterministic account-state reconstruction.

Raw source rows remain auditable in their original tables.  This module provides the
single calculated view consumed by the dashboard: every asset, cash component, and
liability is valued exactly once for a requested date.

Snapshots are anchors, not an alternative accounting system.  Events after the latest
anchor advance state; a later snapshot starts a new segment.  Brokerage position
snapshots follow the same rule, so a partial connector trade history never replaces a
complete reported portfolio.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from decimal import Decimal
import sqlite3
from typing import Iterable, Optional

from findash_core.brokerage_holdings import BrokerageHoldingsMixin
from findash_core.categories import flow_type_for
from findash_core.currencies import canonical_currency
from findash_core.errors import AccountingError, MissingRateError
from findash_core.events import EventReader
from findash_core.market_data import FxBook, PriceBook, _decimal
from findash_core.models import (
    AccountState,
    Coverage,
    ExternalFlow,
    PositionState,
    ValuationRow,
    ZERO,
)


def _date_days(old: Optional[str], new: str) -> Optional[int]:
    if not old:
        return None
    return (date.fromisoformat(new) - date.fromisoformat(old)).days


def _table_exists(con: sqlite3.Connection, table: str) -> bool:
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _columns(con: sqlite3.Connection, table: str) -> frozenset[str]:
    return frozenset(row[1] for row in con.execute(f"PRAGMA table_info({table})"))


class AccountingEngine(BrokerageHoldingsMixin, EventReader):
    """Build normalized account state from snapshots and canonical events."""

    def __init__(self, con: sqlite3.Connection, reporting_currency: str = "ILS"):
        self.con = con
        self.con.row_factory = sqlite3.Row
        self.reporting_currency = canonical_currency(reporting_currency)
        self.fx = FxBook(con, self.reporting_currency)
        self.prices = PriceBook(con)
        self._transaction_columns = _columns(con, "transactions")
        self._trade_columns = _columns(con, "trades")
        self._has_canonical = _table_exists(con, "canonical_events")
        self._has_event_evidence = _table_exists(con, "event_evidence")
        self._has_trade_cash_links = _table_exists(con, "trade_cash_links")
        self._balance_evidence_columns = (
            _columns(con, "balance_evidence")
            if _table_exists(con, "balance_evidence")
            else frozenset()
        )
        self._position_evidence_columns = (
            _columns(con, "position_evidence")
            if _table_exists(con, "position_evidence")
            else frozenset()
        )
        self._canonical_columns = (
            _columns(con, "canonical_events") if self._has_canonical else frozenset()
        )

    def _account_history_start(self, account_id: int) -> Optional[str]:
        """Return the earliest date on which evidence may affect an account."""

        row = self.con.execute(
            "SELECT opened_on FROM accounts WHERE id=?", (account_id,)
        ).fetchone()
        return str(row[0]) if row and row[0] else None

    @staticmethod
    def account_active_on(account: sqlite3.Row, as_of: str) -> bool:
        opened = account["opened_on"]
        closed = account["closed_on"]
        return (not opened or opened <= as_of) and (not closed or as_of < closed)

    def accounts(self, as_of: str, *, include_closed: bool = False) -> list[sqlite3.Row]:
        return [
            row
            for row in self.con.execute("SELECT * FROM accounts ORDER BY id")
            if (not row["opened_on"] or row["opened_on"] <= as_of)
            and (include_closed or self.account_active_on(row, as_of))
        ]

    def _balance_snapshots(self, account_id: int, as_of: str) -> list[dict]:
        """Latest row per component/currency, defensively collapsing legacy NULL duplicates."""

        evidence_state = "0 AS has_account_snapshot_evidence, 0 AS account_snapshot_complete"
        if "account_snapshot_complete" in self._balance_evidence_columns:
            evidence_state = """EXISTS (
                 SELECT 1 FROM balance_evidence AS evidence
                  WHERE evidence.canonical_balance_id=balance.id
               ) AS has_account_snapshot_evidence,
               EXISTS (
                 SELECT 1 FROM balance_evidence AS evidence
                  WHERE evidence.canonical_balance_id=balance.id
                    AND evidence.account_snapshot_complete=1
               ) AS account_snapshot_complete"""
        rows = self.con.execute(
            f"""SELECT balance.*, {evidence_state}
                  FROM balances AS balance
                 WHERE balance.account_id=? AND balance.as_of<=?
                 ORDER BY balance.as_of DESC, balance.id DESC""",
            (account_id, as_of),
        ).fetchall()
        opened_on = self._account_history_start(account_id)
        if opened_on:
            rows = [row for row in rows if str(row["as_of"]) >= opened_on]
        selected: dict[tuple[str, str], dict] = {}
        for row in rows:
            key = (row["component"] or "", canonical_currency(row["currency"]))
            selected.setdefault(key, dict(row))
        return list(selected.values())

    def _reporting_value(self, amount_minor: Decimal, currency: str, on_date: str) -> Decimal:
        return self.fx.convert_minor(
            amount_minor, canonical_currency(currency), self.reporting_currency, on_date
        )

    def _reporting_value_with_fx_date(
        self, amount_minor: Decimal, currency: str, on_date: str
    ) -> tuple[Decimal, str]:
        value, point = self.fx.convert_minor_with_point(
            amount_minor,
            canonical_currency(currency),
            self.reporting_currency,
            on_date,
        )
        return value, point.date

    def _bucket_for(self, account: sqlite3.Row, as_of: str) -> str:
        if account["kind"] == "credit_card":
            return "Liabilities"
        if account["kind"] == "pension":
            return "Locked"
        if account["kind"] == "study_fund":
            if not account["liquidity_date"] or account["liquidity_date"] > as_of:
                return "Locked"
            return "Investments"
        if (
            account["kind"] == "savings"
            and account["liquidity_date"]
            and account["liquidity_date"] > as_of
        ):
            return "Locked"
        if account["kind"] == "brokerage":
            return "Investments"
        if account["kind"] == "other":
            return "Other"
        return "Cash"

    def _cash_rows(
        self,
        account: sqlite3.Row,
        as_of: str,
        *,
        brokerage: bool = False,
        include_trade_cash: bool = False,
    ) -> list[ValuationRow]:
        snapshots = self._balance_snapshots(account["id"], as_of)
        if brokerage:
            snapshots = [
                row
                for row in snapshots
                if row["component"] and str(row["component"]).startswith("cash_")
            ]
        elif account["kind"] in {"pension", "study_fund"}:
            return self._locked_account_rows(account, snapshots, as_of)

        transactions = self.transactions(account["id"], as_of)
        trades = self.trades(account["id"], as_of) if include_trade_cash else []
        complete_account_date = self._latest_complete_account_snapshot_date(
            int(account["id"]), as_of
        )
        currencies = {canonical_currency(row["currency"]) for row in snapshots}
        currencies.update(canonical_currency(row["currency"]) for row in transactions)
        currencies.update(canonical_currency(row["currency"]) for row in trades)
        if not currencies and account["currency"]:
            currencies.add(canonical_currency(account["currency"]))

        result: list[ValuationRow] = []
        for currency in sorted(currencies):
            candidates = [
                row for row in snapshots if canonical_currency(row["currency"]) == currency
            ]
            if brokerage:
                candidates = [
                    row
                    for row in candidates
                    if (row["component"] or "").lower() == f"cash_{currency.lower()}"
                ]
            else:
                plain = [row for row in candidates if not row["component"]]
                candidates = plain or candidates
            anchor = (
                max(candidates, key=lambda row: (row["as_of"], row["id"]))
                if candidates
                else None
            )
            # A reviewed complete account snapshot also says that an omitted cash
            # currency was zero at that checkpoint. This matters when, for example,
            # a USD deposit first appears after an explicitly empty ILS brokerage
            # snapshot. A newer explicit currency balance still wins.
            synthetic_complete_anchor = bool(
                complete_account_date
                and (not anchor or anchor["as_of"] < complete_account_date)
            )
            if synthetic_complete_anchor:
                anchor = None
                anchor_date = complete_account_date
                amount = ZERO
            else:
                anchor_date = anchor["as_of"] if anchor else None
                amount = _decimal(anchor["amount_minor"] if anchor else 0)
            unanchored_credit_card = (
                account["kind"] == "credit_card" and anchor_date is None
            )
            eligible_trades = [
                trade
                for trade in trades
                if canonical_currency(trade["currency"]) == currency
                and (not anchor_date or trade["date"] > anchor_date)
            ]
            suppressed_legacy_cash = False

            for tx in transactions:
                if canonical_currency(tx["currency"]) != currency:
                    continue
                # Card exports contain charges but commonly omit the repayment
                # leg.  Without a balance anchor, accumulating those charges is
                # not a point-in-time liability; it grows forever and then
                # creates a false gain when the first real balance resets it.
                if unanchored_credit_card:
                    continue
                if anchor_date and tx["date"] <= anchor_date:
                    continue
                tx_flow = flow_type_for(tx["category"], tx.get("flow_type"), tx["amount_minor"])
                linked_trade_min = tx.get("linked_trade_min_date")
                linked_trade_max = tx.get("linked_trade_max_date")
                if anchor_date and linked_trade_max:
                    if linked_trade_max <= anchor_date:
                        # The anchor already incorporates the economics of every
                        # trade this later posted cash event replaces.
                        continue
                    if linked_trade_min and linked_trade_min <= anchor_date:
                        raise AccountingError(
                            "linked trade cash spans a balance anchor; reconcile "
                            "the settlement grouping before valuation"
                        )
                # Before normalized events existed, an investment transaction often
                # duplicated raw trade history with no durable relationship between
                # them. Keep the old trade-derived treatment only when overlapping
                # eligible trades make that ambiguity real; an isolated explicit cash
                # observation must not disappear. Canonical cash is always counted and
                # trade_cash_links says exactly which canonical trades it replaces.
                if (
                    include_trade_cash
                    and tx_flow == "investment"
                    and not tx.get("is_canonical")
                    and eligible_trades
                ):
                    suppressed_legacy_cash = True
                    continue
                amount += _decimal(tx["amount_minor"])

            if include_trade_cash:
                for trade in eligible_trades:
                    if trade.get("cash_linked"):
                        continue
                    gross = _decimal(trade["shares"]) * _decimal(trade["price_minor"])
                    fees = _decimal(trade["fees_minor"] or 0)
                    amount += -gross - fees if trade["side"] == "buy" else gross - fees

            if amount == ZERO and not anchor and not transactions and not trades:
                continue
            method = "snapshot+events" if anchor_date else "events-partial"
            warning_items = [] if anchor_date else ["no opening balance snapshot"]
            if unanchored_credit_card:
                warning_items.append(
                    "credit-card transaction history lacks payment coverage"
                )
            if (
                brokerage
                and anchor
                and anchor.get("has_account_snapshot_evidence")
                and not anchor.get("account_snapshot_complete")
            ):
                warning_items.append("account snapshot is not marked complete")
            if suppressed_legacy_cash:
                warning_items.append(
                    "legacy investment cash overlaps unlinked trade history"
                )
            warnings = tuple(warning_items)
            observed = max(
                [anchor_date or ""]
                + [tx["date"] for tx in transactions if canonical_currency(tx["currency"]) == currency]
                + [tr["date"] for tr in trades if canonical_currency(tr["currency"]) == currency]
            ) or None
            reporting, fx_date = self._reporting_value_with_fx_date(
                amount, currency, as_of
            )
            limiting_observation = min(observed, fx_date) if observed else fx_date
            coverage = Coverage(
                method=method,
                anchor_date=anchor_date,
                observed_through=limiting_observation,
                complete_from=anchor_date,
                stale_days=_date_days(limiting_observation, as_of),
                warnings=warnings,
            )
            result.append(
                ValuationRow(
                    as_of=as_of,
                    account_id=account["id"],
                    account_name=account["name"],
                    account_kind=account["kind"],
                    institution=account["institution"],
                    bucket=self._bucket_for(account, as_of),
                    item_type="cash" if brokerage else "balance",
                    item_key=(
                        f"cash_{currency.lower()}" if brokerage else "balance"
                    ),
                    label=f"{account['name']} · {currency}" if brokerage else account["name"],
                    currency=currency,
                    native_value_minor=amount,
                    reporting_currency=self.reporting_currency,
                    reporting_value_minor=reporting,
                    coverage=coverage,
                )
            )
        return result

    def _locked_account_rows(
        self, account: sqlite3.Row, snapshots: list[dict], as_of: str
    ) -> list[ValuationRow]:
        """Carry known locked-account flows between market-value snapshots.

        Contributions are exact cash observations even though investment return
        between statements is unknown.  Keeping them as a separate valuation row
        makes that known minimum visible without pretending it is a fresh market
        valuation; the next statement reanchors the account and resets the row.
        """

        snapshot_rows = self._snapshot_only_rows(account, snapshots, as_of)
        anchor_dates = [
            row.coverage.anchor_date
            for row in snapshot_rows
            if row.coverage.anchor_date is not None
        ]
        anchor_date = max(anchor_dates, default=None)
        all_transactions = self.transactions(int(account["id"]), as_of)
        transactions = [
            row
            for row in all_transactions
            if anchor_date is None or str(row["date"]) > anchor_date
        ]
        if not snapshot_rows and not all_transactions:
            return []
        if snapshot_rows and not all_transactions:
            return snapshot_rows

        amounts: dict[str, Decimal] = defaultdict(lambda: ZERO)
        observed_by_currency: dict[str, list[str]] = defaultdict(list)
        for transaction in transactions:
            currency = canonical_currency(transaction["currency"])
            amounts[currency] += _decimal(transaction["amount_minor"])
            observed_by_currency[currency].append(str(transaction["date"]))

        currencies = {row.currency for row in snapshot_rows} | set(amounts)
        if not currencies and account["currency"]:
            currencies.add(canonical_currency(account["currency"]))

        flow_rows: list[ValuationRow] = []
        for currency in sorted(currencies):
            amount = amounts[currency]
            reporting, fx_date = self._reporting_value_with_fx_date(
                amount, currency, as_of
            )
            observed = max(
                [anchor_date or "", *observed_by_currency[currency]]
            ) or None
            limiting_observation = min(observed, fx_date) if observed else fx_date
            if anchor_date:
                method = "snapshot+events"
                warnings = (
                    "market return after latest locked-account snapshot is unobserved",
                )
            else:
                method = "events-partial"
                warnings = (
                    "no opening balance snapshot",
                    "locked-account market return is unobserved",
                )
            flow_rows.append(
                ValuationRow(
                    as_of=as_of,
                    account_id=account["id"],
                    account_name=account["name"],
                    account_kind=account["kind"],
                    institution=account["institution"],
                    bucket=self._bucket_for(account, as_of),
                    item_type="balance",
                    item_key=f"known_events_{currency.lower()}",
                    label=f"{account['name']} · known flows since statement",
                    currency=currency,
                    native_value_minor=amount,
                    reporting_currency=self.reporting_currency,
                    reporting_value_minor=reporting,
                    coverage=Coverage(
                        method=method,
                        anchor_date=anchor_date,
                        observed_through=limiting_observation,
                        complete_from=anchor_date,
                        stale_days=_date_days(limiting_observation, as_of),
                        warnings=warnings,
                    ),
                )
            )
        return snapshot_rows + flow_rows

    def _snapshot_only_rows(
        self, account: sqlite3.Row, snapshots: list[dict], as_of: str
    ) -> list[ValuationRow]:
        component_rows = [row for row in snapshots if row["component"]]
        plain_rows = [row for row in snapshots if not row["component"]]
        required_components = {
            "pension": {"tagmul_employee", "tagmul_employer", "pitsuyim"},
            "study_fund": {"tagmul_employee", "tagmul_employer"},
        }.get(account["kind"])
        incomplete_warning: tuple[str, ...] = ()
        if required_components:
            placeholders = ",".join("?" for _ in required_components)
            rows = self.con.execute(
                f"""SELECT * FROM balances
                       WHERE account_id=? AND as_of<=?
                         AND component IN ({placeholders})
                       ORDER BY as_of DESC, id DESC""",
                (account["id"], as_of, *sorted(required_components)),
            ).fetchall()
            opened_on = self._account_history_start(int(account["id"]))
            if opened_on:
                rows = [row for row in rows if str(row["as_of"]) >= opened_on]
            by_date: dict[str, dict[str, dict]] = {}
            for row in rows:
                by_date.setdefault(str(row["as_of"]), {})[str(row["component"])] = dict(row)
            complete_rows: list[dict] = []
            for snapshot_date in sorted(by_date, reverse=True):
                components = by_date[snapshot_date]
                if required_components.issubset(components):
                    complete_rows = [components[name] for name in sorted(required_components)]
                    break

            latest_plain_date = max(
                (str(row["as_of"]) for row in plain_rows), default=None
            )
            complete_date = (
                str(complete_rows[0]["as_of"]) if complete_rows else None
            )
            if complete_date and (
                latest_plain_date is None or complete_date >= latest_plain_date
            ):
                chosen = complete_rows
            elif latest_plain_date:
                chosen = [
                    row for row in plain_rows if str(row["as_of"]) == latest_plain_date
                ]
            else:
                # No full total exists. Preserve the visible component evidence,
                # but mark that it is not a complete account valuation.
                chosen = component_rows
                incomplete_warning = ("incomplete component snapshot",)
        else:
            # Outside the modeled locked-account component sets, detailed rows are
            # the best available evidence and a plain total is reconciliation-only.
            chosen = component_rows or plain_rows
        result: list[ValuationRow] = []
        for row in chosen:
            currency = canonical_currency(row["currency"])
            amount = _decimal(row["amount_minor"])
            reporting, fx_date = self._reporting_value_with_fx_date(
                amount, currency, as_of
            )
            limiting_observation = min(str(row["as_of"]), fx_date)
            coverage = Coverage(
                method="snapshot-only",
                anchor_date=row["as_of"],
                observed_through=limiting_observation,
                stale_days=_date_days(limiting_observation, as_of),
                warnings=incomplete_warning,
            )
            result.append(
                ValuationRow(
                    as_of=as_of,
                    account_id=account["id"],
                    account_name=account["name"],
                    account_kind=account["kind"],
                    institution=account["institution"],
                    bucket=self._bucket_for(account, as_of),
                    item_type="balance",
                    item_key=row["component"] or "balance",
                    label=account["name"],
                    currency=currency,
                    native_value_minor=amount,
                    reporting_currency=self.reporting_currency,
                    reporting_value_minor=reporting,
                    coverage=coverage,
                )
            )
        return result

    def _aggregate_brokerage_row(
        self, account: sqlite3.Row, as_of: str, position_snapshots: list[dict]
    ) -> Optional[ValuationRow]:
        """Keep a coarse total until a reviewed complete account snapshot replaces it.

        A plain brokerage balance is the reported value of the whole account, not a
        cash component. Partial holdings/cash observations cannot decompose that total
        because the omitted opening state is unknown. Only source evidence explicitly
        marked as a complete account snapshot may replace it.
        """

        snapshots = self._balance_snapshots(account["id"], as_of)
        plain = [row for row in snapshots if not row["component"]]
        if not plain:
            return None
        aggregate = max(plain, key=lambda row: (row["as_of"], row["id"]))
        complete_date = self._latest_complete_account_snapshot_date(
            int(account["id"]), as_of
        )
        if complete_date and aggregate["as_of"] <= complete_date:
            return None

        detailed_dates = [row["as_of"] for row in position_snapshots]
        detailed_dates.extend(
            row["as_of"]
            for row in snapshots
            if row["component"] and str(row["component"]).startswith("cash_")
        )

        currency = canonical_currency(aggregate["currency"])
        amount = _decimal(aggregate["amount_minor"])
        applied_flow_dates: list[str] = []
        conversion_dependency_dates: list[str] = []
        later_trades = [
            trade
            for trade in self.trades(account["id"], as_of)
            if trade["date"] > aggregate["as_of"]
        ]
        for tx in self.transactions(account["id"], as_of):
            if tx["date"] <= aggregate["as_of"]:
                continue
            flow_type = flow_type_for(
                tx["category"], tx.get("flow_type"), tx["amount_minor"]
            )
            # Explicitly linked investment cash is the other side of a security
            # trade. Applying it to an indivisible whole-account total would count
            # only half of an internal reallocation. An unlinked canonical cash event
            # is a separate economic movement and must remain counted. Legacy rows
            # retain their conservative overlap treatment.
            if flow_type == "investment":
                if tx.get("linked_trade_min_date"):
                    continue
                if not tx.get("is_canonical") and any(
                    canonical_currency(trade["currency"])
                    == canonical_currency(tx["currency"])
                    for trade in later_trades
                ):
                    continue
            converted, point = self.fx.convert_minor_with_point(
                _decimal(tx["amount_minor"]),
                canonical_currency(tx["currency"]),
                currency,
                tx["date"],
            )
            amount += converted
            conversion_dependency_dates.append(point.date)
            applied_flow_dates.append(str(tx["date"]))

        # Gross trade cash and the marked security offset inside a whole-account
        # total. Fees do not: they are a known reduction even before holdings/cash
        # can be decomposed into detailed anchors.
        for trade in later_trades:
            fees = _decimal(trade["fees_minor"] or 0)
            if fees == ZERO:
                continue
            converted_fee, point = self.fx.convert_minor_with_point(
                fees,
                canonical_currency(trade["currency"]),
                currency,
                trade["date"],
            )
            amount -= converted_fee
            conversion_dependency_dates.append(point.date)
            applied_flow_dates.append(str(trade["date"]))

        warnings = [
            "aggregate brokerage snapshot carried forward; market value remains "
            "anchored until a complete account snapshot is recorded"
        ]
        if detailed_dates and aggregate["as_of"] <= max(detailed_dates):
            warnings.append(
                "newer detailed brokerage observations are partial and do not "
                "replace the whole-account snapshot"
            )
        method = "aggregate-snapshot+flows" if applied_flow_dates else "aggregate-snapshot"
        reporting, fx_date = self._reporting_value_with_fx_date(
            amount, currency, as_of
        )
        limiting_observation = min(
            [str(aggregate["as_of"]), fx_date, *conversion_dependency_dates]
        )
        coverage = Coverage(
            method=method,
            anchor_date=aggregate["as_of"],
            observed_through=limiting_observation,
            stale_days=_date_days(limiting_observation, as_of),
            warnings=tuple(warnings),
        )
        return ValuationRow(
            as_of=as_of,
            account_id=account["id"],
            account_name=account["name"],
            account_kind=account["kind"],
            institution=account["institution"],
            bucket="Investments",
            item_type="balance",
            item_key="aggregate",
            label=account["name"],
            currency=currency,
            native_value_minor=amount,
            reporting_currency=self.reporting_currency,
            reporting_value_minor=reporting,
            coverage=coverage,
        )

    def _brokerage_rows(
        self, account: sqlite3.Row, as_of: str
    ) -> tuple[list[ValuationRow], list[PositionState]]:
        snapshots = self._position_snapshots(account["id"], as_of)
        aggregate = self._aggregate_brokerage_row(account, as_of, snapshots)
        if aggregate is not None:
            return [aggregate], []

        holdings = self._holdings(account, as_of)
        trades = self.trades(account["id"], as_of)
        snapshot_by_security = {row["security_id"]: row for row in snapshots}
        rows: list[ValuationRow] = []
        states: list[PositionState] = []

        for holding in holdings:
            point = self.prices.latest(holding.security_id, as_of)
            if point and canonical_currency(point.currency) != holding.currency:
                raise AccountingError(
                    "market-price currency does not match the held security currency"
                )
            price_minor: Optional[Decimal] = point.close_minor if point else None
            price_date: Optional[str] = point.date if point else None
            method = "market-price"

            # A statement/connector position can carry a market observation that
            # is newer than the cached Yahoo close. Treat both as dated price
            # candidates and use the freshest one. "Any cached row wins" would,
            # for example, value a 2025 statement with a lone 2024 close.
            snap = snapshot_by_security.get(holding.security_id)
            snapshot_price: Optional[Decimal] = None
            snapshot_method: Optional[str] = None
            if snap and snap["market_price_minor"] is not None:
                snapshot_price = _decimal(snap["market_price_minor"])
                snapshot_method = "snapshot-price"
            elif (
                snap
                and snap["market_value_minor"] is not None
                and _decimal(snap["quantity"]) != ZERO
            ):
                snapshot_price = (
                    _decimal(snap["market_value_minor"])
                    / _decimal(snap["quantity"])
                )
                snapshot_method = "snapshot-value"
            if snapshot_price is not None and snapshot_price < ZERO:
                raise AccountingError(
                    "position snapshot implies a negative per-unit market price"
                )
            if snapshot_price is not None and (
                price_date is None or snap["as_of"] > price_date
            ):
                price_minor = snapshot_price
                price_date = snap["as_of"]
                method = snapshot_method or "snapshot-price"
            warnings = list(holding.warnings)
            if price_minor is None:
                raise AccountingError(
                    "cannot value a held security: no price or usable position snapshot"
                )
            else:
                market_minor = holding.quantity * price_minor
                reporting_market, fx_date = self._reporting_value_with_fx_date(
                    market_minor, holding.currency, as_of
                )
            # Quote and FX freshness are both limiting dependencies for a marked
            # position. A current quote must not make an old conversion look current.
            observed_candidates = [
                value
                for value in (price_date or holding.observed_through, fx_date)
                if value
            ]
            observed = min(observed_candidates) if observed_candidates else None
            coverage = Coverage(
                method=method,
                anchor_date=holding.anchor_date,
                observed_through=observed,
                complete_from=holding.anchor_date,
                stale_days=_date_days(observed, as_of),
                warnings=tuple(warnings),
            )
            avg = (
                holding.cost_minor / holding.quantity
                if holding.cost_minor is not None and holding.quantity != ZERO
                else None
            )
            states.append(
                PositionState(
                    account_id=account["id"],
                    security_id=holding.security_id,
                    ticker=holding.ticker,
                    currency=holding.currency,
                    quantity=holding.quantity,
                    cost_basis_minor=holding.cost_minor,
                    reporting_cost_basis_minor=holding.reporting_cost_minor,
                    average_cost_minor=avg,
                    anchor_date=holding.anchor_date,
                    observed_through=observed,
                    coverage=coverage,
                )
            )
            rows.append(
                ValuationRow(
                    as_of=as_of,
                    account_id=account["id"],
                    account_name=account["name"],
                    account_kind=account["kind"],
                    institution=account["institution"],
                    bucket="Investments",
                    item_type="security",
                    item_key=str(holding.security_id),
                    label=holding.ticker,
                    currency=holding.currency,
                    native_value_minor=market_minor,
                    reporting_currency=self.reporting_currency,
                    reporting_value_minor=reporting_market,
                    coverage=coverage,
                    quantity=holding.quantity,
                    unit_price_minor=price_minor,
                    cost_basis_minor=holding.cost_minor,
                    reporting_cost_basis_minor=holding.reporting_cost_minor,
                    security_id=holding.security_id,
                    ticker=holding.ticker,
                )
            )

        cash_rows = self._cash_rows(
            account, as_of, brokerage=True, include_trade_cash=True
        )
        has_positions_or_trades = bool(holdings or trades or snapshots)
        if not has_positions_or_trades and not cash_rows:
            # A brokerage represented only by one total balance snapshot remains a
            # legitimate, if coarse, investment valuation.
            cash_rows = self._cash_rows(account, as_of)
        rows.extend(cash_rows)
        return rows, states

    def _external_flows(self, account: sqlite3.Row, as_of: str) -> list[ExternalFlow]:
        result: list[ExternalFlow] = []
        for tx in self.transactions(account["id"], as_of):
            kind = flow_type_for(tx["category"], tx.get("flow_type"), tx["amount_minor"])
            source_doc_id = tx.get("source_doc_id")
            internal_fx_document = bool(
                source_doc_id
                and self.con.execute(
                    """SELECT 1 FROM documents
                        WHERE id=? AND drive_path LIKE 'fx-conversions/%'""",
                    (source_doc_id,),
                ).fetchone()
            )
            if kind != "transfer" or internal_fx_document:
                continue
            currency = canonical_currency(tx["currency"])
            reporting = self._reporting_value(
                _decimal(tx["amount_minor"]), currency, tx["date"]
            )
            result.append(
                ExternalFlow(
                    account_id=account["id"],
                    date=tx["date"],
                    native_amount_minor=int(tx["amount_minor"]),
                    currency=currency,
                    reporting_amount_minor=reporting,
                    transaction_id=tx["id"],
                    source_doc_id=source_doc_id,
                )
            )
        return result

    def external_flows(
        self, as_of: str, *, include_closed: bool = False
    ) -> list[ExternalFlow]:
        rows: list[ExternalFlow] = []
        for account in self.accounts(as_of, include_closed=include_closed):
            if account["kind"] == "brokerage":
                rows.extend(self._external_flows(account, as_of))
        rows.sort(key=lambda row: (row.date, row.transaction_id or 0))
        return rows

    def state_at(self, as_of: str) -> AccountState:
        # Validate early; it also prevents SQL/string comparisons from accepting a
        # malformed date that later crashes only one chart.
        date.fromisoformat(as_of)
        state = AccountState(as_of=as_of, reporting_currency=self.reporting_currency)
        for account in self.accounts(as_of):
            try:
                if account["kind"] == "brokerage":
                    rows, positions = self._brokerage_rows(account, as_of)
                    state.rows.extend(rows)
                    state.positions.extend(positions)
                    state.external_flows.extend(self._external_flows(account, as_of))
                else:
                    state.rows.extend(self._cash_rows(account, as_of))
            except MissingRateError as exc:
                raise MissingRateError(f"account valuation failed: {exc}") from exc

        # The same row set drives headline, Overview, and history.  Duplicate item
        # keys within a logical account are therefore always a data/model error.
        seen: set[tuple[int, str, str, str]] = set()
        for row in state.rows:
            key = (row.account_id, row.item_type, row.item_key, row.currency)
            if key in seen:
                raise AccountingError("duplicate row in normalized account valuation")
            seen.add(key)
            state.warnings.extend(row.coverage.warnings)
        return state

    def earliest_coverage_date(self) -> Optional[str]:
        dates: list[str] = []
        for table, column in (
            ("balances", "as_of"),
            ("positions", "as_of"),
            ("transactions", "date"),
            ("trades", "date"),
        ):
            if not _table_exists(self.con, table):
                continue
            row = self.con.execute(f"SELECT MIN({column}) FROM {table}").fetchone()
            if row and row[0]:
                dates.append(row[0])
        if self._has_canonical and "occurred_on" in self._canonical_columns:
            row = self.con.execute("SELECT MIN(occurred_on) FROM canonical_events").fetchone()
            if row and row[0]:
                dates.append(row[0])
        return min(dates) if dates else None

    def earliest_investment_date(self) -> Optional[str]:
        row = self.con.execute(
            """SELECT MIN(d) FROM (
                   SELECT MIN(b.as_of) AS d
                     FROM balances b JOIN accounts a ON a.id=b.account_id
                    WHERE a.kind='brokerage'
                   UNION ALL
                   SELECT MIN(p.as_of)
                     FROM positions p JOIN accounts a ON a.id=p.account_id
                    WHERE a.kind='brokerage'
                   UNION ALL
                   SELECT MIN(t.date)
                     FROM transactions t JOIN accounts a ON a.id=t.account_id
                    WHERE a.kind='brokerage'
                   UNION ALL
                   SELECT MIN(t.date)
                     FROM trades t JOIN accounts a ON a.id=t.account_id
                    WHERE a.kind='brokerage'
                   UNION ALL
                   SELECT MIN(c.occurred_on)
                     FROM canonical_events c JOIN accounts a ON a.id=c.account_id
                    WHERE a.kind='brokerage' AND c.status='confirmed'
               )"""
        ).fetchone()
        return str(row[0]) if row and row[0] else None
