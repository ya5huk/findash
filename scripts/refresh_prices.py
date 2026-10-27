#!/usr/bin/env python3
"""
refresh_prices.py — keep the `prices` and `fx_rates` tables fresh from Yahoo Finance.

Modes:
  --range 1mo (default): daily top-up for currently-held securities + benchmark + FX.
  --range 3y           : full backfill for every security with any trade ever + benchmark + FX.
  --intraday           : also fetch the current session's intraday series (incl.
                         pre/post market) for currently-held securities.
  --intraday-only      : only the intraday fetch — the light between-full refresh.

Any security with zero existing price rows is auto-promoted to a 3y fetch regardless
of the mode flag (covers the "I bought a new ticker today" case).

Writes:
  prices    — provider close cache, updated deterministically by security + date.
  security_price_observations — append-only values seen by each live refresh; the
              intraday fetch backfills the whole session at provider timestamps
              (source='yahoo-intraday') so the dashboard sparkline is complete
              even when the dashboard was just opened.
  meta      — 'market_session:<security_id>' JSON with the fetched session's
              pre/regular/post boundaries (UTC), so the dashboard can tell a
              pre-market series from a regular one.
  fx_rates  — source-aware UPSERT that NEVER overwrites rows with source='document'.
  fx_rate_observations — append-only values seen by each live refresh.
              Docs are the user's actual conversion rate on a given day; Yahoo only
              fills gaps and refreshes its own historical rows.

Partial failures are reported as aggregate counts but remain non-fatal — the
next run picks them up. Script exits non-zero only if every fetch failed. Output
never names a security or currency pair from the user's ledger.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from findash_core.currencies import canonical_currency, yahoo_price_to_minor
from findash_core.db import connect_db
from findash_core.dividends import (
    DividendSeries,
    event_minor_fraction,
    fetch_many,
    fetch_yahoo_dividend_series,
)

YAHOO_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; findash/1.0)"}
REQUEST_DELAY_S = 0.25
RETRY_SLEEP_S = 2.0
DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "finance.db"
VALID_RANGES = ["1mo", "3mo", "6mo", "1y", "2y", "3y", "5y", "max"]


def history_range_for(earliest: Optional[str], *, minimum: str = "3y") -> str:
    """Choose a Yahoo range that actually reaches the earliest required date."""

    if not earliest:
        return minimum
    age_days = (
        datetime.now(timezone.utc).date() - datetime.fromisoformat(earliest).date()
    ).days
    if age_days > 5 * 366:
        return "max"
    if age_days > 3 * 366:
        return "5y"
    return minimum


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


def _fetch_yahoo_history(
    symbol: str, range_str: str, timeout: int = 20
) -> Optional[tuple[list[tuple[str, float]], Optional[str]]]:
    """Return price rows plus Yahoo's quote currency. One retry on 429."""
    encoded_symbol = urllib.parse.quote(symbol, safe="-._^=")
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{encoded_symbol}"
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
            currency = (result.get("meta") or {}).get("currency")
            return out, currency
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt == 0:
                time.sleep(RETRY_SLEEP_S)
                continue
            return None
        except (
            urllib.error.URLError,
            IndexError,
            KeyError,
            TypeError,
            ValueError,
            TimeoutError,
        ):
            return None
    return None


def fetch_yahoo_history(
    symbol: str, range_str: str, timeout: int = 20
) -> Optional[list[tuple[str, float]]]:
    """List of (ISO date, close) tuples, or None on failure."""

    result = _fetch_yahoo_history(symbol, range_str, timeout)
    return None if result is None else result[0]


def fetch_yahoo_history_with_currency(
    symbol: str, range_str: str, timeout: int = 20
) -> Optional[tuple[list[tuple[str, float]], Optional[str]]]:
    """Price history with Yahoo's quote-unit code (for example GBp or ILA)."""

    return _fetch_yahoo_history(symbol, range_str, timeout)


