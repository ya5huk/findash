"""Signed position quantity and average-cost basis transitions."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

from findash_core.errors import AccountingError
from findash_core.models import ZERO


@dataclass
class Holding:
    security_id: int
    ticker: str
    currency: str
    quantity: Decimal = ZERO
    cost_minor: Optional[Decimal] = ZERO
    reporting_cost_minor: Optional[Decimal] = ZERO
    anchor_date: Optional[str] = None
    observed_through: Optional[str] = None
    warnings: list[str] = field(default_factory=list)
    snapshot_price_minor: Optional[Decimal] = None
    snapshot_value_minor: Optional[Decimal] = None

    def apply_trade(
        self,
        *,
        side: str,
        shares: Decimal,
        price_minor: Decimal,
        fees_minor: Decimal,
        reporting_trade_cost_minor: Decimal,
        reporting_fees_minor: Decimal,
        trade_date: str,
    ) -> None:
        if shares <= ZERO:
            self.warnings.append(f"non-positive trade quantity on {trade_date}")
            return
        if side not in {"buy", "sell"}:
            raise AccountingError("unsupported trade side in normalized trade event")

        delta = shares if side == "buy" else -shares
        gross_minor = shares * price_minor

        def opening_basis(
            direction: Decimal,
            native_gross: Decimal,
            native_fees: Decimal,
            reporting_gross: Decimal,
            reporting_fees: Decimal,
        ) -> tuple[Decimal, Decimal]:
            if direction > ZERO:
                return (
                    native_gross + native_fees,
                    reporting_gross + reporting_fees,
                )
            return (
                -native_gross + native_fees,
                -reporting_gross + reporting_fees,
            )

        # A same-direction trade extends the existing average-cost segment.
        if self.quantity == ZERO or self.quantity * delta > ZERO:
            native_basis, reporting_basis = opening_basis(
                delta,
                gross_minor,
                fees_minor,
                reporting_trade_cost_minor,
                reporting_fees_minor,
            )
            self.quantity += delta
            if self.cost_minor is not None:
                self.cost_minor += native_basis
            if self.reporting_cost_minor is not None:
                self.reporting_cost_minor += reporting_basis
            self.observed_through = trade_date
            return

        # The trade closes some or all of the opposite-signed position. Closing
        # costs affect realized P/L, not the remaining position's average basis.
        opening_quantity = abs(self.quantity)
        closed_quantity = min(opening_quantity, shares)
        remaining_quantity = opening_quantity - closed_quantity
        excess_quantity = shares - closed_quantity

        if remaining_quantity > ZERO:
            fraction_remaining = remaining_quantity / opening_quantity
            self.quantity += delta
            if self.cost_minor is not None:
                self.cost_minor *= fraction_remaining
            if self.reporting_cost_minor is not None:
                self.reporting_cost_minor *= fraction_remaining
        elif excess_quantity == ZERO:
            self.quantity = ZERO
            # Even an unknown opening basis is known to be zero after the position
            # is fully closed. A later trade therefore starts a clean segment.
            self.cost_minor = ZERO
            self.reporting_cost_minor = ZERO
        else:
            # A crossing trade closes the old segment and opens a new, opposite one.
            # Only the proportional gross and fees belonging to the excess shares
            # become basis for that new segment.
            opening_fraction = excess_quantity / shares
            native_basis, reporting_basis = opening_basis(
                delta,
                gross_minor * opening_fraction,
                fees_minor * opening_fraction,
                reporting_trade_cost_minor * opening_fraction,
                reporting_fees_minor * opening_fraction,
            )
            self.quantity = excess_quantity if delta > ZERO else -excess_quantity
            self.cost_minor = native_basis
            self.reporting_cost_minor = reporting_basis
        self.observed_through = trade_date
