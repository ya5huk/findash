"""Pure currency and quote-unit normalization helpers.

Database money uses ISO currency codes and integer minor units. Market-data
providers sometimes quote in a *minor-unit code* instead (Yahoo's ``GBp`` /
``GBX`` for pence and ``ILA`` for agorot). Keeping that distinction here avoids
scattering special cases through ingestion, valuation, and rendering code.

All arithmetic uses :class:`~decimal.Decimal`; floats are first converted
through ``str`` so their binary representation does not leak into rounding.
"""

from __future__ import annotations

from functools import lru_cache
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Union


Number = Union[int, float, str, Decimal]


@dataclass(frozen=True)
class QuoteUnit:
    """How one provider quote maps to the database's ISO currency."""

    currency: str
    is_minor: bool


# ISO 4217 currencies are normally expressed with two decimal places. These
# are the current exceptions. Codes whose ISO minor-unit field is "N.A." are
# rejected rather than silently inventing a scale.
_ZERO_DECIMAL = frozenset({
    "BIF", "CLP", "DJF", "GNF", "ISK", "JPY", "KMF", "KRW", "PYG",
    "RWF", "UGX", "UYI", "VND", "VUV", "XAF", "XOF", "XPF",
})
_THREE_DECIMAL = frozenset({"BHD", "IQD", "JOD", "KWD", "LYD", "OMR", "TND"})
_FOUR_DECIMAL = frozenset({"CLF", "UYW"})
_NO_MINOR_UNIT = frozenset({
    "XAG", "XAU", "XBA", "XBB", "XBC", "XBD", "XDR", "XPD", "XPT",
    "XSU", "XTS", "XUA", "XXX",
})

# Case matters for GBp: ``GBP`` means pounds, while Yahoo's ``GBp`` means
# pence. ILA is Yahoo's agorot quote unit, not a separate portfolio currency.
_YAHOO_MINOR_QUOTES = {
    "GBp": QuoteUnit("GBP", True),
    "GBX": QuoteUnit("GBP", True),
    "ILA": QuoteUnit("ILS", True),
}


def _decimal(value: Number, *, name: str) -> Decimal:
    if isinstance(value, bool):
        raise TypeError(f"{name} must be numeric, not bool")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{name} must be a finite number") from exc
    if not result.is_finite():
        raise ValueError(f"{name} must be a finite number")
    return result


@lru_cache(maxsize=None)
def canonical_currency(code: str) -> str:
    """Return the ISO currency represented by ``code``.

    Yahoo quote-unit aliases are accepted so callers cannot accidentally treat
    ``ILA`` as a currency separate from ILS or ``GBp`` as separate from GBP.
    """

    if not isinstance(code, str):
        raise TypeError("currency code must be a string")
    stripped = code.strip()
    if stripped in _YAHOO_MINOR_QUOTES:
        return _YAHOO_MINOR_QUOTES[stripped].currency
    normalized = stripped.upper()
    if not re.fullmatch(r"[A-Z]{3}", normalized):
        raise ValueError(f"invalid currency code: {code!r}")
    return normalized


def minor_unit_exponent(currency: str) -> int:
    """Return the ISO 4217 minor-unit exponent for ``currency``.

    For example, USD/ILS/GBP return 2, JPY returns 0, KWD returns 3, and CLF
    returns 4. Non-currency ISO accounting codes have no minor unit and raise.
    """

    code = canonical_currency(currency)
    if code in _NO_MINOR_UNIT:
        raise ValueError(f"currency has no ISO minor unit: {code}")
    if code in _ZERO_DECIMAL:
        return 0
    if code in _THREE_DECIMAL:
        return 3
    if code in _FOUR_DECIMAL:
        return 4
    return 2


def minor_unit_factor(currency: str) -> int:
    """Return the integer number of minor units in one major unit."""

    return 10 ** minor_unit_exponent(currency)


def major_to_minor(amount: Number, currency: str) -> int:
    """Round a major-unit amount to an integer ISO minor-unit amount."""

    value = _decimal(amount, name="amount")
    scaled = value * minor_unit_factor(currency)
    return int(scaled.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def minor_to_major(amount_minor: int, currency: str) -> Decimal:
    """Convert an integer minor-unit amount to exact major units."""

    if isinstance(amount_minor, bool) or not isinstance(amount_minor, int):
        raise TypeError("amount_minor must be an integer")
    return Decimal(amount_minor) / minor_unit_factor(currency)


def yahoo_quote_unit(code: str) -> QuoteUnit:
    """Describe a Yahoo quote currency without losing quote-unit semantics."""

    if not isinstance(code, str):
        raise TypeError("Yahoo quote currency must be a string")
    stripped = code.strip()
    if stripped in _YAHOO_MINOR_QUOTES:
        return _YAHOO_MINOR_QUOTES[stripped]
    return QuoteUnit(canonical_currency(stripped), False)


def yahoo_amount_to_major(amount: Number, yahoo_currency: str) -> Decimal:
    """Normalize a Yahoo amount to major units of its canonical currency."""

    value = _decimal(amount, name="Yahoo amount")
    quote = yahoo_quote_unit(yahoo_currency)
    if quote.is_minor:
        return value / minor_unit_factor(quote.currency)
    return value


def yahoo_price_to_minor(
    price: Number,
    yahoo_currency: str,
    *,
    expected_currency: str | None = None,
) -> int:
    """Normalize a Yahoo price to the database's ``price_minor`` convention.

    ``expected_currency`` is optional but recommended: a provider/DB currency
    mismatch then fails loudly instead of producing a plausible wrong value.
    """

    quote = yahoo_quote_unit(yahoo_currency)
    if expected_currency is not None:
        expected = canonical_currency(expected_currency)
        if quote.currency != expected:
            raise ValueError(
                f"Yahoo quote currency {quote.currency} does not match {expected}"
            )
    if quote.is_minor:
        value = _decimal(price, name="Yahoo price")
        return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    return major_to_minor(price, quote.currency)


def convert_minor(
    amount_minor: int,
    source_currency: str,
    target_currency: str,
    rate: Number,
) -> int:
    """Convert integer minor units using ``1 source = rate target``.

    The result is rounded once, in the target currency's minor unit. Negative
    values are supported; zero or negative FX rates are rejected.
    """

    source = canonical_currency(source_currency)
    target = canonical_currency(target_currency)
    fx = _decimal(rate, name="FX rate")
    if fx <= 0:
        raise ValueError("FX rate must be greater than zero")
    source_major = minor_to_major(amount_minor, source)
    return major_to_minor(source_major * fx, target)


def to_ils_minor(
    amount_minor: int,
    currency: str,
    ils_per_unit: Number | None = None,
) -> int:
    """Convert a minor-unit amount to ILS minor units.

    ``ils_per_unit`` is required for foreign currency and means ILS per one
    major unit of ``currency``. ILS/ILA amounts pass through exactly.
    """

    source = canonical_currency(currency)
    if source == "ILS":
        if isinstance(amount_minor, bool) or not isinstance(amount_minor, int):
            raise TypeError("amount_minor must be an integer")
        return amount_minor
    if ils_per_unit is None:
        raise ValueError(f"missing {source}/ILS FX rate")
    return convert_minor(amount_minor, source, "ILS", ils_per_unit)


__all__ = [
    "QuoteUnit",
    "canonical_currency",
    "convert_minor",
    "major_to_minor",
    "minor_to_major",
    "minor_unit_exponent",
    "minor_unit_factor",
    "to_ils_minor",
    "yahoo_amount_to_major",
    "yahoo_price_to_minor",
    "yahoo_quote_unit",
]