def fetch_yahoo_intraday(
    symbol: str, timeout: int = 20
) -> Optional[tuple[list[tuple[int, float]], Optional[str], dict]]:
    """Current-session points (epoch seconds), quote currency, and session bounds.

    Includes pre- and post-market trading so the dashboard can show the
    pre-market series before the regular session opens. One retry on 429.
    """

    encoded_symbol = urllib.parse.quote(symbol, safe="-._^=")
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{encoded_symbol}"
        "?interval=5m&range=1d&includePrePost=true"
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
            out: list[tuple[int, float]] = []
            for ts, close in zip(timestamps, closes):
                if close is None:
                    continue
                out.append((int(ts), float(close)))
            meta = result.get("meta") or {}
            currency = meta.get("currency")
            periods = meta.get("currentTradingPeriod") or {}
            return out, currency, periods
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt == 0:
                time.sleep(RETRY_SLEEP_S)
                continue
            return None
        except (
            urllib.error.URLError,
            IndexError,
            KeyError,
            TypeError,
            ValueError,
            TimeoutError,
        ):
            return None
    return None


def close_to_minor(
    close: float,
    ccy: str,
    yahoo_currency: Optional[str] = None,
) -> int:
    """Normalize one Yahoo close to ``price_minor``.

    Callers fetching Yahoo metadata should always supply ``yahoo_currency``.
    The fallback treats ``ccy`` as the provider quote currency and exists only
    for compatibility with direct callers of this helper.
    """

    return yahoo_price_to_minor(
        close,
        yahoo_currency or ccy,
        expected_currency=ccy,
    )


def _table_exists(cur: sqlite3.Cursor, table: str) -> bool:
    return cur.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def store_security_price_observation(
    cur: sqlite3.Cursor,
    security_id: int,
    price_minor: int,
    currency: str,
) -> int:
    """Append the value seen by this refresh when the current schema supports it."""

    if not _table_exists(cur, "security_price_observations"):
        return 0
    cur.execute(
        """INSERT OR IGNORE INTO security_price_observations(
               security_id,price_minor,currency,source
           ) VALUES(?,?,?,'yahoo')""",
        (security_id, price_minor, canonical_currency(currency)),
    )
    return cur.rowcount


def store_fx_rate_observation(
    cur: sqlite3.Cursor,
    base: str,
    quote: str,
    rate: float,
) -> int:
    """Append the value seen by this refresh when the current schema supports it."""

    if not _table_exists(cur, "fx_rate_observations"):
        return 0
    cur.execute(
        """INSERT OR IGNORE INTO fx_rate_observations(
               base_currency,quote_currency,rate,source
           ) VALUES(?,?,?,'yahoo')""",
        (canonical_currency(base), canonical_currency(quote), rate),
    )
    return cur.rowcount


