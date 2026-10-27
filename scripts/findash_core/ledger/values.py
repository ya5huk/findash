"""Strict parsing of intent fields: closed shapes, exact money, ISO dates."""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Optional

from findash_core.currencies import canonical_currency, minor_unit_exponent


class IntentError(ValueError):
    """An intent is malformed, contradicts recorded facts, or cannot be applied.

    Messages name fields and intent positions only, never private values.
    """


def require_shape(
    payload: Any,
    label: str,
    *,
    required: Iterable[str],
    optional: Iterable[str] = (),
) -> dict:
    if not isinstance(payload, dict):
        raise IntentError(f"{label} must be an object")
    required = set(required)
    allowed = required | set(optional)
    missing = sorted(required - set(payload))
    unknown = sorted(set(payload) - allowed)
    if missing or unknown:
        details = []
        if missing:
            details.append(f"missing {', '.join(missing)}")
        if unknown:
            details.append(f"unknown {', '.join(unknown)}")
        raise IntentError(f"{label}: {'; '.join(details)}")
    return payload


def iso_date(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise IntentError(f"{field} must be a YYYY-MM-DD string")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise IntentError(f"{field} must be a YYYY-MM-DD string") from exc
    if parsed.isoformat() != value:
        raise IntentError(f"{field} must be a YYYY-MM-DD string")
    return value


def optional_date(payload: dict, key: str, field: str) -> Optional[str]:
    return iso_date(payload[key], field) if payload.get(key) is not None else None


def currency(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise IntentError(f"{field} must be an ISO currency code")
    try:
        return canonical_currency(value)
    except (TypeError, ValueError) as exc:
        raise IntentError(f"{field} must be an ISO currency code") from exc


def decimal(value: Any, field: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise IntentError(f"{field} must be a decimal number or numeric string")
    try:
        result = Decimal(str(value).strip())
    except InvalidOperation as exc:
        raise IntentError(f"{field} must be a decimal number or numeric string") from exc
    if not result.is_finite():
        raise IntentError(f"{field} must be finite")
    return result


def minor(value: Any, iso_currency: str, field: str) -> int:
    """Exact major-unit amount to minor units; extra precision is an error."""

    amount = decimal(value, field)
    exponent = minor_unit_exponent(iso_currency)
    scaled = amount.scaleb(exponent)
    if scaled != scaled.to_integral_value():
        raise IntentError(f"{field} has more decimals than {iso_currency} allows")
    return int(scaled)


def positive_minor(value: Any, iso_currency: str, field: str) -> int:
    amount = minor(value, iso_currency, field)
    if amount <= 0:
        raise IntentError(f"{field} must be positive")
    return amount


def text(value: Any, field: str, *, optional: bool = True) -> Optional[str]:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value.strip():
        raise IntentError(f"{field} must be non-empty text")
    return value.strip()


def positive_id(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise IntentError(f"{field} must be a positive integer id")
    return value


__all__ = [
    "IntentError",
    "currency",
    "decimal",
    "iso_date",
    "minor",
    "optional_date",
    "positive_id",
    "positive_minor",
    "require_shape",
    "text",
]
