"""Shared failures raised by deterministic accounting calculations."""


class AccountingError(RuntimeError):
    """A fact set cannot be valued honestly."""


class MissingRateError(AccountingError):
    """No valid historical conversion rate exists for a requested value."""
