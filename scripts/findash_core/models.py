"""Small value objects shared by the accounting and dashboard layers."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional


ZERO = Decimal("0")


@dataclass(frozen=True)
class Coverage:
    """How a calculated value was established.

    Coverage is intentionally carried to the renderer.  A snapshot-only value and a
    fully event-derived value may have the same number, but they do not make the same
    promise about history or freshness.
    """

    method: str
    anchor_date: Optional[str] = None
    observed_through: Optional[str] = None
    complete_from: Optional[str] = None
    stale_days: Optional[int] = None
    warnings: tuple[str, ...] = ()

    @property
    def is_partial(self) -> bool:
        return self.method in {"events-partial", "snapshot-only", "unpriced"} or bool(self.warnings)


@dataclass(frozen=True)
class ValuationRow:
    """One asset, cash component, or liability valued exactly once."""

    as_of: str
    account_id: int
    account_name: str
    account_kind: str
    institution: str
    bucket: str
    item_type: str
    item_key: str
    label: str
    currency: str
    native_value_minor: Decimal
    reporting_currency: str
    reporting_value_minor: Decimal
    coverage: Coverage
    quantity: Optional[Decimal] = None
    unit_price_minor: Optional[Decimal] = None
    cost_basis_minor: Optional[Decimal] = None
    reporting_cost_basis_minor: Optional[Decimal] = None
    security_id: Optional[int] = None
    ticker: Optional[str] = None


@dataclass(frozen=True)
class PositionState:
    """Quantity and remaining average-cost basis for one security."""

    account_id: int
    security_id: int
    ticker: str
    currency: str
    quantity: Decimal
    cost_basis_minor: Optional[Decimal]
    reporting_cost_basis_minor: Optional[Decimal]
    average_cost_minor: Optional[Decimal]
    anchor_date: Optional[str]
    observed_through: Optional[str]
    coverage: Coverage


@dataclass(frozen=True)
class ExternalFlow:
    """A signed flow into an investment account, normalized for performance math."""

    account_id: int
    date: str
    native_amount_minor: int
    currency: str
    reporting_amount_minor: Decimal
    transaction_id: Optional[int] = None
    source_doc_id: Optional[int] = None


@dataclass
class AccountState:
    """Normalized state returned to every dashboard consumer."""

    as_of: str
    reporting_currency: str
    rows: list[ValuationRow] = field(default_factory=list)
    positions: list[PositionState] = field(default_factory=list)
    external_flows: list[ExternalFlow] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def total_minor(self) -> Decimal:
        return sum((row.reporting_value_minor for row in self.rows), ZERO)

    def bucket_total_minor(self, bucket: str) -> Decimal:
        return sum(
            (row.reporting_value_minor for row in self.rows if row.bucket == bucket),
            ZERO,
        )

    def account_total_minor(self, account_id: int) -> Decimal:
        return sum(
            (row.reporting_value_minor for row in self.rows if row.account_id == account_id),
            ZERO,
        )
