"""Historical FX and security-price lookups for normalized accounting."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import sqlite3
from typing import Optional

from findash_core.currencies import canonical_currency, minor_unit_factor
from findash_core.errors import AccountingError, MissingRateError
from findash_core.models import ZERO


def _decimal(value) -> Decimal:
    return Decimal(str(value))


@dataclass(frozen=True)
class FxPoint:
    rate: Decimal
    date: str
    source: str


class FxBook:
    """Historical FX lookups that never borrow a rate from the future."""

    def __init__(self, con: sqlite3.Connection, reporting_currency: str = "ILS"):
        self.con = con
        self.reporting_currency = canonical_currency(reporting_currency)
        self._cache: dict[tuple[str, str, str], FxPoint] = {}

    def latest(self, source: str, target: str, on_or_before: str) -> FxPoint:
        source = canonical_currency(source)
        target = canonical_currency(target)
        if source == target:
            return FxPoint(Decimal("1"), on_or_before, "identity")
        key = (source, target, on_or_before)
        if key in self._cache:
            return self._cache[key]

        direct = self.con.execute(
            """SELECT date, rate, source FROM fx_rates
               WHERE base_currency=? AND quote_currency=? AND date<=?
               ORDER BY date DESC LIMIT 1""",
            (source, target, on_or_before),
        ).fetchone()
        inverse = self.con.execute(
            """SELECT date, rate, source FROM fx_rates
               WHERE base_currency=? AND quote_currency=? AND date<=?
               ORDER BY date DESC LIMIT 1""",
            (target, source, on_or_before),
        ).fetchone()

        # Direct and inverse rows are equivalent observations. Rank them by date,
        # then provenance, then orientation. This lets a same-day document rate
        # beat a Yahoo quote without letting any older quote mask a newer one.
        candidates = [
            (row, use_inverse)
            for row, use_inverse in ((direct, False), (inverse, True))
            if row
        ]
        selected_pair = max(
            candidates,
            key=lambda item: (
                str(item[0][0]),
                1 if str(item[0][2]).lower() == "document" else 0,
                1 if not item[1] else 0,
            ),
            default=None,
        )
        selected, use_inverse = selected_pair if selected_pair else (None, False)

        # Two same-day document observations should be reciprocals apart from
        # ordinary source rounding. A material disagreement is an audit conflict,
        # not a reason to silently pick whichever orientation happens to sort first.
        if (
            direct
            and inverse
            and str(direct[0]) == str(inverse[0])
            and str(direct[2]).lower() == "document"
            and str(inverse[2]).lower() == "document"
        ):
            direct_rate = _decimal(direct[1])
            inverse_rate = _decimal(inverse[1])
            if direct_rate <= ZERO or inverse_rate <= ZERO:
                raise AccountingError(
                    f"invalid authoritative {source}/{target} FX observations"
                )
            inverse_as_direct = Decimal("1") / inverse_rate
            relative_gap = abs(direct_rate - inverse_as_direct) / max(
                abs(direct_rate), abs(inverse_as_direct)
            )
            if relative_gap > Decimal("0.005"):
                raise AccountingError(
                    f"conflicting authoritative {source}/{target} FX observations "
                    f"on {direct[0]}"
                )

        if selected:
            raw = _decimal(selected[1])
            if raw <= ZERO:
                raise MissingRateError(
                    f"invalid {source}/{target} FX rate on or before {on_or_before}"
                )
            result = FxPoint(
                Decimal("1") / raw if use_inverse else raw,
                str(selected[0]),
                str(selected[2]),
            )
            self._cache[key] = result
            return result
        raise MissingRateError(
            f"no {source}/{target} FX rate on or before {on_or_before}"
        )

    def rate(self, source: str, target: str, on_or_before: str) -> Decimal:
        return self.latest(source, target, on_or_before).rate

    def convert_minor(
        self, amount_minor: Decimal | int, source: str, target: str, on_date: str
    ) -> Decimal:
        source = canonical_currency(source)
        target = canonical_currency(target)
        amount = _decimal(amount_minor)
        if amount == ZERO:
            return ZERO
        if source == target:
            return amount
        source_factor = _decimal(minor_unit_factor(source))
        target_factor = _decimal(minor_unit_factor(target))
        return amount / source_factor * self.rate(source, target, on_date) * target_factor

    def convert_minor_with_point(
        self, amount_minor: Decimal | int, source: str, target: str, on_date: str
    ) -> tuple[Decimal, FxPoint]:
        source = canonical_currency(source)
        target = canonical_currency(target)
        amount = _decimal(amount_minor)
        if amount == ZERO:
            return ZERO, FxPoint(Decimal("1"), on_date, "zero")
        point = self.latest(source, target, on_date)
        if source == target:
            return amount, point
        source_factor = _decimal(minor_unit_factor(source))
        target_factor = _decimal(minor_unit_factor(target))
        return amount / source_factor * point.rate * target_factor, point


@dataclass(frozen=True)
class PricePoint:
    security_id: int
    date: str
    close_minor: Decimal
    currency: str


class PriceBook:
    def __init__(self, con: sqlite3.Connection):
        self.con = con
        self._cache: dict[tuple[int, str], Optional[PricePoint]] = {}

    def latest(self, security_id: int, on_or_before: str) -> Optional[PricePoint]:
        key = (security_id, on_or_before)
        if key in self._cache:
            return self._cache[key]
        row = self.con.execute(
            """SELECT security_id, date, close_minor, currency FROM prices
               WHERE security_id=? AND date<=?
               ORDER BY date DESC LIMIT 1""",
            (security_id, on_or_before),
        ).fetchone()
        result = (
            PricePoint(
                security_id=int(row[0]),
                date=str(row[1]),
                close_minor=_decimal(row[2]),
                currency=canonical_currency(row[3]),
            )
            if row
            else None
        )
        self._cache[key] = result
        return result
