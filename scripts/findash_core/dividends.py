"""Best-effort market dividend events and conservative cadence projections.

These rows are analytical reference data. They never become cash transactions,
never alter net worth, and are never described as money that was received.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from fractions import Fraction
import json
import re
import statistics
from typing import Callable, Iterable, Optional, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from findash_core.currencies import (
    canonical_currency,
    minor_unit_factor,
    yahoo_amount_to_major,
    yahoo_quote_unit,
)


YAHOO_CHART_ROOT = "https://query1.finance.yahoo.com/v8/finance/chart"
YAHOO_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; findash/1.0)"}
_SAFE_YAHOO_SYMBOL = re.compile(r"[A-Za-z0-9.^=-]{1,40}")


@dataclass(frozen=True)
class DividendEvent:
    """One per-share market distribution on Yahoo's event date."""

    event_date: str
    amount_per_share_major: Decimal
    currency: str
    projected: bool = False
    source_key: str = ""


@dataclass(frozen=True)
class DividendSeries:
    events: tuple[DividendEvent, ...]
    currency: str


DividendProvider = Callable[[str, str], Optional[DividendSeries]]


def yahoo_symbol_for(ticker: str) -> Optional[str]:
    """Map a database ticker to a safe Yahoo path component."""

    if not isinstance(ticker, str):
        return None
    symbol = ticker.strip()
    if symbol.endswith(".LSE"):
        symbol = symbol[:-4] + ".L"
    elif symbol.endswith(".CRYPTO"):
        symbol = symbol[:-7] + "-USD"
    elif symbol == "BRK.B":
        symbol = "BRK-B"
    return symbol if _SAFE_YAHOO_SYMBOL.fullmatch(symbol) else None


def fetch_yahoo_dividend_series(
    ticker: str,
    expected_currency: str,
    *,
    range_str: str = "max",
    timeout: int = 8,
) -> Optional[DividendSeries]:
    """Fetch Yahoo chart dividend events for one validated security.

    Yahoo chart timestamps are treated as event/ex-dates, not guaranteed cash
    payment dates. Provider quote units such as GBp and ILA are normalized before
    any holding multiplication.
    """

    symbol = yahoo_symbol_for(ticker)
    if symbol is None:
        return None
    expected = canonical_currency(expected_currency)
    query = urlencode(
        {
            "interval": "1d",
            "range": range_str,
            "events": "div",
            "includeAdjustedClose": "false",
        }
    )
    url = f"{YAHOO_CHART_ROOT}/{quote(symbol, safe='.^=-')}?{query}"
    try:
        request = Request(url, headers=YAHOO_HEADERS)
        with urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
        result = payload["chart"]["result"][0]
        yahoo_currency = str((result.get("meta") or {}).get("currency") or "")
        quote_unit = yahoo_quote_unit(yahoo_currency)
        if quote_unit.currency != expected:
            return None
        raw_events = (result.get("events") or {}).get("dividends") or {}
        events: dict[tuple[str, Decimal], DividendEvent] = {}
        for provider_key, raw in raw_events.items():
            timestamp = raw.get("date")
            amount = raw.get("amount")
            if timestamp is None or amount is None:
                continue
            event_date = datetime.fromtimestamp(
                int(timestamp), tz=timezone.utc
            ).date().isoformat()
            per_share = yahoo_amount_to_major(amount, yahoo_currency)
            if per_share <= 0:
                continue
            event = DividendEvent(
                event_date,
                per_share,
                expected,
                source_key=str(provider_key),
            )
            events[(event_date, per_share)] = event
        return DividendSeries(
            tuple(sorted(events.values(), key=lambda row: row.event_date)),
            expected,
        )
    except (
        HTTPError,
        URLError,
        TimeoutError,
        OSError,
        ValueError,
        TypeError,
        KeyError,
        IndexError,
        InvalidOperation,
        json.JSONDecodeError,
    ):
        return None


