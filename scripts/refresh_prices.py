#!/usr/bin/env python3
"""
refresh_prices.py — keep the `prices` and `fx_rates` tables fresh from Yahoo Finance.

Modes:
  --range 1mo (default): daily top-up for currently-held securities + benchmark + FX.
  --range 3y           : full backfill for every security with any trade ever + benchmark + FX.

Any security with zero existing price rows is auto-promoted to a 3y fetch regardless
of the mode flag (covers the "I bought a new ticker today" case).

Writes:
  prices    — INSERT OR IGNORE (market closes are pure reference data, no conflict).
  fx_rates  — source-aware UPSERT that NEVER overwrites rows with source='document'.
              Docs are the user's actual conversion rate on a given day; Yahoo only
              fills gaps and refreshes its own historical rows.

Partial failures (a single ticker 429s twice) are logged but non-fatal — the next
run picks them up. Script exits non-zero only if every fetch failed.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

YAHOO_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; findash/1.0)"}
REQUEST_DELAY_S = 0.25
RETRY_SLEEP_S = 2.0
DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "finance.db"
VALID_RANGES = ["1mo", "3mo", "6mo", "1y", "2y", "3y", "5y", "max"]


def yahoo_symbol_for(ticker: str) -> str:
    if ticker.endswith(".LSE"):
        return ticker[:-4] + ".L"
    if ticker.endswith(".CRYPTO"):
        # Yahoo lists crypto as <BASE>-USD (e.g. ETH-USD, BTC-USD).
        return ticker[:-7] + "-USD"
    if ticker == "BRK.B":
        # Berkshire Hathaway Class B is BRK-B on Yahoo (hyphen, not dot).
        return "BRK-B"
    return ticker


def fetch_yahoo_history(
    symbol: str, range_str: str, timeout: int = 20
) -> Optional[list[tuple[str, float]]]:
    """List of (ISO date, close) tuples, or None on failure. One retry on 429."""
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        f"?interval=1d&range={range_str}"
    )
    for attempt in range(2):
        try:
            req = urllib.request.Request(url, headers=YAHOO_HEADERS)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.load(resp)
            result = data["chart"]["result"][0]
            timestamps = result.get("timestamp") or []
            closes = (
                result.get("indicators", {}).get("quote", [{}])[0].get("close") or []
            )
            out: list[tuple[str, float]] = []
            for ts, close in zip(timestamps, closes):
                if close is None:
                    continue
                d = datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()
                out.append((d, float(close)))
            return out
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt == 0:
                time.sleep(RETRY_SLEEP_S)
                continue
            return None
        except (urllib.error.URLError, KeyError, TypeError, ValueError, TimeoutError):
            return None
    return None


def close_to_minor(close: float, ccy: str) -> int:
    # Yahoo returns LSE prices in pence and TASE in agorot — already minor units.
    if ccy == "USD":
        return round(close * 100)
    return round(close)


def securities_to_fetch(cur: sqlite3.Cursor, mode_range: str) -> list[dict]:
    if mode_range == "3y" or mode_range == "5y" or mode_range == "max":
        ids = [r[0] for r in cur.execute(
            "SELECT DISTINCT security_id FROM trades"
        ).fetchall()]
    else:
        rows = cur.execute("""
            SELECT security_id
            FROM trades
            GROUP BY security_id
            HAVING SUM(CASE WHEN side='buy' THEN shares ELSE -shares END) > 1e-9
        """).fetchall()
        ids = [r[0] for r in rows]

    spy_row = cur.execute("SELECT id FROM securities WHERE ticker='SPY'").fetchone()
    if spy_row and spy_row[0] not in ids:
        ids.append(spy_row[0])

    out: list[dict] = []
    for sid in ids:
        row = cur.execute(
            "SELECT ticker, currency FROM securities WHERE id=?", (sid,)
        ).fetchone()
        if not row:
            continue
        has_price = cur.execute(
            "SELECT 1 FROM prices WHERE security_id=? LIMIT 1", (sid,)
        ).fetchone()
        out.append({
            "id": sid,
            "ticker": row[0],
            "currency": row[1],
            "range": "3y" if not has_price else mode_range,
        })
    return out


def refresh_security(
    cur: sqlite3.Cursor, sid: int, ticker: str, ccy: str, range_str: str
) -> tuple[int, bool]:
    hist = fetch_yahoo_history(yahoo_symbol_for(ticker), range_str)
    if hist is None:
        return 0, False
    inserted = 0
    for d, close in hist:
        cur.execute(
            "INSERT OR IGNORE INTO prices (security_id, date, close_minor, currency) "
            "VALUES (?,?,?,?)",
            (sid, d, close_to_minor(close, ccy), ccy),
        )
        inserted += cur.rowcount
    return inserted, True


def refresh_fx(
    cur: sqlite3.Cursor, base: str, quote: str, range_str: str
) -> tuple[int, bool]:
    sym = f"{base}{quote}=X"
    hist = fetch_yahoo_history(sym, range_str)
    if hist is None:
        return 0, False
    written = 0
    for d, rate in hist:
        # Insert; on conflict, update only if existing source != 'document'.
        cur.execute(
            """
            INSERT INTO fx_rates (date, base_currency, quote_currency, rate, source)
            VALUES (?, ?, ?, ?, 'yahoo')
            ON CONFLICT(date, base_currency, quote_currency) DO UPDATE
              SET rate = excluded.rate, source = excluded.source
              WHERE fx_rates.source != 'document'
            """,
            (d, base, quote, rate),
        )
        if cur.rowcount > 0:
            written += 1
    return written, True


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Refresh prices and FX rates from Yahoo Finance.",
    )
    ap.add_argument(
        "--range", default="1mo", choices=VALID_RANGES,
        help="Time range to fetch (default 1mo for daily top-up; use 3y for backfill).",
    )
    ap.add_argument("--db", default=str(DEFAULT_DB), help="Path to SQLite DB.")
    args = ap.parse_args()

    con = sqlite3.connect(args.db)
    cur = con.cursor()

    targets = securities_to_fetch(cur, args.range)
    print(f"Refreshing {len(targets)} security row(s), mode={args.range}:")

    failed: list[str] = []
    total_inserted = 0
    for i, t in enumerate(targets):
        if i > 0:
            time.sleep(REQUEST_DELAY_S)
        n, ok = refresh_security(cur, t["id"], t["ticker"], t["currency"], t["range"])
        marker = f"+{n} rows" if ok else "FAILED"
        print(f"  {t['ticker']:12s} [{t['range']:>3}]  {marker}")
        if not ok:
            failed.append(t["ticker"])
        total_inserted += n
    con.commit()

    print()
    print(f"FX rates, mode={args.range}:")
    fx_failed: list[str] = []
    fx_pairs = [("USD", "ILS"), ("GBP", "ILS")]
    for base, quote in fx_pairs:
        time.sleep(REQUEST_DELAY_S)
        n, ok = refresh_fx(cur, base, quote, args.range)
        marker = f"{n} rows written" if ok else "FAILED"
        print(f"  {base}->{quote}      {marker}")
        if not ok:
            fx_failed.append(f"{base}{quote}")
    con.commit()
    con.close()

    print()
    print(f"Total price rows inserted: {total_inserted}")
    if failed:
        print(f"Failed price tickers: {', '.join(failed)} (will retry on next run)")
    if fx_failed:
        print(f"Failed FX pairs: {', '.join(fx_failed)} (will retry on next run)")

    total_attempts = len(targets) + len(fx_pairs)
    total_failures = len(failed) + len(fx_failed)
    if total_failures and total_failures == total_attempts:
        sys.exit(1)


if __name__ == "__main__":
    main()
