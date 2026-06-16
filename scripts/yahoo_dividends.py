#!/usr/bin/env python3
"""yahoo_dividends.py — fetch dividend events for a ticker.

Stdlib-only. Hits Yahoo's chart endpoint with `events=div` to get historical
dividend payments, then projects the next pay date by extrapolating the
median interval between past payments. The most recent historical amount is
reused as the estimate for the next one (close enough for ETFs that pay a
regular cadence; unreliable for irregular payers — see `next: null` cases).

Amounts are returned EXACTLY as Yahoo reports them, alongside `yahoo_currency`
so the caller can decide units. Yahoo reports US-listed dividends in dollars
(USD) and LSE-listed dividends in pence with `yahoo_currency='GBp'` — the
sync skill is responsible for dividing GBp by 100 to get pounds before
multiplying by shares × 0.75 for the net transaction amount.

Usage:
    python3 scripts/yahoo_dividends.py <ticker>

Output (JSON to stdout):
    {
      "ticker": "JEPI",
      "yahoo_symbol": "JEPI",
      "yahoo_currency": "USD",
      "past": [{"pay_date": "2025-12-01", "amount_per_share": 0.421}, ...],
      "next": {"pay_date": "2026-01-01", "amount_per_share": 0.421} | null
    }

Exit codes:
    0  on success (even if past is empty / next is null).
    1  on fetch failure.
    2  on invalid arguments.
"""

from __future__ import annotations

import json
import statistics
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from typing import Optional


YAHOO_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; findash/1.0)"}


def yahoo_symbol_for(ticker: str) -> str:
    if ticker.endswith(".LSE"):
        return ticker[:-4] + ".L"
    if ticker.endswith(".CRYPTO"):
        return ticker[:-7] + "-USD"
    if ticker == "BRK.B":
        return "BRK-B"
    return ticker


def fetch_chart(symbol: str, range_str: str = "5y", timeout: int = 20) -> Optional[dict]:
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        f"?interval=1d&range={range_str}&events=div"
    )
    try:
        req = urllib.request.Request(url, headers=YAHOO_HEADERS)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.load(resp)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError):
        return None


def extract_past(chart_result: dict) -> list[dict]:
    events = (chart_result.get("events") or {}).get("dividends") or {}
    out: list[dict] = []
    for v in events.values():
        ts = v.get("date")
        amt = v.get("amount")
        if ts is None or amt is None:
            continue
        d = datetime.fromtimestamp(int(ts), tz=timezone.utc).date().isoformat()
        out.append({"pay_date": d, "amount_per_share": float(amt)})
    out.sort(key=lambda x: x["pay_date"])
    return out


def _cadence_days(past: list[dict]) -> Optional[int]:
    """Median days between consecutive past payments, or None when the cadence
    can't be trusted: fewer than 2 events, or an implausible interval (one-time
    specials and irregular payouts land here).
    Monthly ≈ 30d, quarterly ≈ 90d, semi-annual ≈ 180d, annual ≈ 365d."""
    if len(past) < 2:
        return None
    dates = [date.fromisoformat(p["pay_date"]) for p in past]
    diffs = [(dates[i + 1] - dates[i]).days for i in range(len(dates) - 1)]
    if not diffs:
        return None
    median_days = int(statistics.median(diffs))
    if median_days < 20 or median_days > 400:
        return None
    return median_days


def project_next(past: list[dict]) -> Optional[dict]:
    """Project the next pay_date by median interval between past payments.
    Returns None for fewer than 2 events or out-of-band cadences (one-time
    specials, irregular payouts)."""
    median_days = _cadence_days(past)
    if median_days is None:
        return None
    next_date = date.fromisoformat(past[-1]["pay_date"]) + timedelta(days=median_days)
    return {
        "pay_date": next_date.isoformat(),
        "amount_per_share": past[-1]["amount_per_share"],
    }


def project_through(past: list[dict], end_iso: str,
                    after_iso: Optional[str] = None) -> list[dict]:
    """Project EVERY pay_date on/before `end_iso` (and strictly after `after_iso`
    when given), stepping the last past payment forward by the median interval.
    The most recent past amount is reused for each projected event. Returns [] for
    the same untrustworthy-cadence cases `project_next` returns None for.

    Used for "how much will I still receive this year": pass Dec 31 as `end_iso`
    and today as `after_iso` to get the remaining payments for held positions."""
    median_days = _cadence_days(past)
    if median_days is None:
        return []
    end = date.fromisoformat(end_iso)
    after = date.fromisoformat(after_iso) if after_iso else None
    amount = past[-1]["amount_per_share"]
    out: list[dict] = []
    nxt = date.fromisoformat(past[-1]["pay_date"]) + timedelta(days=median_days)
    # median_days >= 20 guarantees forward progress; the count cap is a belt-and-
    # suspenders guard against a pathological span.
    while nxt <= end and len(out) < 24:
        if after is None or nxt > after:
            out.append({"pay_date": nxt.isoformat(), "amount_per_share": amount})
        nxt += timedelta(days=median_days)
    return out


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("Usage: yahoo_dividends.py <ticker>", file=sys.stderr)
        return 2
    ticker = argv[1]
    symbol = yahoo_symbol_for(ticker)

    data = fetch_chart(symbol)
    if data is None:
        print(json.dumps({
            "ticker": ticker, "yahoo_symbol": symbol,
            "yahoo_currency": None, "past": [], "next": None,
            "error": "fetch_failed",
        }))
        return 1

    try:
        result = data["chart"]["result"][0]
    except (KeyError, IndexError, TypeError):
        print(json.dumps({
            "ticker": ticker, "yahoo_symbol": symbol,
            "yahoo_currency": None, "past": [], "next": None,
            "error": "no_result",
        }))
        return 1

    yahoo_currency = (result.get("meta") or {}).get("currency")
    past = extract_past(result)
    next_div = project_next(past)

    print(json.dumps({
        "ticker": ticker,
        "yahoo_symbol": symbol,
        "yahoo_currency": yahoo_currency,
        "past": past,
        "next": next_div,
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
