"""Market reference rates and the orientation/cost rules for FX conversions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
import sqlite3
from typing import Optional

from findash_core.currencies import canonical_currency, minor_to_major


REPORTING_CURRENCY = "ILS"
MARKET_RATE_MAX_AGE_DAYS = 5
# Observed broker markups sit between roughly 0.6% and 1.7%. A conversion may beat
# the daily close slightly (intraday timing, previous-day pricing) but is never
# expected to be far better than market.
BAND_BETTER = Decimal("0.01")
BAND_WORSE = Decimal("0.025")


@dataclass(frozen=True)
class MarketRate:
    rate: Decimal
    date: str


def rate_orientation(
    sell_currency: str,
    buy_currency: str,
    reporting_currency: str = REPORTING_CURRENCY,
) -> tuple[str, str]:
    """Return ``(base, quote)`` so the rate reads the way people quote it.

    With the reporting currency on one side the rate is "reporting units per one
    foreign unit" (ILS per USD) regardless of direction. Otherwise it is "sold
    units per one bought unit".
    """

    sell = canonical_currency(sell_currency)
    buy = canonical_currency(buy_currency)
    reporting = canonical_currency(reporting_currency)
    if sell == buy:
        raise ValueError("a conversion needs two different currencies")
    if buy == reporting:
        return sell, buy
    return buy, sell


def implied_rate(
    sell_minor: int,
    sell_currency: str,
    buy_minor: int,
    buy_currency: str,
    base_currency: str,
) -> Decimal:
    """Quote units per one base unit implied by the two cash legs."""

    amounts = {
        canonical_currency(sell_currency): minor_to_major(abs(sell_minor), sell_currency),
        canonical_currency(buy_currency): minor_to_major(abs(buy_minor), buy_currency),
    }
    base = canonical_currency(base_currency)
    (quote,) = set(amounts) - {base}
    if amounts[base] == 0:
        raise ValueError("a conversion leg cannot be zero")
    return amounts[quote] / amounts[base]


def market_rate(
    connection: sqlite3.Connection,
    base_currency: str,
    quote_currency: str,
    on_date: str,
    *,
    max_age_days: int = MARKET_RATE_MAX_AGE_DAYS,
) -> Optional[MarketRate]:
    """Latest market close on or before ``on_date``; document rates never count."""

    base = canonical_currency(base_currency)
    quote = canonical_currency(quote_currency)
    earliest = (date.fromisoformat(on_date) - timedelta(days=max_age_days)).isoformat()
    best: Optional[MarketRate] = None
    for row_base, row_quote, inverse in ((base, quote, False), (quote, base, True)):
        row = connection.execute(
            """SELECT date, rate FROM fx_rates
                WHERE base_currency=? AND quote_currency=?
                  AND date BETWEEN ? AND ?
                  AND lower(source)<>'document'
                ORDER BY date DESC LIMIT 1""",
            (row_base, row_quote, earliest, on_date),
        ).fetchone()
        if row is None or float(row[1]) <= 0:
            continue
        value = Decimal(str(row[1]))
        candidate = MarketRate(Decimal(1) / value if inverse else value, str(row[0]))
        if best is None or candidate.date > best.date:
            best = candidate
    return best


def conversion_cost(
    actual_rate: Decimal,
    market: Decimal,
    *,
    sell_currency: str,
    base_currency: str,
) -> Decimal:
    """Fraction paid above market; negative means better than market."""

    if canonical_currency(sell_currency) == canonical_currency(base_currency):
        # Sold base units: fewer quote units per base is worse.
        return market / actual_rate - 1
    # Bought base units: more quote units per base is worse.
    return actual_rate / market - 1


def within_band(cost: Decimal) -> bool:
    return -BAND_BETTER <= cost <= BAND_WORSE


__all__ = [
    "BAND_BETTER",
    "BAND_WORSE",
    "MARKET_RATE_MAX_AGE_DAYS",
    "MarketRate",
    "REPORTING_CURRENCY",
    "conversion_cost",
    "implied_rate",
    "market_rate",
    "rate_orientation",
    "within_band",
]
