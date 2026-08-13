"""Canonical transaction accounting classes.

Display categories stay open-vocabulary and are chosen with judgment at ingestion.
`flow_type` is the deliberately small accounting dimension used by calculations.
"""

from __future__ import annotations

from typing import Mapping, Optional


FLOW_TYPES = frozenset({
    "expense",
    "income",
    "transfer",
    "investment",
    "tax",
    "refund",
    "other",
})


# Compatibility for databases ingested before `transactions.flow_type` existed.  This
# mapping is intentionally about accounting meaning, never merchant-name matching.
_CATEGORY_FLOW_TYPES: Mapping[str, str] = {
    "salary": "income",
    "income": "income",
    "dividend": "income",
    "interest": "income",
    "bank_gift": "income",
    "bank_credit": "income",
    "government_payment": "income",
    "refund": "refund",
    "transfer": "transfer",
    "card_payment": "transfer",
    "savings_deposit": "transfer",
    "savings_withdrawal": "transfer",
    "deposit": "transfer",
    "withdrawal": "transfer",
    "pension_deposit": "transfer",
    "study_fund_deposit": "transfer",
    # Internal FX reallocates cash already inside the same account. It is never
    # external capital, even in legacy rows that predate explicit flow_type.
    "fx": "investment",
    "securities_buy": "investment",
    "securities_sell": "investment",
    "realized_gain": "investment",
    "fee": "expense",
    "tax": "tax",
}


def flow_type_for(
    category: Optional[str],
    explicit: Optional[str],
    amount_minor: int,
) -> str:
    """Return the stable accounting class for a transaction.

    New ingestion should always provide ``explicit``.  The sign-based final fallback
    preserves old databases without making display categories part of the new API.
    """

    if explicit:
        if explicit not in FLOW_TYPES:
            raise ValueError(f"unsupported transaction flow_type: {explicit}")
        return explicit
    mapped = _CATEGORY_FLOW_TYPES.get((category or "").strip().casefold())
    if mapped:
        return mapped
    return "expense" if amount_minor < 0 else "other"


def is_consumer_expense(category: Optional[str], explicit: Optional[str], amount_minor: int) -> bool:
    return amount_minor < 0 and flow_type_for(category, explicit, amount_minor) == "expense"