def securities_to_fetch(cur: sqlite3.Cursor, mode_range: str) -> list[dict]:
    if mode_range in {"3y", "5y", "max"}:
        ids = [r[0] for r in cur.execute(
            """SELECT security_id FROM trades
               UNION SELECT security_id FROM positions
               UNION SELECT security_id FROM canonical_events
                    WHERE event_type='trade' AND status='confirmed'"""
        ).fetchall()]
    else:
        # Position snapshots remain anchors even when only a partial trade history is
        # available. Include their symbols as well as event-derived open positions.
        rows = cur.execute("""
            SELECT security_id FROM (
                SELECT security_id
                FROM trades
                WHERE canonical_event_id IS NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM event_evidence ee
                      WHERE ee.source_event_id=trades.source_event_id
                  )
                GROUP BY account_id, security_id
                HAVING ABS(SUM(CASE WHEN side='buy' THEN shares ELSE -shares END)) > 1e-9
                UNION
                SELECT security_id FROM canonical_events
                WHERE event_type='trade' AND status='confirmed'
                GROUP BY account_id, security_id
                HAVING ABS(SUM(CASE WHEN side='buy' THEN quantity ELSE -quantity END)) > 1e-9
                UNION
                SELECT p.security_id
                FROM positions p
                JOIN (
                    SELECT account_id, security_id, MAX(as_of) AS as_of
                    FROM positions GROUP BY account_id, security_id
                ) latest ON latest.account_id=p.account_id
                         AND latest.security_id=p.security_id
                         AND latest.as_of=p.as_of
                WHERE ABS(p.quantity)>1e-9
            )
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
        earliest_price = cur.execute(
            "SELECT MIN(date) FROM prices WHERE security_id=?", (sid,)
        ).fetchone()[0]
        earliest = cur.execute(
            """SELECT MIN(d) FROM (
                   SELECT MIN(date) AS d FROM trades WHERE security_id=?
                   UNION ALL SELECT MIN(as_of) FROM positions WHERE security_id=?
                   UNION ALL SELECT MIN(occurred_on) FROM canonical_events
                     WHERE event_type='trade' AND status='confirmed' AND security_id=?
               )""",
            (sid, sid, sid),
        ).fetchone()[0]
        if row[0] == "SPY":
            benchmark_start = cur.execute(
                """SELECT MIN(d) FROM (
                       SELECT MIN(b.as_of) AS d
                       FROM balances b JOIN accounts a ON a.id=b.account_id
                       WHERE a.kind='brokerage'
                       UNION ALL
                       SELECT MIN(p.as_of)
                       FROM positions p JOIN accounts a ON a.id=p.account_id
                       WHERE a.kind='brokerage'
                       UNION ALL
                       SELECT MIN(t.date)
                       FROM trades t JOIN accounts a ON a.id=t.account_id
                       WHERE a.kind='brokerage'
                       UNION ALL
                       SELECT MIN(t.date)
                       FROM transactions t JOIN accounts a ON a.id=t.account_id
                       WHERE a.kind='brokerage'
                       UNION ALL
                       SELECT MIN(c.occurred_on)
                       FROM canonical_events c JOIN accounts a ON a.id=c.account_id
                       WHERE a.kind='brokerage' AND c.status='confirmed'
                   )"""
            ).fetchone()[0]
            if benchmark_start and (not earliest or benchmark_start < earliest):
                earliest = benchmark_start
        minimum_history = mode_range if mode_range in {"3y", "5y", "max"} else "3y"
        initial_range = history_range_for(earliest, minimum=minimum_history)
        if row[0] == "SPY" and not earliest_price:
            # A benchmark may need to reach funding flows older than the
            # locally tracked portfolio history.
            initial_range = "max"
        out.append({
            "id": sid,
            "ticker": row[0],
            "currency": row[1],
            "range": (
                initial_range
                if not earliest_price or (earliest and earliest_price > earliest)
                else mode_range
            ),
        })
    return out


def distribution_securities_to_fetch(
    cur: sqlite3.Cursor,
) -> list[tuple[int, str, str]]:
    """Every security whose holding history can support an entitlement model."""

    return [
        (int(row[0]), str(row[1]), canonical_currency(str(row[2])))
        for row in cur.execute(
            """SELECT DISTINCT s.id,s.ticker,s.currency
                 FROM securities s
                WHERE EXISTS (
                          SELECT 1 FROM trades t
                           WHERE t.security_id=s.id
                      )
                   OR EXISTS (
                          SELECT 1 FROM positions p
                           WHERE p.security_id=s.id
                      )
                   OR EXISTS (
                          SELECT 1 FROM canonical_events c
                           WHERE c.security_id=s.id
                             AND c.event_type='trade'
                             AND c.status='confirmed'
                      )
                ORDER BY s.id"""
        )
    ]


def store_distribution_series(
    cur: sqlite3.Cursor,
    series_by_security: dict[int, DividendSeries],
) -> int:
    """Replace successful provider series in the exact reference-data cache."""

    written = 0
    for security_id, series in series_by_security.items():
        prepared: list[tuple[object, ...]] = []
        for event in series.events:
            numerator, denominator = event_minor_fraction(event)
            source_key = (
                event.source_key
                or f"{event.event_date}:{numerator}/{denominator}"
            )
            prepared.append(
                (
                    security_id,
                    event.event_date,
                    numerator,
                    denominator,
                    series.currency,
                    source_key,
                )
            )
        if prepared:
            placeholders = ",".join("?" for _ in prepared)
            cur.execute(
                f"""DELETE FROM security_distributions
                     WHERE security_id=? AND source='yahoo'
                       AND source_key NOT IN ({placeholders})""",
                (security_id, *(row[-1] for row in prepared)),
            )
        else:
            # A successful empty response is evidence that old provider rows
            # disappeared. A failed response never reaches this function, so it
            # cannot erase the last known-good cache.
            cur.execute(
                """DELETE FROM security_distributions
                    WHERE security_id=? AND source='yahoo'""",
                (security_id,),
            )
        for values in prepared:
            cur.execute(
                """INSERT INTO security_distributions(
                       security_id,event_date,
                       amount_per_share_minor_numerator,
                       amount_per_share_minor_denominator,currency,
                       source,source_key
                   ) VALUES(?,?,?,?,?,'yahoo',?)
                   ON CONFLICT(security_id,source,source_key) DO UPDATE SET
                       event_date=excluded.event_date,
                       amount_per_share_minor_numerator=
                           excluded.amount_per_share_minor_numerator,
                       amount_per_share_minor_denominator=
                           excluded.amount_per_share_minor_denominator,
                       currency=excluded.currency,
                       observed_at=CURRENT_TIMESTAMP""",
                values,
            )
            written += cur.rowcount
    return written


def store_distribution_refreshes(
    cur: sqlite3.Cursor,
    targets: list[tuple[int, str, str]],
    series_by_security: dict[int, DividendSeries],
) -> None:
    """Persist success/empty/failure without leaking selected symbols."""

    for security_id, _ticker, _currency in targets:
        if security_id not in series_by_security:
            status = "failed"
        elif series_by_security[security_id].events:
            status = "success"
        else:
            status = "empty"
        cur.execute(
            """INSERT INTO security_distribution_refreshes(
                   security_id,source,status
               ) VALUES(?,'yahoo',?)
               ON CONFLICT(security_id,source) DO UPDATE SET
                   status=excluded.status,
                   observed_at=CURRENT_TIMESTAMP""",
            (security_id, status),
        )


def earliest_currency_use(cur: sqlite3.Cursor, currency: str) -> Optional[str]:
    """Earliest dated fact or close that needs this currency converted."""

    row = cur.execute(
        """SELECT MIN(d) FROM (
               SELECT MIN(as_of) AS d FROM balances WHERE currency=?
               UNION ALL SELECT MIN(date) FROM transactions WHERE currency=?
               UNION ALL SELECT MIN(date) FROM trades WHERE currency=?
               UNION ALL SELECT MIN(as_of) FROM positions WHERE currency=?
               UNION ALL SELECT MIN(COALESCE(paid_on,period_end,period_start))
                 FROM payslips WHERE currency=?
               UNION ALL SELECT MIN(occurred_on) FROM canonical_events
                 WHERE currency=? AND status='confirmed'
               UNION ALL SELECT MIN(date) FROM prices WHERE currency=?
           )""",
        (currency,) * 7,
    ).fetchone()
    return str(row[0]) if row and row[0] else None


def fx_range_to_fetch(
    cur: sqlite3.Cursor, base: str, quote: str, mode_range: str
) -> str:
    earliest_use = earliest_currency_use(cur, base)
    earliest_rate = cur.execute(
        """SELECT MIN(date) FROM fx_rates
           WHERE base_currency=? AND quote_currency=?""",
        (base, quote),
    ).fetchone()[0]
    if earliest_use and (not earliest_rate or earliest_rate > earliest_use):
        minimum_history = mode_range if mode_range in {"3y", "5y", "max"} else "3y"
        return history_range_for(earliest_use, minimum=minimum_history)
    return mode_range


def refresh_security(
    cur: sqlite3.Cursor, sid: int, ticker: str, ccy: str, range_str: str
) -> tuple[int, bool]:
    fetched = fetch_yahoo_history_with_currency(yahoo_symbol_for(ticker), range_str)
    if fetched is None:
        return 0, False
    hist, yahoo_currency = fetched
    if not yahoo_currency:
        return 0, False
    try:
        stored_currency = canonical_currency(ccy)
        normalized = [
            (d, close_to_minor(close, ccy, yahoo_currency))
            for d, close in hist
        ]
    except (TypeError, ValueError):
        # A missing/mismatched provider currency is safer to retry than to store
        # a plausible price that is off by 100x.
        return 0, False
    inserted = 0
    for d, close_minor in normalized:
        cur.execute(
            """INSERT INTO prices (security_id, date, close_minor, currency)
               VALUES (?,?,?,?)
               ON CONFLICT(security_id,date) DO UPDATE SET
                   close_minor=excluded.close_minor,
                   currency=excluded.currency""",
            (sid, d, close_minor, stored_currency),
        )
        inserted += cur.rowcount
    if normalized:
        _, current_minor = max(normalized, key=lambda item: item[0])
        store_security_price_observation(
            cur, sid, current_minor, stored_currency
        )
    return inserted, True


def _utc_text(epoch_seconds: int) -> str:
    return (
        datetime.fromtimestamp(epoch_seconds, tz=timezone.utc)
        .strftime("%Y-%m-%d %H:%M:%S")
    )


def store_market_session(cur: sqlite3.Cursor, sid: int, periods: dict) -> bool:
    """Persist the fetched session's pre/regular/post boundaries (UTC)."""

    regular = periods.get("regular") or {}
    if not regular.get("start") or not regular.get("end"):
        return False
    pre = periods.get("pre") or {}
    post = periods.get("post") or {}
    bounds = {
        "date": _utc_text(int(regular["start"]))[:10],
        "pre_start": _utc_text(int(pre["start"])) if pre.get("start") else None,
        "regular_start": _utc_text(int(regular["start"])),
        "regular_end": _utc_text(int(regular["end"])),
        "post_end": _utc_text(int(post["end"])) if post.get("end") else None,
    }
    cur.execute(
        """INSERT INTO meta(key,value) VALUES(?,?)
           ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
        (f"market_session:{sid}", json.dumps(bounds)),
    )
    return True


def refresh_security_intraday(
    cur: sqlite3.Cursor, sid: int, ticker: str, ccy: str
) -> tuple[int, bool]:
    """Backfill today's session observations so the sparkline is complete."""

    if not _table_exists(cur, "security_price_observations"):
        return 0, True
    fetched = fetch_yahoo_intraday(yahoo_symbol_for(ticker))
    if fetched is None:
        return 0, False
    points, yahoo_currency, periods = fetched
    if not yahoo_currency:
        return 0, False
    try:
        stored_currency = canonical_currency(ccy)
        normalized = [
            (ts, close_to_minor(close, ccy, yahoo_currency))
            for ts, close in points
        ]
    except (TypeError, ValueError):
        return 0, False
    written = 0
    for ts, price_minor in normalized:
        cur.execute(
            """INSERT OR IGNORE INTO security_price_observations(
                   security_id,observed_at,price_minor,currency,source
               ) VALUES(?,?,?,?,'yahoo-intraday')""",
            (sid, _utc_text(ts), price_minor, stored_currency),
        )
        written += cur.rowcount
    store_market_session(cur, sid, periods)
    return written, True


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
              WHERE lower(fx_rates.source) != 'document'
            """,
            (d, base, quote, rate),
        )
        if cur.rowcount > 0:
            written += 1
    if hist:
        _, current_rate = max(hist, key=lambda item: item[0])
        store_fx_rate_observation(cur, base, quote, current_rate)
    return written, True


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Refresh prices and FX rates from Yahoo Finance.",
    )
    ap.add_argument(
        "--range", default="1mo", choices=VALID_RANGES,
        help="Time range to fetch (default 1mo for daily top-up; use 3y for backfill).",
    )
    ap.add_argument(
        "--intraday", action="store_true",
        help="Also fetch the current session's intraday series for held securities.",
    )
    ap.add_argument(
        "--intraday-only", action="store_true",
        help="Fetch only the intraday series (skip daily prices, dividends, FX).",
    )
    args = ap.parse_args()

    con = connect_db(DEFAULT_DB)
    cur = con.cursor()

    intraday_targets: list[dict] = []
    if args.intraday or args.intraday_only:
        # Currently-held securities; the benchmark rides along when it is also
        # a real holding, and costs one extra request otherwise.
        intraday_targets = securities_to_fetch(cur, "1mo")

    if args.intraday_only:
        intraday_failures = 0
        intraday_successes = 0
        intraday_rows = 0
        for i, t in enumerate(intraday_targets):
            if i > 0:
                time.sleep(REQUEST_DELAY_S)
            n, ok = refresh_security_intraday(
                cur, t["id"], t["ticker"], t["currency"]
            )
            if ok:
                intraday_successes += 1
            else:
                intraday_failures += 1
            intraday_rows += n
        con.commit()
        con.close()
        print("Market refresh: mode=intraday")
        print(
            "Intraday: "
            f"attempted={len(intraday_targets)} succeeded={intraday_successes} "
            f"failed={intraday_failures} rows_written={intraday_rows}"
        )
        if intraday_failures and intraday_failures == len(intraday_targets):
            sys.exit(1)
        return

    # Benchmarks are reference metadata, not user facts. Create SPY in the refresh
    # write path so the renderer can remain strictly read-only.
    cur.execute(
        """INSERT INTO securities(ticker,name,asset_class,currency)
           VALUES('SPY','SPDR S&P 500 ETF','benchmark','USD')
           ON CONFLICT(ticker) DO NOTHING"""
    )

    targets = securities_to_fetch(cur, args.range)

    price_failures = 0
    price_successes = 0
    total_inserted = 0
    for i, t in enumerate(targets):
        if i > 0:
            time.sleep(REQUEST_DELAY_S)
        n, ok = refresh_security(cur, t["id"], t["ticker"], t["currency"], t["range"])
        if ok:
            price_successes += 1
        else:
            price_failures += 1
        total_inserted += n
    con.commit()

    intraday_failures = 0
    intraday_successes = 0
    intraday_rows = 0
    for t in intraday_targets:
        time.sleep(REQUEST_DELAY_S)
        n, ok = refresh_security_intraday(cur, t["id"], t["ticker"], t["currency"])
        if ok:
            intraday_successes += 1
        else:
            intraday_failures += 1
        intraday_rows += n
    con.commit()

    distribution_targets = distribution_securities_to_fetch(cur)
    distribution_series, distribution_failures = fetch_many(
        fetch_yahoo_dividend_series,
        distribution_targets,
    )
    distribution_written = store_distribution_series(
        cur, distribution_series
    )
    store_distribution_refreshes(
        cur,
        distribution_targets,
        distribution_series,
    )
    distribution_successes = len(distribution_series)
    con.commit()

    fx_failures = 0
    fx_successes = 0
    fx_rows_written = 0
    # Refresh FX for every non-base currency actually present in the data.
    # ILA is TASE agorot — minor units of ILS, not FX.
    raw_fx_currencies = [
        r[0] for r in cur.execute(
            """SELECT DISTINCT currency FROM (
                   SELECT currency FROM securities
                   UNION SELECT currency FROM trades
                   UNION SELECT currency FROM balances
                   UNION SELECT currency FROM transactions
                   UNION SELECT currency FROM positions
                   UNION SELECT currency FROM payslips
                   UNION SELECT currency FROM canonical_events
               ) WHERE currency IS NOT NULL AND currency NOT IN ('ILS', 'ILA')
               ORDER BY currency"""
        ).fetchall()
    ]
    fx_currencies = sorted({
        canonical_currency(ccy)
        for ccy in raw_fx_currencies
        if canonical_currency(ccy) != "ILS"
    })
    fx_pairs = [(ccy, "ILS") for ccy in fx_currencies]
    for base, quote in fx_pairs:
        time.sleep(REQUEST_DELAY_S)
        range_str = fx_range_to_fetch(cur, base, quote, args.range)
        n, ok = refresh_fx(cur, base, quote, range_str)
        if ok:
            fx_successes += 1
        else:
            fx_failures += 1
        fx_rows_written += n
    con.commit()
    con.close()

    print(f"Market refresh: mode={args.range}")
    print(
        "Prices: "
        f"attempted={len(targets)} succeeded={price_successes} "
        f"failed={price_failures} rows_written={total_inserted}"
    )
    if intraday_targets:
        print(
            "Intraday: "
            f"attempted={len(intraday_targets)} succeeded={intraday_successes} "
            f"failed={intraday_failures} rows_written={intraday_rows}"
        )
    print(
        "FX: "
        f"attempted={len(fx_pairs)} succeeded={fx_successes} "
        f"failed={fx_failures} rows_written={fx_rows_written}"
    )
    print(
        "Dividends: "
        f"attempted={len(distribution_targets)} "
        f"succeeded={distribution_successes} "
        f"failed={distribution_failures} "
        f"rows_written={distribution_written}"
    )

    total_attempts = (
        len(targets)
        + len(intraday_targets)
        + len(fx_pairs)
        + len(distribution_targets)
    )
    total_failures = (
        price_failures + intraday_failures + fx_failures + distribution_failures
    )
    if total_failures and total_failures == total_attempts:
        sys.exit(1)


if __name__ == "__main__":
    main()