def fetch_many(
    provider: DividendProvider,
    securities: Iterable[tuple[int, str, str]],
    *,
    workers: int = 6,
) -> tuple[dict[int, DividendSeries], int]:
    """Load independent series concurrently; failures remain count-only."""

    requests = list(securities)
    if not requests:
        return {}, 0
    loaded: dict[int, DividendSeries] = {}
    failures = 0
    with ThreadPoolExecutor(max_workers=min(workers, len(requests))) as pool:
        futures = {
            pool.submit(provider, ticker, currency): security_id
            for security_id, ticker, currency in requests
        }
        for future in as_completed(futures):
            security_id = futures[future]
            try:
                series = future.result()
            except Exception:
                # A third-party/injected provider must not make the private
                # dashboard fail. No provider error text or ticker is surfaced.
                series = None
            if series is None:
                failures += 1
            else:
                loaded[security_id] = series
    return loaded, failures


def _cadence_days(events: Sequence[DividendEvent]) -> Optional[int]:
    actual = [row for row in events if not row.projected]
    if len(actual) < 2:
        return None
    dates = [date.fromisoformat(row.event_date) for row in actual]
    diffs = [
        (dates[index + 1] - dates[index]).days
        for index in range(len(dates) - 1)
        if dates[index + 1] > dates[index]
    ][-8:]
    if not diffs:
        return None
    cadence = int(statistics.median(diffs))
    return cadence if 20 <= cadence <= 400 else None


def project_between(
    events: Sequence[DividendEvent], *, after: str, through: str
) -> list[DividendEvent]:
    """Project recent cadence strictly after ``after`` through ``through``.

    A lapsed series is not restarted years later. Projections use the latest
    per-share amount and are explicitly marked as such.
    """

    if not events:
        return []
    cadence = _cadence_days(events)
    if cadence is None:
        return []
    after_date = date.fromisoformat(after)
    through_date = date.fromisoformat(through)
    if through_date <= after_date:
        return []
    latest = max(events, key=lambda row: row.event_date)
    latest_date = date.fromisoformat(latest.event_date)
    if (after_date - latest_date).days > max(cadence * 2, 450):
        return []
    cursor = latest_date + timedelta(days=cadence)
    rows: list[DividendEvent] = []
    while cursor <= through_date and len(rows) < 48:
        if cursor > after_date:
            rows.append(
                DividendEvent(
                    cursor.isoformat(),
                    latest.amount_per_share_major,
                    latest.currency,
                    projected=True,
                )
            )
        cursor += timedelta(days=cadence)
    return rows


def event_minor_fraction(event: DividendEvent) -> tuple[int, int]:
    """Represent a per-share amount exactly as a rational minor-unit value."""

    value = event.amount_per_share_major * minor_unit_factor(event.currency)
    fraction = Fraction(value)
    if fraction <= 0:
        raise ValueError("dividend amount must be positive")
    return fraction.numerator, fraction.denominator


def event_from_minor_fraction(
    *,
    event_date: str,
    numerator: int,
    denominator: int,
    currency: str,
    source_key: str = "",
) -> DividendEvent:
    """Rebuild an exact event from the SQLite rational representation."""

    if (
        isinstance(numerator, bool)
        or isinstance(denominator, bool)
        or not isinstance(numerator, int)
        or not isinstance(denominator, int)
        or numerator <= 0
        or denominator <= 0
    ):
        raise ValueError("invalid dividend minor-unit fraction")
    code = canonical_currency(currency)
    amount_major = (
        Decimal(numerator)
        / Decimal(denominator)
        / Decimal(minor_unit_factor(code))
    )
    return DividendEvent(
        event_date,
        amount_major,
        code,
        source_key=source_key,
    )


__all__ = [
    "DividendEvent",
    "DividendProvider",
    "DividendSeries",
    "event_from_minor_fraction",
    "event_minor_fraction",
    "fetch_many",
    "fetch_yahoo_dividend_series",
    "project_between",
    "yahoo_symbol_for",
]
