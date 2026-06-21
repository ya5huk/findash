#!/usr/bin/env python3
"""Render the finance dashboard from data/finance.db.

Canonical implementation invoked by skills/render-finance-dashboard/SKILL.md.
Fetches live prices/FX from Yahoo Finance, computes ILS-normalized market values,
fills templates/dashboard.html.tpl, and writes output/dashboard.html as a single
self-contained file with all CSS, fonts, and Chart.js inlined.
"""

from __future__ import annotations

import sqlite3
import json
import html
import sys
import urllib.request
import urllib.error
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

# Share Yahoo dividend helpers with the sync skill (which calls yahoo_dividends.py
# as a subprocess). Same data here, just no fork overhead.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from yahoo_dividends import (  # noqa: E402
    fetch_chart as _yd_fetch_chart,
    extract_past as _yd_extract_past,
    project_next as _yd_project_next,
    project_through as _yd_project_through,
)


# ---------- paths ----------

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "finance.db"
TEMPLATE_DIR = ROOT / "templates"
VENDOR_DIR = TEMPLATE_DIR / "vendor"
OUTPUT_PATH = ROOT / "output" / "dashboard.html"
MAX_STORED_PRICE_AGE_DAYS = 7


# Categories that are NOT expenses (transfers, income, refunds, internal movements).
# Negative-amount rows with `category` outside this set count as expenses.
# Canonical source: docs/doc-types.md "Expense vs transfer classification".
EXPENSE_EXCLUDED_CATEGORIES = frozenset({
    "transfer", "card_payment", "savings_deposit", "savings_withdrawal",
    "securities_buy", "check", "withdrawal", "fx", "refund", "interest",
    "salary", "deposit", "dividend", "income", "realized_gain",
    "bank_gift", "bank_credit", "pension_deposit", "study_fund_deposit",
    "government_payment",
})

# Positive-amount categories that count as real income but aren't payslip salary
# (handled separately) and aren't brokerage stocks income (handled separately).
# Refunds, bank credits/gifts, interest credits, government payments, and the
# `income` catch-all (side gigs, tips like Buy Me A Coffee).
OTHER_INCOME_CATEGORIES = frozenset({
    "income", "refund", "bank_gift", "bank_credit", "interest",
    "government_payment",
})

# Israeli capital-gains tax applied to *unrealized* stock gains in the dashboard's
# "stocks income" figure. Realized rows arrive from sync already net of this tax
# plus the brokerage fee (see docs/doc-types.md "Brokerage sell screenshots").
ISRAELI_CAPGAINS_TAX = 0.25


# ---------- formatters ----------

def fmt_money(amount: float, decimals: Optional[int] = None) -> str:
    """Format money in major units. Auto-decimals if not specified (0 for >=10k, 2 otherwise)."""
    if decimals is None:
        decimals = 0 if abs(amount) >= 10000 else 2
    sign = "-" if amount < 0 else ""
    return f"{sign}{abs(amount):,.{decimals}f}"


def money_td(amount: float, currency: str = "ILS", decimals: Optional[int] = None,
             extra_class: str = "") -> str:
    text = fmt_money(amount, decimals)
    cls = "amount num"
    if amount > 0:
        cls += " pos"
    elif amount < 0:
        cls += " neg"
    if extra_class:
        cls += " " + extra_class
    return f'<td class="{cls}">{html.escape(text)}<span class="currency-tag">{html.escape(currency)}</span></td>'


def pct_td(value: Optional[float]) -> str:
    if value is None:
        return '<td class="num muted">—</td>'
    cls = "num"
    if value > 0:
        cls += " pos"
    elif value < 0:
        cls += " neg"
    sign = "+" if value > 0 else ""
    return f'<td class="{cls}">{sign}{value:.2f}%</td>'


def num_td(value, decimals: int = 2) -> str:
    if value is None:
        return '<td class="num muted">—</td>'
    if isinstance(value, (int, float)):
        text = f"{value:,.{decimals}f}"
    else:
        text = str(value)
    return f'<td class="num">{html.escape(text)}</td>'


def h(s) -> str:
    return "" if s is None else html.escape(str(s))


def heb_td(s) -> str:
    return f'<td dir="auto">{h(s)}</td>'


# ---------- Yahoo Finance fetcher (stdlib only) ----------

YAHOO_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; findash/1.0)"}


def yahoo_symbol_for(ticker: str) -> str:
    """Translate our DB ticker to Yahoo's symbol."""
    if ticker.endswith(".LSE"):
        return ticker[:-4] + ".L"
    if ticker.endswith(".CRYPTO"):
        return ticker[:-7] + "-USD"
    if ticker == "BRK.B":
        return "BRK-B"
    return ticker


def fetch_yahoo_quote(symbol: str, timeout: int = 15) -> Optional[dict]:
    """Return {'price': float, 'currency': str} or None on any failure."""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=5d"
    try:
        req = urllib.request.Request(url, headers=YAHOO_HEADERS)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.load(resp)
        meta = data["chart"]["result"][0]["meta"]
        price = float(meta.get("regularMarketPrice") or 0)
        if not price:
            return None
        return {"price": price, "currency": meta.get("currency")}
    except (urllib.error.URLError, urllib.error.HTTPError, KeyError,
            TypeError, ValueError, TimeoutError):
        return None


def fetch_yahoo_history(symbol: str, range_str: str = "2y",
                       interval: str = "1d", timeout: int = 20) -> Optional[list[tuple[str, float]]]:
    """Return list of (ISO date, close) tuples, or None on failure."""
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
           f"?interval={interval}&range={range_str}")
    try:
        req = urllib.request.Request(url, headers=YAHOO_HEADERS)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.load(resp)
        result = data["chart"]["result"][0]
        timestamps = result.get("timestamp") or []
        quotes = result.get("indicators", {}).get("quote") or [{}]
        closes = quotes[0].get("close") or []
        out: list[tuple[str, float]] = []
        for ts, close in zip(timestamps, closes):
            if close is None:
                continue
            d = datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()
            out.append((d, float(close)))
        return out
    except (urllib.error.URLError, urllib.error.HTTPError, KeyError,
            TypeError, ValueError, TimeoutError):
        return None


def price_per_share_in_ils(ticker: str, db_currency: str, yahoo_price: float,
                           fx_usd_ils: float, fx_gbp_ils: float) -> float:
    """Convert Yahoo's regularMarketPrice for a security to ILS per share."""
    if db_currency == "USD":
        return yahoo_price * fx_usd_ils
    if db_currency == "GBP":
        # Yahoo returns LSE prices in pence.
        return (yahoo_price / 100.0) * fx_gbp_ils
    if db_currency == "ILA":
        # Yahoo returns TASE prices in agorot.
        return yahoo_price / 100.0
    if db_currency == "ILS":
        return yahoo_price
    return 0.0


def price_per_share_in_security_ccy(ticker: str, db_currency: str,
                                    yahoo_price: float) -> float:
    """Per-share price in the security's quoted currency unit (USD/GBP/ILS major)."""
    if db_currency in ("USD", "ILS"):
        return yahoo_price
    if db_currency == "GBP":
        return yahoo_price / 100.0
    if db_currency == "ILA":
        # Convention: ILA prices are in agorot; the "major" display is ILS.
        return yahoo_price / 100.0
    return yahoo_price


# ---------- main ----------

def main() -> int:
    if not DB.exists():
        print(f"ERROR: {DB} not found", file=sys.stderr)
        return 1

    for vf in ("chart.umd.min.js", "chartjs-adapter-date-fns.bundle.min.js", "fonts-inline.css"):
        if not (VENDOR_DIR / vf).exists():
            print(f"ERROR: missing {VENDOR_DIR / vf}", file=sys.stderr)
            print("Run `python3 scripts/bundle-assets.py` once to vendor offline assets.", file=sys.stderr)
            return 1

    today = date.today()
    as_of = today.isoformat()

    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    # ----- 1. Live prices + FX -----

    # NOTE: HAVING must repeat the SUM expression — SQLite resolves `ABS(shares)`
    # against a column lookup before the aggregate, so the alias gives all rows.
    held = cur.execute("""
        SELECT s.id, s.ticker, s.currency, s.asset_class,
               SUM(CASE WHEN t.side='buy' THEN t.shares ELSE -t.shares END) AS shares,
               SUM(CASE WHEN t.side='buy' THEN t.shares*t.price_minor ELSE -t.shares*t.price_minor END)/100.0 AS net_cost,
               COUNT(*) AS n_trades,
               MAX(t.date) AS last_trade,
               MIN(t.date) AS first_trade
        FROM trades t JOIN securities s ON s.id=t.security_id
        GROUP BY s.id
        HAVING ABS(SUM(CASE WHEN t.side='buy' THEN t.shares ELSE -t.shares END)) > 1e-6
        ORDER BY s.ticker
    """).fetchall()
    held = [dict(r) for r in held]

    # Fetch FX first so we can convert.
    fx_quote_usd = fetch_yahoo_quote("USDILS=X")
    fx_quote_gbp = fetch_yahoo_quote("GBPILS=X")
    fx_usd_ils = fx_quote_usd["price"] if fx_quote_usd else None
    fx_gbp_ils = fx_quote_gbp["price"] if fx_quote_gbp else None

    # Fall back to most recent DB rate if Yahoo is unreachable. If both fail,
    # abort — silently using a magic number would corrupt the headline.
    def _fallback_fx(base: str, quote: str) -> float:
        row = cur.execute(
            "SELECT rate FROM fx_rates WHERE base_currency=? AND quote_currency=? "
            "ORDER BY date DESC LIMIT 1",
            (base, quote),
        ).fetchone()
        if not row:
            raise SystemExit(
                f"FX rate missing: {base}->{quote}. Live quote failed and the "
                f"fx_rates table has no rows for this pair. Run "
                f"`python3 scripts/refresh_prices.py --range 1mo` and retry."
            )
        return row["rate"]

    if fx_usd_ils is None:
        fx_usd_ils = _fallback_fx("USD", "ILS")
    if fx_gbp_ils is None:
        fx_gbp_ils = _fallback_fx("GBP", "ILS")

    failed_tickers: list[str] = []
    stored_price_fallbacks: list[dict] = []
    cost_basis_fallback_tickers: list[str] = []
    prices_by_security_id: dict[int, dict] = {}

    for pos in held:
        sym = yahoo_symbol_for(pos["ticker"])
        q = fetch_yahoo_quote(sym)
        if q is None:
            failed_tickers.append(pos["ticker"])
            continue
        prices_by_security_id[pos["id"]] = {
            "yahoo_price": q["price"],
            "yahoo_currency": q["currency"],
            "price_in_ccy": price_per_share_in_security_ccy(pos["ticker"], pos["currency"], q["price"]),
            "price_in_ils": price_per_share_in_ils(
                pos["ticker"], pos["currency"], q["price"], fx_usd_ils, fx_gbp_ils
            ),
        }

    # Persist prices + FX so we accumulate a history.
    for sid, p in prices_by_security_id.items():
        sec = cur.execute("SELECT currency FROM securities WHERE id=?", (sid,)).fetchone()
        # close_minor convention: integer minor units matching the security's price_minor in trades.
        # For USD: cents; GBP: pence; ILA: agorot.
        ccy = sec["currency"]
        if ccy == "USD":
            close_minor = round(p["yahoo_price"] * 100)
        else:
            # For GBP (yahoo pence) and ILA (yahoo agorot), the integer Yahoo value is already minor.
            close_minor = round(p["yahoo_price"])
        cur.execute(
            "INSERT OR REPLACE INTO prices (security_id, date, close_minor, currency) VALUES (?,?,?,?)",
            (sid, as_of, close_minor, ccy),
        )
    cur.execute(
        "INSERT OR REPLACE INTO fx_rates (date, base_currency, quote_currency, rate) VALUES (?,?,?,?)",
        (as_of, "USD", "ILS", fx_usd_ils),
    )
    cur.execute(
        "INSERT OR REPLACE INTO fx_rates (date, base_currency, quote_currency, rate) VALUES (?,?,?,?)",
        (as_of, "GBP", "ILS", fx_gbp_ils),
    )
    con.commit()

    # ----- 1b. Historical prices + FX (for the over-time charts) -----

    # SPY is the benchmark for stocks-vs-SPY. Create the security row if absent.
    spy_row = cur.execute(
        "SELECT id, currency FROM securities WHERE ticker = 'SPY'"
    ).fetchone()
    if spy_row:
        spy_id = spy_row["id"]
        spy_ccy = spy_row["currency"]
    else:
        cur.execute(
            "INSERT INTO securities (ticker, name, asset_class, currency) VALUES (?,?,?,?)",
            ("SPY", "SPDR S&P 500 ETF", "benchmark", "USD"),
        )
        spy_id = cur.lastrowid
        spy_ccy = "USD"

    # Windowed P/L needs boundary prices for positions that may have been sold
    # since the window opened, not only currently-held securities.
    history_targets: dict[int, tuple[str, str]] = {
        r["id"]: (r["ticker"], r["currency"])
        for r in cur.execute(
            """SELECT DISTINCT s.id, s.ticker, s.currency
               FROM trades t JOIN securities s ON s.id = t.security_id"""
        ).fetchall()
    }
    history_targets.setdefault(spy_id, ("SPY", spy_ccy))

    for sid, (ticker, ccy) in history_targets.items():
        hist = fetch_yahoo_history(yahoo_symbol_for(ticker))
        if not hist:
            continue
        for d, close in hist:
            if ccy == "USD":
                close_minor = round(close * 100)
            else:
                close_minor = round(close)
            cur.execute(
                "INSERT OR IGNORE INTO prices (security_id, date, close_minor, currency) VALUES (?,?,?,?)",
                (sid, d, close_minor, ccy),
            )

    for base in ("USD", "GBP"):
        hist = fetch_yahoo_history(f"{base}ILS=X")
        if not hist:
            continue
        for d, rate in hist:
            cur.execute(
                "INSERT OR IGNORE INTO fx_rates (date, base_currency, quote_currency, rate) VALUES (?,?,?,?)",
                (d, base, "ILS", rate),
            )
    con.commit()

    def _stored_price_to_major(close_minor: int, ccy: str) -> float:
        if ccy in ("USD", "GBP", "ILS", "ILA"):
            return close_minor / 100.0
        return close_minor

    def _stored_price_fallback(pos: dict) -> Optional[dict]:
        row = cur.execute(
            """SELECT date, close_minor, currency FROM prices
               WHERE security_id = ? AND date <= ?
               ORDER BY date DESC LIMIT 1""",
            (pos["id"], as_of),
        ).fetchone()
        if not row:
            return None
        price_date = date.fromisoformat(row["date"])
        age_days = (today - price_date).days
        if age_days < 0 or age_days > MAX_STORED_PRICE_AGE_DAYS:
            return None
        ccy = row["currency"]
        price_in_ccy = _stored_price_to_major(row["close_minor"], ccy)
        if ccy == "USD":
            price_in_ils = price_in_ccy * fx_usd_ils
        elif ccy == "GBP":
            price_in_ils = price_in_ccy * fx_gbp_ils
        elif ccy in ("ILS", "ILA"):
            price_in_ils = price_in_ccy
        else:
            return None
        return {
            "yahoo_price": None,
            "yahoo_currency": ccy,
            "price_in_ccy": price_in_ccy,
            "price_in_ils": price_in_ils,
            "source": "stored",
            "price_date": row["date"],
            "age_days": age_days,
        }

    for pos in held:
        if pos["id"] in prices_by_security_id:
            prices_by_security_id[pos["id"]]["source"] = "live"
            prices_by_security_id[pos["id"]]["price_date"] = as_of
            prices_by_security_id[pos["id"]]["age_days"] = 0
            continue
        stored = _stored_price_fallback(pos)
        if stored:
            prices_by_security_id[pos["id"]] = stored
            stored_price_fallbacks.append({
                "ticker": pos["ticker"],
                "date": stored["price_date"],
                "age_days": stored["age_days"],
            })
        else:
            cost_basis_fallback_tickers.append(pos["ticker"])

    # ----- 2. Balances at today (with NULL-component fix) -----

    def latest_fx(base: str, quote: str, on_or_before: str) -> float:
        # Prefer the most recent rate on or before the requested date.
        row = cur.execute(
            """SELECT rate FROM fx_rates
               WHERE base_currency=? AND quote_currency=? AND date<=?
               ORDER BY date DESC LIMIT 1""",
            (base, quote, on_or_before),
        ).fetchone()
        if row:
            return row["rate"]
        # Pre-cache dates: best-effort use the earliest rate we have. Better than 1.0
        # for a chart point that pre-dates our coverage, but still bounded.
        row = cur.execute(
            """SELECT rate FROM fx_rates
               WHERE base_currency=? AND quote_currency=?
               ORDER BY date ASC LIMIT 1""",
            (base, quote),
        ).fetchone()
        if row:
            return row["rate"]
        # No rate at all for this pair — never silently treat 1 base = 1 quote.
        raise SystemExit(
            f"FX rate missing: no rows for {base}->{quote} in fx_rates. "
            f"Run `python3 scripts/refresh_prices.py --range 3y` to backfill, "
            f"or remove the holding/transaction in that currency."
        )

    def balances_at(as_of_str: str) -> list[dict]:
        rows = cur.execute(
            """WITH latest AS (
                  SELECT account_id,
                         COALESCE(component, '__null__') AS comp_key,
                         MAX(as_of) AS as_of
                  FROM balances
                  WHERE as_of <= ?
                  GROUP BY account_id, COALESCE(component, '__null__')
               )
               SELECT b.* FROM balances b
               JOIN latest l
                 ON l.account_id = b.account_id
                AND l.comp_key = COALESCE(b.component, '__null__')
                AND l.as_of = b.as_of""",
            (as_of_str,),
        ).fetchall()
        return [dict(r) for r in rows]

    def cost_basis_ils_at(on_date: str, security_id: Optional[int] = None) -> float:
        """Net cost (buys - sells, excluding fees) in ILS for all trades through
        on_date, with each trade leg converted using FX *at that trade's date*.
        If security_id is given, restrict to that one position; otherwise sum
        across the whole brokerage. Matches the existing held.net_cost semantic
        (price*shares only — fees aren't counted) but in honest historical ILS."""
        if security_id is None:
            rows = cur.execute(
                """SELECT date, side, shares, price_minor, currency
                   FROM trades WHERE date <= ?""",
                (on_date,),
            ).fetchall()
        else:
            rows = cur.execute(
                """SELECT date, side, shares, price_minor, currency
                   FROM trades WHERE date <= ? AND security_id = ?""",
                (on_date, security_id),
            ).fetchall()
        total = 0.0
        for r in rows:
            leg_native = r["shares"] * r["price_minor"] / 100.0
            sgn = 1.0 if r["side"] == "buy" else -1.0
            if r["currency"] in ("ILS", "ILA"):
                total += sgn * leg_native
            else:
                total += sgn * leg_native * latest_fx(r["currency"], "ILS", r["date"])
        return total

    def brokerage_usd_deposits_at(as_of_str: str) -> float:
        if primary_brokerage_id is None:
            return 0.0
        r = cur.execute(
            """SELECT COALESCE(SUM(amount_minor),0) AS t FROM transactions
               WHERE account_id=? AND category IN ('deposit','transfer') AND amount_minor>0 AND date<=?""",
            (primary_brokerage_id, as_of_str),
        ).fetchone()
        return r["t"] / 100.0

    def snapshot_positions_at(account_id: int, as_of_str: str) -> list[dict]:
        """Latest-on-or-before position snapshot for a snapshot-based brokerage
        (e.g. IBKR). Returns the rows at the single most-recent as_of <= the
        requested date, joined to securities, ordered by reported market value.
        Empty when the account has no snapshot at/before that date — so dates
        before the first snapshot contribute nothing (the account didn't exist
        yet) and later dates carry the last snapshot forward, matching the
        `balances_at` "latest snapshot ≤ date" semantics."""
        snap = cur.execute(
            "SELECT MAX(as_of) AS d FROM positions WHERE account_id=? AND as_of<=?",
            (account_id, as_of_str),
        ).fetchone()
        snap_date = snap["d"] if snap else None
        if not snap_date:
            return []
        rows = cur.execute(
            """SELECT p.*, s.ticker AS ticker, s.name AS sec_name
               FROM positions p JOIN securities s ON s.id = p.security_id
               WHERE p.account_id=? AND p.as_of=?
               ORDER BY p.market_value_minor DESC""",
            (account_id, snap_date),
        ).fetchall()
        return [dict(r) for r in rows]

    def brokerage_cash_at(as_of_str: str) -> dict[str, dict]:
        """Per-currency cash for the trade-fed brokerage (primary_brokerage_id) at
        as_of_str, in each currency's major units. Returns {} when there is no
        trade-fed brokerage yet.

        Anchors on the latest `cash_<ccy>` balance snapshot ≤ as_of_str (forward
        derivation); if no such snapshot exists, falls back to the earliest one
        and back-derives. Adjusts the anchor by the net of transactions on the
        brokerage in that currency minus the cash-leg of buy trades on the
        brokerage in that currency. Sells already write their proceeds into
        `transactions` per `docs/doc-types.md` ("Brokerage sell screenshots"), so
        they are NOT derived from `trades` here.
        """
        if primary_brokerage_id is None:
            return {}
        out: dict[str, dict] = {}
        for ccy in ("USD", "GBP", "ILS"):
            component = f"cash_{ccy.lower()}"
            snap = cur.execute(
                """SELECT amount_minor, as_of FROM balances
                   WHERE account_id=? AND component=? AND as_of<=?
                   ORDER BY as_of DESC LIMIT 1""",
                (primary_brokerage_id, component, as_of_str),
            ).fetchone()
            forward = True
            if not snap:
                snap = cur.execute(
                    """SELECT amount_minor, as_of FROM balances
                       WHERE account_id=? AND component=?
                       ORDER BY as_of ASC LIMIT 1""",
                    (primary_brokerage_id, component),
                ).fetchone()
                forward = False
            if snap:
                baseline = snap["amount_minor"] / 100.0
                anchor = snap["as_of"]
                snap_for_disp: Optional[str] = anchor
            else:
                # No snapshot ever for this currency. Two cases:
                #   (a) genuinely zero — never held this currency, no activity → baseline 0 is correct.
                #   (b) activity exists (deposits/buys) but no snapshot yet → baseline 0 silently
                #       undercounts. Fail loud so the user uploads a snapshot.
                has_activity = cur.execute(
                    "SELECT 1 FROM transactions WHERE account_id=? AND currency=? "
                    "UNION SELECT 1 FROM trades WHERE account_id=? AND currency=? LIMIT 1",
                    (primary_brokerage_id, ccy, primary_brokerage_id, ccy),
                ).fetchone()
                if has_activity:
                    broker_name = accounts[primary_brokerage_id]["name"]
                    raise SystemExit(
                        f"{broker_name} {ccy} cash: activity exists in transactions/trades but "
                        f"no balances snapshot has been ingested. Upload a brokerage balance "
                        f"screenshot for {ccy} so the cash baseline can be anchored."
                    )
                baseline = 0.0
                anchor = "0000-01-01"
                snap_for_disp = None
                forward = True  # derive forward from epoch (no-op since no activity)
            if forward:
                lo, hi, sign = anchor, as_of_str, 1.0
            else:
                lo, hi, sign = as_of_str, anchor, -1.0
            txn_sum = cur.execute(
                """SELECT COALESCE(SUM(amount_minor),0)/100.0 AS t FROM transactions
                   WHERE account_id=? AND currency=? AND date>? AND date<=?""",
                (primary_brokerage_id, ccy, lo, hi),
            ).fetchone()["t"]
            buy_sum = cur.execute(
                """SELECT COALESCE(SUM(shares*price_minor + fees_minor),0)/100.0 AS t
                   FROM trades
                   WHERE account_id=? AND side='buy' AND currency=?
                     AND date>? AND date<=?""",
                (primary_brokerage_id, ccy, lo, hi),
            ).fetchone()["t"]
            out[ccy] = {
                "amount": baseline + sign * (txn_sum - buy_sum),
                "snapshot_as_of": snap_for_disp,
            }
        return out

    accounts = {r["id"]: dict(r) for r in cur.execute("SELECT * FROM accounts ORDER BY id")}

    def account_active_on(account: dict, as_of_str: str) -> bool:
        opened_on = account.get("opened_on")
        closed_on = account.get("closed_on")
        return (not opened_on or opened_on <= as_of_str) and (not closed_on or as_of_str < closed_on)

    # Data-driven brokerage valuation routing (replaces the old hardcoded account 7).
    # An account is TRADE-FED when it has any `trades` rows — positions are derived
    # from the event ledger, giving true cost basis + the stocks-vs-S&P benchmark.
    # A brokerage with only `positions` rows and no trades is SNAPSHOT-FED — holdings
    # read as-reported from a live API (e.g. IBKR). The two sets are disjoint, so no
    # holding is ever valued twice (see the `positions` comment in init-db.sql).
    trade_fed_ids = {r["account_id"] for r in cur.execute(
        "SELECT DISTINCT account_id FROM trades"
    ).fetchall()}
    trade_fed_brokerage_ids = [aid for aid, a in accounts.items()
                               if a["kind"] == "brokerage" and aid in trade_fed_ids]
    # The trade-fed brokerage whose per-account cash / deposits / label the sections
    # below use (was the literal account 7). None when there's no trade history yet.
    primary_brokerage_id = trade_fed_brokerage_ids[0] if trade_fed_brokerage_ids else None

    # Snapshot-fed brokerages (e.g. IBKR mapped onto its own account): brokerage
    # accounts with a positions snapshot and NO trades. Disjoint from the trade-fed
    # set, so they never double-count. Additive + inert when empty. Guard on the
    # `positions` table existing so a DB that predates it renders unchanged.
    positions_table_exists = bool(cur.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='positions'"
    ).fetchone())
    snapshot_fed_ids = []
    if positions_table_exists:
        snapshot_fed_ids = [
            a["id"] for a in accounts.values()
            if a["kind"] == "brokerage" and a["id"] not in trade_fed_ids
            and account_active_on(a, as_of)
            and cur.execute(
                "SELECT 1 FROM positions WHERE account_id=? LIMIT 1", (a["id"],)
            ).fetchone()
        ]

    # Hapoalim checking moved from account 2 to account 1. Historical manual cash
    # snapshots and official bank snapshots can overlap, so net worth should use
    # the freshest valid balance from the continuity pair, not both.
    checking_continuity_groups = ((1, 2),)
    checking_continuity_ids = {account_id for group in checking_continuity_groups for account_id in group}

    def net_worth_balances_at(as_of_str: str) -> list[dict]:
        rows = balances_at(as_of_str)
        selected: list[dict] = []

        for group in checking_continuity_groups:
            priority = {account_id: idx for idx, account_id in enumerate(group)}
            candidates = [
                r for r in rows
                if r["account_id"] in group
                and r["component"] is None
                and account_active_on(accounts[r["account_id"]], as_of_str)
            ]
            if candidates:
                row = max(
                    candidates,
                    key=lambda r: (r["as_of"], -priority.get(r["account_id"], 999)),
                )
                placeholders = ",".join("?" for _ in group)
                delta_minor = cur.execute(
                    f"""SELECT COALESCE(SUM(amount_minor),0) AS delta
                        FROM transactions
                        WHERE account_id IN ({placeholders})
                          AND currency = ?
                          AND date > ?
                          AND date <= ?""",
                    (*group, row["currency"], row["as_of"], as_of_str),
                ).fetchone()["delta"]
                adjusted = dict(row)
                adjusted["amount_minor"] = row["amount_minor"] + delta_minor
                selected.append(adjusted)

        for r in rows:
            if r["account_id"] in checking_continuity_ids and r["component"] is None:
                continue
            if not account_active_on(accounts[r["account_id"]], as_of_str):
                continue
            selected.append(r)

        return selected

    # ----- 3. Brokerage market value (live) + cost basis -----

    brokerage_market_value_ils = 0.0
    # True ILS cost basis: walk each trade and convert at the FX rate on its own date,
    # not today's. Today's FX makes "unrealized P&L in ILS" move whenever the shekel
    # moves, hiding what's actually a stock-price vs FX-impact decomposition.
    brokerage_cost_basis_ils = cost_basis_ils_at(as_of)
    for pos in held:
        shares = pos["shares"]
        p = prices_by_security_id.get(pos["id"])
        if p:
            brokerage_market_value_ils += shares * p["price_in_ils"]
        else:
            # Per-position fallback: this ticker's market value is unavailable,
            # so show its (true historical) cost basis in its place. Footnoted below.
            brokerage_market_value_ils += cost_basis_ils_at(as_of, pos["id"])

    brokerage_usd_deposits = brokerage_usd_deposits_at(as_of)

    # ----- 4. Net worth -----

    nw_ils = 0.0
    latest = balances_at(as_of)
    for r in net_worth_balances_at(as_of):
        if r["account_id"] == primary_brokerage_id:
            continue  # brokerage cash is derived via brokerage_cash_at to include post-snapshot flows
        amt = r["amount_minor"] / 100.0
        if r["currency"] == "ILS":
            nw_ils += amt
        else:
            nw_ils += amt * latest_fx(r["currency"], "ILS", as_of)
    nw_ils += brokerage_market_value_ils
    brokerage_cash_now = brokerage_cash_at(as_of)
    for ccy, info in brokerage_cash_now.items():
        amt = info["amount"]
        if abs(amt) < 0.01:
            continue
        nw_ils += amt if ccy == "ILS" else amt * latest_fx(ccy, "ILS", as_of)

    # Snapshot brokerages (IBKR): value positions directly from the reported
    # market value (no price lookup). IBKR cash is already in nw via the generic
    # balance loop above (the trade-fed brokerage is the only one it skips), so only positions
    # are added here. Capture per-account once so net worth and the Overview
    # table use identical numbers. Inert when snapshot_fed_ids is empty.
    snapshot_values: dict[int, dict] = {}
    for aid in snapshot_fed_ids:
        acct_pos = snapshot_positions_at(aid, as_of)
        pos_ils = 0.0
        for p in acct_pos:
            if p["market_value_minor"] is None:
                continue
            native = p["market_value_minor"] / 100.0
            ccy = p["currency"]
            pos_ils += native if ccy in ("ILS", "ILA") else native * latest_fx(ccy, "ILS", as_of)
        cash_ils = 0.0
        cash_parts: list[str] = []
        for b in latest:
            if b["account_id"] != aid or not b["component"]:
                continue
            if not str(b["component"]).startswith("cash_"):
                continue
            amt = b["amount_minor"] / 100.0
            if abs(amt) < 0.01:
                continue
            cash_ils += amt if b["currency"] == "ILS" else amt * latest_fx(b["currency"], "ILS", as_of)
            cash_parts.append(f"{fmt_money(amt, 2)} {b['currency']}")
        snapshot_values[aid] = {
            "pos_rows": acct_pos,
            "pos_ils": pos_ils,
            "cash_ils": cash_ils,
            "cash_parts": cash_parts,
            "snap_date": acct_pos[0]["as_of"] if acct_pos else None,
        }
        nw_ils += pos_ils  # IBKR cash already counted by the generic balance loop

    net_worth_str = f"{nw_ils:,.0f} ILS"

    # ----- 5. Cash table (open, excluding closed accounts) -----

    latest_by_key = {(b["account_id"], b["component"]): b for b in latest}
    # ILS cash only — foreign-currency checking/savings accounts surface in Investments
    # as uninvested-currency reserves rather than here.
    cash_ids = [a["id"] for a in accounts.values()
                if a["kind"] in ("checking", "savings", "cash")
                and a["currency"] == "ILS"
                and not a["liquidity_date"]
                and not a.get("closed_on")]
    cash_rows = []
    cash_total = 0.0
    for aid in cash_ids:
        acc = accounts[aid]
        b = latest_by_key.get((aid, None))
        if not b:
            continue
        amount = b["amount_minor"] / 100.0
        if amount == 0:
            continue
        ils = amount if b["currency"] == "ILS" else amount * latest_fx(b["currency"], "ILS", b["as_of"])
        cash_total += ils
        cash_rows.append(
            f"<tr>{heb_td(acc['name'])}"
            f'<td class="muted">{h(acc["institution"])}</td>'
            f"{money_td(amount, b['currency'])}"
            f'<td class="muted num">{h(b["as_of"])}</td></tr>'
        )

    # Trade-fed brokerage cash — multi-currency, derived from latest snapshot + post-snapshot flows
    brokerage_cash_parts: list[str] = []
    brokerage_cash_ils_sum = 0.0
    brokerage_cash_snap_dates: list[str] = []
    for ccy in ("USD", "GBP", "ILS"):
        info = brokerage_cash_now.get(ccy, {})
        amt = info.get("amount", 0.0)
        if abs(amt) < 0.01:
            continue
        brokerage_cash_parts.append(f"{fmt_money(amt, 2)} {ccy}")
        brokerage_cash_ils_sum += amt if ccy == "ILS" else amt * latest_fx(ccy, "ILS", as_of)
        if info.get("snapshot_as_of"):
            brokerage_cash_snap_dates.append(info["snapshot_as_of"])
    if brokerage_cash_parts:
        cash_total += brokerage_cash_ils_sum
        snap_disp = max(brokerage_cash_snap_dates) if brokerage_cash_snap_dates else "no snapshot"
        cash_rows.append(
            f"<tr>{heb_td(accounts[primary_brokerage_id]['name'])}"
            f'<td class="muted">{h(accounts[primary_brokerage_id]["institution"])} · {h(" + ".join(brokerage_cash_parts))}</td>'
            f"{money_td(brokerage_cash_ils_sum, 'ILS')}"
            f'<td class="muted num">{h(snap_disp)}</td></tr>'
        )

    # ----- 6. Locked rows -----

    locked_ids = [a["id"] for a in accounts.values()
                  if a["kind"] in ("pension", "study_fund")
                  or (a["kind"] == "savings" and a["liquidity_date"])
                  and not a.get("closed_on")]
    locked_rows = []
    locked_total = 0.0
    for aid in locked_ids:
        acc = accounts[aid]
        comps = [r for r in latest if r["account_id"] == aid and r["component"] is not None]
        plain = latest_by_key.get((aid, None))
        if comps:
            total = sum(c["amount_minor"] for c in comps) / 100.0
            as_of_row = max(c["as_of"] for c in comps)
            track_extra = f' <span class="muted">· {h(acc["investment_track"])}</span>' if acc["investment_track"] else ""
            liq_extra = f' <span class="muted">· liq {h(acc["liquidity_date"])}</span>' if acc["liquidity_date"] else ""
            locked_rows.append(
                f"<tr>{heb_td(acc['name'])}"
                f'<td class="muted">{h(acc["institution"])}{track_extra}{liq_extra}</td>'
                f"{money_td(total, 'ILS')}"
                f'<td class="muted num">{h(as_of_row)}</td></tr>'
            )
            comp_labels = {
                "tagmul_employee": "tagmul (employee)",
                "tagmul_employer": "tagmul (employer)",
                "pitsuyim": "pitsuyim (severance)",
            }
            for c in sorted(comps, key=lambda x: x["component"]):
                label = comp_labels.get(c["component"], c["component"])
                amt = c["amount_minor"] / 100.0
                locked_rows.append(
                    f'<tr><td style="padding-left:32px" class="muted">↳ {h(label)}</td>'
                    f"<td></td>{money_td(amt, c['currency'])}<td></td></tr>"
                )
            locked_total += total
        elif plain:
            amount = plain["amount_minor"] / 100.0
            ils = amount if plain["currency"] == "ILS" else amount * latest_fx(plain["currency"], "ILS", plain["as_of"])
            locked_total += ils
            liq_extra = f' <span class="muted">· liq {h(acc["liquidity_date"])}</span>' if acc["liquidity_date"] else ""
            locked_rows.append(
                f"<tr>{heb_td(acc['name'])}"
                f'<td class="muted">{h(acc["institution"])}{liq_extra}</td>'
                f"{money_td(amount, plain['currency'])}"
                f'<td class="muted num">{h(plain["as_of"])}</td></tr>'
            )

    # ----- 7. Investment rows (4-col, for unified Overview table) -----

    inv_rows = []
    inv_total_ils = 0.0

    # Hapoalim Capital Market (account 8) — balance snapshot
    acc8 = accounts.get(8)
    b8 = latest_by_key.get((8, None))
    if acc8 and b8 and not acc8.get("closed_on"):
        amount = b8["amount_minor"] / 100.0
        inv_total_ils += amount
        inv_rows.append(
            f"<tr>{heb_td(acc8['name'])}"
            f'<td class="muted">{h(acc8["institution"])}</td>'
            f"{money_td(amount, b8['currency'])}"
            f'<td class="muted num">{h(b8["as_of"])}</td></tr>'
        )

    # Trade-fed brokerage (primary_brokerage_id) — live market value
    acc_brokerage = accounts.get(primary_brokerage_id) if primary_brokerage_id is not None else None
    if acc_brokerage and not acc_brokerage.get("closed_on"):
        cost_basis_ils = brokerage_cost_basis_ils
        mv_ils = brokerage_market_value_ils
        pnl_ils = mv_ils - cost_basis_ils
        pnl_pct = (pnl_ils / cost_basis_ils * 100.0) if cost_basis_ils else None
        # Pure USD framing: USD deposits as cost basis, MV in USD via current FX.
        # Different from pnl_ils because the ILS cost basis used historical FX rates per deposit.
        mv_usd = (mv_ils / fx_usd_ils) if fx_usd_ils else None
        pnl_usd = (mv_usd - brokerage_usd_deposits) if (mv_usd is not None and brokerage_usd_deposits) else None
        pnl_pct_usd = (pnl_usd / brokerage_usd_deposits * 100.0) if (pnl_usd is not None and brokerage_usd_deposits) else None
        inv_total_ils += mv_ils
        pnl_sign = "+" if pnl_ils >= 0 else "−"
        pnl_pct_str = f"{pnl_sign}{abs(pnl_pct):.1f}%" if pnl_pct is not None else "—"
        if pnl_usd is not None and pnl_pct_usd is not None:
            usd_sign = "+" if pnl_usd >= 0 else "−"
            pnl_usd_str = f'{usd_sign}${abs(pnl_usd):,.0f} ({usd_sign}{abs(pnl_pct_usd):.1f}%)'
        else:
            pnl_usd_str = "—"
        inv_rows.append(
            f"<tr>{heb_td(acc_brokerage['name'])}"
            f'<td class="muted">{h(acc_brokerage["institution"])}'
            f' <span class="muted">· {brokerage_usd_deposits:,.0f} USD deposited'
            f' · P/L {h(pnl_usd_str)} · {h(pnl_sign + format(abs(pnl_ils), ",.0f"))} ILS ({h(pnl_pct_str)})</span></td>'
            f"{money_td(mv_ils, 'ILS')}"
            f'<td class="muted num">live</td></tr>'
        )

    # Foreign-currency cash falls back to SUM(transactions) when no balance snapshot exists.
    fx_cash_accounts = [a for a in accounts.values()
                        if a["kind"] in ("checking", "savings")
                        and a["currency"] != "ILS"
                        and not a.get("closed_on")
                        and not a.get("liquidity_date")]
    for acc in sorted(fx_cash_accounts, key=lambda a: (a["currency"], a["name"])):
        b = latest_by_key.get((acc["id"], None))
        if b:
            balance = b["amount_minor"] / 100.0
            as_of_row = b["as_of"]
        else:
            r = cur.execute(
                "SELECT COALESCE(SUM(amount_minor), 0) AS t FROM transactions WHERE account_id=?",
                (acc["id"],),
            ).fetchone()
            balance = r["t"] / 100.0
            as_of_row = as_of
        if balance == 0:
            continue
        ils_equiv = balance * latest_fx(acc["currency"], "ILS", as_of_row)
        inv_total_ils += ils_equiv
        inv_rows.append(
            f"<tr>{heb_td(acc['name'])}"
            f'<td class="muted">{h(acc["institution"])} '
            f'<span class="muted">· uninvested {h(acc["currency"])} reserve</span></td>'
            f"{money_td(balance, acc['currency'])}"
            f'<td class="muted num">{h(as_of_row)}</td></tr>'
        )

    # Snapshot brokerages (IBKR) — positions valued from reported market value
    # plus uninvested cash, one row per account. IBKR cash is NOT in the cash
    # section (brokerage accounts are excluded there), so it rides this row;
    # net worth counts the same cash via the generic balance loop, keeping the
    # Overview grand total equal to the headline. Additive; inert without IBKR.
    for aid in snapshot_fed_ids:
        acc = accounts[aid]
        v = snapshot_values[aid]
        total_ils = v["pos_ils"] + v["cash_ils"]
        if abs(total_ils) < 0.01:
            continue
        inv_total_ils += total_ils
        cash_note = f' · {h(" + ".join(v["cash_parts"]))} cash' if v["cash_parts"] else ""
        inv_rows.append(
            f"<tr>{heb_td(acc['name'])}"
            f'<td class="muted">{h(acc["institution"])}{cash_note}</td>'
            f"{money_td(total_ils, 'ILS')}"
            f'<td class="muted num">{h(v["snap_date"] or as_of)}</td></tr>'
        )

    # ----- 7b. Unified Overview table -----

    grand_total_ils = cash_total + locked_total + inv_total_ils
    overview_rows: list[str] = []

    overview_rows.append('<tr class="category-row"><td colspan="4">Cash</td></tr>')
    overview_rows.extend(cash_rows)
    overview_rows.append(
        f'<tr class="subtotal-row"><td>Total cash</td><td></td>'
        f"{money_td(cash_total, 'ILS')}<td></td></tr>"
    )

    overview_rows.append('<tr class="category-row"><td colspan="4">Locked</td></tr>')
    overview_rows.extend(locked_rows)
    overview_rows.append(
        f'<tr class="subtotal-row"><td>Total locked</td><td></td>'
        f"{money_td(locked_total, 'ILS')}<td></td></tr>"
    )

    overview_rows.append('<tr class="category-row"><td colspan="4">Investments</td></tr>')
    overview_rows.extend(inv_rows)
    overview_rows.append(
        f'<tr class="subtotal-row"><td>Total investments</td><td></td>'
        f"{money_td(inv_total_ils, 'ILS')}<td></td></tr>"
    )

    overview_rows.append(
        f'<tr class="grand-total-row"><td>Net worth</td><td></td>'
        f"{money_td(grand_total_ils, 'ILS')}<td></td></tr>"
    )

    overview_table = render_ledger(
        [
            ("Account", ""), ("Institution", ""),
            ("Balance", "num"), ("As of", "num"),
        ],
        overview_rows,
    )

    overview_footnote_parts = [
        f"Net worth uses live market values for tradable positions. "
        f"FX (USD→ILS: {fx_usd_ils:.4f}, GBP→ILS: {fx_gbp_ils:.4f}) and prices fetched from Yahoo Finance "
        f"at render time. Closed accounts are excluded from Cash; "
        f"see SQLite-data → accounts for the full ledger.",
    ]
    if failed_tickers:
        overview_footnote_parts.append(
            f"Live quote failed for: {', '.join(failed_tickers)}."
        )
    if stored_price_fallbacks:
        max_age = max(item["age_days"] for item in stored_price_fallbacks)
        overview_footnote_parts.append(
            f"Used stored prices for {len(stored_price_fallbacks)} position(s), "
            f"max {max_age} day(s) old."
        )
    if cost_basis_fallback_tickers:
        overview_footnote_parts.append(
            f"No recent stored price for: {', '.join(cost_basis_fallback_tickers)} — "
            f"these positions fall back to cost basis."
        )
    overview_footnote = " ".join(overview_footnote_parts)

    # ----- 8. Positions table (sorted by ILS market value desc) -----

    pos_rendered = []
    for pos in held:
        shares = pos["shares"]
        avg_cost = (pos["net_cost"] / shares) if shares else 0.0
        ccy = pos["currency"]
        # Display currency: ILA is shown as ILS (already converted via /100).
        display_ccy = "ILS" if ccy == "ILA" else ccy
        p = prices_by_security_id.get(pos["id"])
        if p:
            mv_ccy = shares * p["price_in_ccy"]
            mv_ils = shares * p["price_in_ils"]
            cost_ils = pos["net_cost"] * (
                fx_usd_ils if ccy == "USD" else (fx_gbp_ils if ccy == "GBP" else 1.0)
            )
            pnl_ils = mv_ils - cost_ils
            pnl_pct = (pnl_ils / cost_ils * 100.0) if cost_ils else None
            # Pure USD P/L: native-currency gain converted to USD at current FX.
            pnl_ccy = mv_ccy - pos["net_cost"]
            if ccy == "USD":
                pnl_usd = pnl_ccy
            elif ccy == "GBP":
                pnl_usd = (pnl_ccy * fx_gbp_ils / fx_usd_ils) if (fx_gbp_ils and fx_usd_ils) else None
            else:  # ILA (already in ILS units after /100)
                pnl_usd = (pnl_ccy / fx_usd_ils) if fx_usd_ils else None
        else:
            mv_ccy = None
            mv_ils = 0.0
            pnl_ils = None
            pnl_pct = None
            pnl_usd = None
        pos_rendered.append({
            "ticker": pos["ticker"],
            "ccy": ccy,
            "display_ccy": display_ccy,
            "shares": shares,
            "avg_cost": avg_cost,
            "net_cost": pos["net_cost"],
            "last_price": p["price_in_ccy"] if p else None,
            "mv_ccy": mv_ccy,
            "mv_ils": mv_ils,
            "pnl_usd": pnl_usd,
            "pnl_ils": pnl_ils,
            "pnl_pct": pnl_pct,
            "last_trade": pos["last_trade"],
        })

    # Sort by ILS market value desc; positions with no price sink to bottom.
    pos_rendered.sort(key=lambda r: (r["mv_ils"] is None, -(r["mv_ils"] or 0)))

    pos_rows = []
    for r in pos_rendered:
        shares_dec = 4 if abs(r["shares"]) < 1 else 2
        last_price_cell = (
            money_td(r["last_price"], r["display_ccy"], decimals=2)
            if r["last_price"] is not None else '<td class="num muted">—</td>'
        )
        mv_ccy_cell = (
            money_td(r["mv_ccy"], r["display_ccy"])
            if r["mv_ccy"] is not None else '<td class="num muted">—</td>'
        )
        mv_ils_cell = (
            money_td(r["mv_ils"], "ILS")
            if r["mv_ils"] is not None and r["last_price"] is not None
            else '<td class="num muted">—</td>'
        )
        pnl_ils_cell = (
            money_td(r["pnl_ils"], "ILS")
            if r["pnl_ils"] is not None else '<td class="num muted">—</td>'
        )
        pnl_usd_cell = (
            money_td(r["pnl_usd"], "USD")
            if r["pnl_usd"] is not None else '<td class="num muted">—</td>'
        )
        pos_rows.append(
            f'<tr><td>{h(r["ticker"])}</td>'
            f'{num_td(r["shares"], shares_dec)}'
            f'{money_td(r["avg_cost"], r["display_ccy"], decimals=2)}'
            f'{money_td(r["net_cost"], r["display_ccy"])}'
            f'{last_price_cell}'
            f'{mv_ccy_cell}'
            f'{mv_ils_cell}'
            f'{pnl_usd_cell}'
            f'{pnl_ils_cell}'
            f'{pct_td(r["pnl_pct"])}'
            f'<td class="muted num">{h(r["last_trade"])}</td></tr>'
        )

    if pos_rows:
        positions_table = render_ledger(
            [
                ("Ticker", ""), ("Shares", "num"),
                ("Avg cost", "num"), ("Net cost", "num"),
                ("Last", "num"), ("Mkt value", "num"),
                ("Mkt value (ILS)", "num"),
                ("P/L (USD)", "num"), ("P/L (ILS)", "num"),
                ("%", "num"), ("Last trade", "num"),
            ],
            pos_rows,
        )
    else:
        positions_table = '<p class="footnote"><em>No open positions.</em></p>'

    # ----- 8b. Snapshot-fed brokerage positions — separate panel -----
    # Snapshot holdings carry fewer columns than the trade-derived table (no Net
    # cost / Last trade), so they get their own panel rather than being crammed
    # into the wider trade-fed table. Values come straight from the reported
    # market value. The whole panel (including its <details> wrapper) is the
    # substitution value, so it renders to "" — nothing at all — when there are
    # no snapshot-fed accounts.
    ibkr_pos_rows: list[str] = []
    for aid in snapshot_fed_ids:
        acc = accounts[aid]
        v = snapshot_values[aid]
        if not v["pos_rows"]:
            continue
        ibkr_pos_rows.append(
            f'<tr class="category-row"><td colspan="7">{h(acc["name"])} '
            f'· {h(acc["institution"])} · snapshot {h(v["snap_date"] or "—")}</td></tr>'
        )
        for p in v["pos_rows"]:
            ccy = p["currency"]
            display_ccy = "ILS" if ccy == "ILA" else ccy
            shares = p["quantity"]
            shares_dec = 4 if abs(shares) < 1 else 2
            mv_native = (p["market_value_minor"] / 100.0) if p["market_value_minor"] is not None else None
            if mv_native is None:
                mv_ils = None
            elif ccy in ("ILS", "ILA"):
                mv_ils = mv_native
            else:
                mv_ils = mv_native * latest_fx(ccy, "ILS", as_of)
            avg_cost = (p["avg_cost_minor"] / 100.0) if p["avg_cost_minor"] is not None else None
            cost_native = (avg_cost * shares) if avg_cost is not None else None
            pnl_native = (mv_native - cost_native) if (mv_native is not None and cost_native is not None) else None
            pnl_pct = (pnl_native / cost_native * 100.0) if (pnl_native is not None and cost_native) else None
            avg_cost_cell = (
                money_td(avg_cost, display_ccy, decimals=2)
                if avg_cost is not None else '<td class="num muted">—</td>'
            )
            mv_native_cell = (
                money_td(mv_native, display_ccy)
                if mv_native is not None else '<td class="num muted">—</td>'
            )
            mv_ils_cell = (
                money_td(mv_ils, "ILS")
                if mv_ils is not None else '<td class="num muted">—</td>'
            )
            pnl_cell = (
                money_td(pnl_native, display_ccy)
                if pnl_native is not None else '<td class="num muted">—</td>'
            )
            ibkr_pos_rows.append(
                f'<tr><td>{h(p["ticker"])}</td>'
                f'{num_td(shares, shares_dec)}'
                f'{avg_cost_cell}'
                f'{mv_native_cell}'
                f'{mv_ils_cell}'
                f'{pnl_cell}'
                f'{pct_td(pnl_pct)}</tr>'
            )
    if ibkr_pos_rows:
        ibkr_positions_table = (
            '<details class="breakdown" open>\n'
            '      <summary>Live snapshot positions</summary>\n      '
            + render_ledger(
                [
                    ("Ticker", ""), ("Shares", "num"),
                    ("Avg cost", "num"), ("Mkt value", "num"),
                    ("Mkt value (ILS)", "num"), ("P/L", "num"), ("%", "num"),
                ],
                ibkr_pos_rows,
            )
            + "\n    </details>"
        )
    else:
        ibkr_positions_table = ""

    # ----- 9. Brokerage deposits (newest first) -----

    deposits = cur.execute("""
        SELECT date, amount_minor/100.0 AS amount, currency, counterparty, description
        FROM transactions
        WHERE account_id=? AND category IN ('deposit','transfer') AND amount_minor>0
        ORDER BY date DESC
    """, (primary_brokerage_id,)).fetchall()
    deposits = [dict(r) for r in deposits]

    # Build cumulative from oldest→newest, then reverse so the table reads newest→oldest
    # with "cumulative-through-this-date" still meaningful (top row = all-time total).
    cum_pairs = []
    running = 0.0
    for r in reversed(deposits):
        running += r["amount"]
        cum_pairs.append((r["date"], running))
    cum_map = dict(cum_pairs)

    dep_rows = []
    for r in deposits:
        cum = cum_map[r["date"]]
        dep_rows.append(
            f'<tr><td class="num">{h(r["date"])}</td>'
            f'{money_td(r["amount"], r["currency"], decimals=2)}'
            f'{money_td(cum, r["currency"])}'
            f'<td class="muted">{h(r["counterparty"])}</td>'
            f'<td class="muted" dir="auto">{h(r["description"])}</td></tr>'
        )

    if dep_rows:
        deposits_table = render_ledger(
            [
                ("Date", "num"), ("Amount", "num"),
                ("Cumulative", "num"), ("Counterparty", ""),
                ("Note", ""),
            ],
            dep_rows,
            footer=f'<tr><td>{len(deposits)} deposits</td><td></td>{money_td(running, "USD")}<td></td><td></td></tr>',
        )
    else:
        deposits_table = '<p class="footnote"><em>No brokerage deposits ingested.</em></p>'

    # ----- 10. All trades (newest first) -----

    trades = cur.execute("""
        SELECT t.date, t.side, s.ticker, t.shares,
               t.price_minor/100.0 AS price,
               (t.shares * t.price_minor)/100.0 AS gross,
               t.fees_minor/100.0 AS fees, t.currency
        FROM trades t JOIN securities s ON s.id=t.security_id
        ORDER BY t.date DESC, t.id DESC
    """).fetchall()
    trades = [dict(r) for r in trades]

    trade_rows = []
    for r in trades:
        side_cls = "pos" if r["side"] == "sell" else "neg"
        side_label = "BUY" if r["side"] == "buy" else "SELL"
        display_ccy = "ILS" if r["currency"] == "ILA" else r["currency"]
        fees_cell = (num_td(r["fees"], 2) if r["fees"]
                     else '<td class="num muted">—</td>')
        shares_dec = 4 if abs(r["shares"]) < 1 else 2
        trade_rows.append(
            f'<tr><td class="num">{h(r["date"])}</td>'
            f'<td class="{side_cls}">{side_label}</td>'
            f'<td>{h(r["ticker"])}</td>'
            f'{num_td(r["shares"], shares_dec)}'
            f'{money_td(r["price"], display_ccy, decimals=2)}'
            f'{money_td(r["gross"], display_ccy)}'
            f'{fees_cell}</tr>'
        )

    if trade_rows:
        trades_table = render_ledger(
            [
                ("Date", "num"), ("Side", ""), ("Ticker", ""),
                ("Shares", "num"), ("Price", "num"), ("Gross", "num"),
                ("Fees", "num"),
            ],
            trade_rows,
            footer=f'<tr><td>{len(trades)} trades</td><td></td><td></td><td></td><td></td><td></td><td></td></tr>',
        )
    else:
        trades_table = '<p class="footnote"><em>No trades ingested.</em></p>'

    # ----- 11. Payslips -----

    payslips = cur.execute(
        "SELECT * FROM payslips ORDER BY period_start DESC LIMIT 12"
    ).fetchall()
    payslips = [dict(r) for r in payslips]

    pay_rows = []
    for r in payslips:
        period = r["period_start"][:7]
        deductions = (r["income_tax_minor"] + r["social_security_minor"] + r["health_insurance_minor"]) / 100.0
        p_e = r["pension_employee_minor"] / 100.0
        p_r = r["pension_employer_minor"] / 100.0
        s_e = r["study_fund_employee_minor"] / 100.0
        s_r = r["study_fund_employer_minor"] / 100.0
        pay_rows.append(
            f'<tr><td class="num">{h(period)}</td>'
            f'<td>{h(r["employer"])}</td>'
            f'{money_td(r["gross_minor"] / 100.0, r["currency"])}'
            f'{money_td(r["net_minor"] / 100.0, r["currency"])}'
            f'{money_td(deductions, r["currency"])}'
            f'<td class="num muted">{fmt_money(p_e)} / {fmt_money(p_r)}</td>'
            f'<td class="num muted">{fmt_money(s_e)} / {fmt_money(s_r)}</td>'
            f'<td class="muted num">{h(r["paid_on"] or "")}</td></tr>'
        )

    if pay_rows:
        payslips_table = render_ledger(
            [
                ("Period", "num"), ("Employer", ""),
                ("Gross", "num"), ("Net", "num"),
                ("Tax+BL+BR", "num"),
                ("Pension emp/empr", "num"),
                ("Study fund emp/empr", "num"),
                ("Paid on", "num"),
            ],
            pay_rows,
        )
    else:
        payslips_table = '<p class="footnote"><em>No payslips yet.</em></p>'

    # ----- 11a. Stock return + dividends for cashflow -----

    from datetime import timedelta as _td

    all_traded_securities = [
        dict(r) for r in cur.execute(
            """SELECT s.id, s.ticker, s.currency, s.asset_class,
                      COALESCE(SUM(CASE WHEN t.side='buy' THEN t.shares ELSE -t.shares END), 0) AS current_shares
               FROM trades t JOIN securities s ON s.id = t.security_id
               GROUP BY s.id
               ORDER BY s.ticker"""
        ).fetchall()
    ]

    def month_start(ym: str) -> date:
        return date.fromisoformat(f"{ym}-01")

    def day_before_month(ym: str) -> str:
        return (month_start(ym) - _td(days=1)).isoformat()

    def month_span_inclusive(start_iso: str, end_iso: str) -> int:
        start = date.fromisoformat(start_iso)
        end = date.fromisoformat(end_iso)
        return max(1, (end.year - start.year) * 12 + end.month - start.month + 1)

    def stock_price_close_minor_on(sec_id: int, on_date: str):
        return cur.execute(
            """SELECT close_minor, currency FROM prices
               WHERE security_id = ? AND date <= ?
               ORDER BY date DESC LIMIT 1""",
            (sec_id, on_date),
        ).fetchone()

    def stock_per_share_ils(close_minor: int, ccy: str, on_date: str) -> float:
        if ccy == "USD":
            return (close_minor / 100.0) * latest_fx("USD", "ILS", on_date)
        if ccy == "GBP":
            return (close_minor / 100.0) * latest_fx("GBP", "ILS", on_date)
        if ccy in ("ILA", "ILS"):
            return close_minor / 100.0
        return 0.0

    def stock_holdings_on(on_date: str) -> dict[int, float]:
        out: dict[int, float] = {}
        for r in cur.execute(
            """SELECT security_id, side, shares
               FROM trades
               WHERE date <= ?
               ORDER BY date, id""",
            (on_date,),
        ).fetchall():
            sgn = 1.0 if r["side"] == "buy" else -1.0
            out[r["security_id"]] = out.get(r["security_id"], 0.0) + sgn * r["shares"]
        return out

    def stock_market_value_ils_on(on_date: str) -> tuple[float, bool]:
        total = 0.0
        all_priced = True
        for sec_id, shares in stock_holdings_on(on_date).items():
            if abs(shares) < 1e-9:
                continue
            pr = stock_price_close_minor_on(sec_id, on_date)
            if not pr:
                all_priced = False
                continue
            total += shares * stock_per_share_ils(pr["close_minor"], pr["currency"], on_date)
        return total, all_priced

    def trade_value_ils(r: sqlite3.Row, include_fee: bool, fee_sign: float = 1.0) -> float:
        native = r["shares"] * r["price_minor"] / 100.0
        if include_fee:
            native += fee_sign * (r["fees_minor"] / 100.0)
        if r["currency"] in ("ILS", "ILA"):
            return native
        return native * latest_fx(r["currency"], "ILS", r["date"])

    stock_pl_used_fallback = False

    def stock_pl_between(start_boundary: Optional[str], end_iso: str) -> float:
        """Period security P/L in ILS, net of trade fees and excluding dividends:
        ending MV + sell proceeds - buy costs - starting MV."""
        nonlocal stock_pl_used_fallback
        if start_boundary is None:
            start_mv = 0.0
            start_priced = True
            date_filter = "date <= ?"
            params: tuple = (end_iso,)
        else:
            start_mv, start_priced = stock_market_value_ils_on(start_boundary)
            date_filter = "date > ? AND date <= ?"
            params = (start_boundary, end_iso)

        end_mv, end_priced = stock_market_value_ils_on(end_iso)
        if not start_priced:
            stock_pl_used_fallback = True
            start_mv = cost_basis_ils_at(start_boundary or end_iso)
        if not end_priced:
            stock_pl_used_fallback = True
            end_mv = cost_basis_ils_at(end_iso)

        buy_cost = 0.0
        sell_proceeds = 0.0
        for r in cur.execute(
            f"""SELECT date, side, shares, price_minor, fees_minor, currency
                FROM trades
                WHERE {date_filter}""",
            params,
        ).fetchall():
            if r["side"] == "buy":
                buy_cost += trade_value_ils(r, include_fee=True, fee_sign=1.0)
            else:
                sell_proceeds += trade_value_ils(r, include_fee=True, fee_sign=-1.0)
        return end_mv + sell_proceeds - buy_cost - start_mv

    def build_dividend_context() -> dict:
        recorded: list[dict] = []
        for r in cur.execute(
            """SELECT t.account_id, t.date, t.amount_minor, t.currency,
                      t.counterparty AS ticker, t.description,
                      d.doc_type AS source_doc_type
               FROM transactions t
               LEFT JOIN documents d ON d.id = t.source_doc_id
               WHERE t.category = 'dividend'
               ORDER BY t.date DESC"""
        ).fetchall():
            amount_native = r["amount_minor"] / 100.0
            amount_ils = (amount_native if r["currency"] == "ILS"
                          else amount_native * latest_fx(r["currency"], "ILS", r["date"]))
            recorded.append({
                "date": r["date"], "ticker": r["ticker"] or "—",
                "amount_native": amount_native, "currency": r["currency"], "amount_ils": amount_ils,
                "status": "recorded",
                "confirmed": r["source_doc_type"] != "yahoo_dividend_estimate",
                "note": r["description"] or "",
                "account_id": r["account_id"],
            })

        recorded_keys = {
            (r["ticker"], r["date"][:7])
            for r in recorded
            if r.get("account_id") in trade_fed_ids and r.get("ticker") and r["ticker"] != "—"
        }

        def latest_cash_snapshot_for(ccy: str) -> Optional[str]:
            if primary_brokerage_id is None:
                return None
            cash_ccy = "ILS" if ccy in ("ILS", "ILA") else ccy
            component = f"cash_{cash_ccy.lower()}"
            return cur.execute(
                "SELECT MAX(as_of) FROM balances WHERE account_id=? AND component=?",
                (primary_brokerage_id, component),
            ).fetchone()[0]

        estimated: list[dict] = []
        upcoming: list[dict] = []
        will_get_usd = 0.0
        will_get_ils = 0.0
        fetch_failed: list[str] = []
        end_of_year_iso = date(today.year, 12, 31).isoformat()

        for sec in all_traded_securities:
            sym = yahoo_symbol_for(sec["ticker"])
            try:
                chart_data_raw = _yd_fetch_chart(sym, range_str="5y", timeout=10)
            except Exception:
                chart_data_raw = None
            if not chart_data_raw:
                fetch_failed.append(sec["ticker"])
                continue
            try:
                result = chart_data_raw["chart"]["result"][0]
            except (KeyError, IndexError, TypeError):
                fetch_failed.append(sec["ticker"])
                continue
            yahoo_ccy = (result.get("meta") or {}).get("currency")
            past_events = _yd_extract_past(result)
            sec_ccy = sec["currency"]
            disp_ccy = "ILS" if sec_ccy in ("ILS", "ILA") else sec_ccy
            latest_snapshot = latest_cash_snapshot_for(sec_ccy)

            sec_trades = cur.execute(
                """SELECT date, side, shares FROM trades
                   WHERE security_id = ? ORDER BY date, id""",
                (sec["id"],),
            ).fetchall()

            for ev in past_events:
                if ev["pay_date"] > today.isoformat():
                    continue
                shares_then = sum((t["shares"] if t["side"] == "buy" else -t["shares"])
                                  for t in sec_trades if t["date"] <= ev["pay_date"])
                if shares_then <= 1e-6:
                    continue
                if (sec["ticker"], ev["pay_date"][:7]) in recorded_keys:
                    continue
                per_share = ev["amount_per_share"] / 100.0 if yahoo_ccy in ("GBp", "ILA") else ev["amount_per_share"]
                net_native = shares_then * per_share * (1.0 - ISRAELI_CAPGAINS_TAX)
                amount_ils = (net_native if disp_ccy == "ILS"
                              else net_native * latest_fx(disp_ccy, "ILS", ev["pay_date"]))
                absorbed = latest_snapshot is not None and ev["pay_date"] <= latest_snapshot
                estimated.append({
                    "date": ev["pay_date"], "ticker": sec["ticker"],
                    "amount_native": net_native, "currency": disp_ccy, "amount_ils": amount_ils,
                    "status": "absorbed" if absorbed else "est", "confirmed": False, "note": "",
                })

            current_shares = sec["current_shares"]
            if current_shares <= 1e-6:
                continue

            for rev in _yd_project_through(past_events, end_of_year_iso, today.isoformat()):
                ps = rev["amount_per_share"] / 100.0 if yahoo_ccy in ("GBp", "ILA") else rev["amount_per_share"]
                net_native = current_shares * ps * (1.0 - ISRAELI_CAPGAINS_TAX)
                if disp_ccy == "USD":
                    will_get_usd += net_native
                    will_get_ils += net_native * fx_usd_ils
                elif disp_ccy == "GBP":
                    will_get_ils += net_native * fx_gbp_ils
                    will_get_usd += (net_native * fx_gbp_ils / fx_usd_ils) if fx_usd_ils else 0.0
                else:
                    will_get_ils += net_native
                    will_get_usd += (net_native / fx_usd_ils) if fx_usd_ils else 0.0

            nxt = _yd_project_next(past_events)
            if not nxt or nxt["pay_date"] <= today.isoformat():
                continue
            per_share = nxt["amount_per_share"] / 100.0 if yahoo_ccy in ("GBp", "ILA") else nxt["amount_per_share"]
            amount_native_net = current_shares * per_share * (1.0 - ISRAELI_CAPGAINS_TAX)
            if disp_ccy == "USD":
                amount_ils = amount_native_net * fx_usd_ils
            elif disp_ccy == "GBP":
                amount_ils = amount_native_net * fx_gbp_ils
            else:
                amount_ils = amount_native_net
            upcoming.append({
                "date": nxt["pay_date"], "ticker": sec["ticker"],
                "amount_native": amount_native_net, "currency": disp_ccy, "amount_ils": amount_ils,
            })

        return {
            "recorded": recorded,
            "estimated": estimated,
            "past": sorted(recorded + estimated, key=lambda r: r["date"], reverse=True),
            "upcoming": sorted(upcoming, key=lambda x: x["date"])[:5],
            "will_get_usd": will_get_usd,
            "will_get_ils": will_get_ils,
            "fetch_failed": fetch_failed,
        }

    dividend_context = build_dividend_context()
    div_past = dividend_context["past"]

    # ----- 11b. Expenses + cash flow -----

    def ils_equiv(amount_minor: int, ccy: str, on_date: str) -> float:
        amt = amount_minor / 100.0
        return amt if ccy == "ILS" else amt * latest_fx(ccy, "ILS", on_date)

    excl_placeholders = ",".join("?" * len(EXPENSE_EXCLUDED_CATEGORIES))
    excl_params = tuple(EXPENSE_EXCLUDED_CATEGORIES)

    def expenses_between(start_iso: str, end_iso: str) -> list[dict]:
        rows = cur.execute(
            f"""SELECT date, amount_minor, currency, counterparty, description, category, account_id
                FROM transactions
                WHERE amount_minor < 0
                  AND COALESCE(category, '') NOT IN ({excl_placeholders})
                  AND date >= ? AND date <= ?
                ORDER BY date DESC, id DESC""",
            excl_params + (start_iso, end_iso),
        ).fetchall()
        return [dict(r) for r in rows]

    # --- 30-day window for recent table + two charts ---
    thirty_start = (today - _td(days=29)).isoformat()
    recent_expenses = expenses_between(thirty_start, as_of)

    # Recent expenses scrollable table
    exp_total_30d = 0.0
    exp_rows = []
    for r in recent_expenses:
        amt_ils_signed = ils_equiv(r["amount_minor"], r["currency"], r["date"])
        exp_total_30d += abs(amt_ils_signed)
        label = r["counterparty"] or r["description"] or "(no merchant)"
        cat_label = r["category"] or "—"
        exp_rows.append(
            f'<tr><td class="num">{h(r["date"])}</td>'
            f'<td dir="auto">{h(label)}</td>'
            f'<td class="muted">{h(cat_label)}</td>'
            f'{money_td(amt_ils_signed, "ILS")}</tr>'
        )

    if exp_rows:
        expenses_table = render_ledger(
            [
                ("Date", "num"), ("Merchant", ""),
                ("Category", ""), ("Amount", "num"),
            ],
            exp_rows,
            footer=(f"<tr><td>{len(exp_rows)} txns (last 30d)</td><td></td><td></td>"
                    f"{money_td(-exp_total_30d, 'ILS')}</tr>"),
        )
    else:
        expenses_table = '<p class="footnote"><em>No expenses recorded in the last 30 days.</em></p>'

    # Categories chart — trailing 30 days, with per-category txns for tooltip
    cat_aggs: dict[str, dict] = {}
    recent_total_ils = 0.0
    for r in recent_expenses:
        amt_abs = abs(ils_equiv(r["amount_minor"], r["currency"], r["date"]))
        recent_total_ils += amt_abs
        cat = r["category"] or "uncategorized"
        if cat not in cat_aggs:
            cat_aggs[cat] = {"total": 0.0, "count": 0, "txns": []}
        cat_aggs[cat]["total"] += amt_abs
        cat_aggs[cat]["count"] += 1
        label = r["counterparty"] or r["description"] or "(merchant)"
        cat_aggs[cat]["txns"].append([
            r["date"],
            label[:60],
            round(amt_abs, 2),
        ])
    expense_categories_data = sorted(
        [{
            "category": c,
            "total": round(d["total"], 2),
            "count": d["count"],
            "txns": sorted(d["txns"], key=lambda t: -t[2]),
        } for c, d in cat_aggs.items()],
        key=lambda x: -x["total"],
    )

    if recent_total_ils > 0:
        recent_total_str = f"{recent_total_ils:,.0f} ILS"
        mtd_note_html = (
            f'<p class="breakdown-note">Spent <strong>{h(recent_total_str)}</strong> '
            f'over the last 30 days — breakdown below.</p>'
        )
    else:
        mtd_note_html = (
            f'<p class="breakdown-note">No expenses in the last 30 days.</p>'
        )

    # Daily 30-day chart — totals + per-category daily series (for stacked bars)
    # + per-day txn list (for tooltip). Category order matches expense_categories_data
    # (descending by 30-day total), so JS palette indexing stays consistent across charts.
    daily_dates_list: list[str] = []
    daily_totals_list: list[float] = []
    daily_txns_list: list[list] = []
    cursor_d = today - _td(days=29)
    while cursor_d <= today:
        daily_dates_list.append(cursor_d.isoformat())
        daily_totals_list.append(0.0)
        daily_txns_list.append([])
        cursor_d += _td(days=1)
    date_to_idx = {d: i for i, d in enumerate(daily_dates_list)}

    cat_order = [c["category"] for c in expense_categories_data]
    cat_to_pos = {c: i for i, c in enumerate(cat_order)}
    daily_per_cat = [[0.0] * len(daily_dates_list) for _ in cat_order]

    for r in recent_expenses:
        idx = date_to_idx.get(r["date"])
        if idx is None:
            continue
        amt_abs = abs(ils_equiv(r["amount_minor"], r["currency"], r["date"]))
        daily_totals_list[idx] += amt_abs
        cat = r["category"] or "uncategorized"
        ci = cat_to_pos.get(cat)
        if ci is not None:
            daily_per_cat[ci][idx] += amt_abs
        label = r["counterparty"] or r["description"] or "(merchant)"
        daily_txns_list[idx].append([
            label[:60],
            cat,
            round(amt_abs, 2),
        ])
    daily_totals_list = [round(v, 2) for v in daily_totals_list]
    daily_per_cat = [[round(v, 2) for v in series] for series in daily_per_cat]

    # Monthly all-time chart (1 bar per year-month, with top categories for tooltip)
    monthly_rows = cur.execute(
        f"""SELECT substr(date, 1, 7) AS ym, category, amount_minor, currency, date
            FROM transactions
            WHERE amount_minor < 0
              AND COALESCE(category, '') NOT IN ({excl_placeholders})
            ORDER BY date""",
        excl_params,
    ).fetchall()
    monthly_buckets: dict[str, dict] = {}
    for r in monthly_rows:
        ym = r["ym"]
        amt_abs = abs(ils_equiv(r["amount_minor"], r["currency"], r["date"]))
        if ym not in monthly_buckets:
            monthly_buckets[ym] = {"total": 0.0, "cats": {}}
        monthly_buckets[ym]["total"] += amt_abs
        c = r["category"] or "uncategorized"
        monthly_buckets[ym]["cats"][c] = monthly_buckets[ym]["cats"].get(c, 0.0) + amt_abs

    monthly_alltime_data: list[dict] = []
    if monthly_buckets:
        start_ym = min(monthly_buckets.keys())
        end_ym = today.strftime("%Y-%m")
        sy, sm = (int(p) for p in start_ym.split("-"))
        ey, em = (int(p) for p in end_ym.split("-"))
        y, m = sy, sm
        while (y, m) <= (ey, em):
            ym = f"{y:04d}-{m:02d}"
            bucket = monthly_buckets.get(ym, {"total": 0.0, "cats": {}})
            top = sorted(bucket["cats"].items(), key=lambda kv: -kv[1])[:3]
            monthly_alltime_data.append({
                "month": ym,
                "total": round(bucket["total"], 2),
                "top": [[c, round(t, 2)] for c, t in top],
            })
            m += 1
            if m > 12:
                m = 1
                y += 1

    # --- Cash flow averages (Overview compact block) ---
    # Bucket by calendar month, not by day-of-month window: payslips have
    # period_start on the 1st, so a today-minus-90d window silently clips the
    # earliest payslip whenever today isn't near month-end.
    def last_n_calendar_months(n: int) -> list[str]:
        y, m = today.year, today.month
        out: list[str] = []
        for _ in range(n):
            out.append(f"{y:04d}-{m:02d}")
            m -= 1
            if m == 0:
                m = 12
                y -= 1
        return list(reversed(out))

    def expense_total_for_months(months: list[str]) -> float:
        if not months:
            return 0.0
        ym_placeholders = ",".join("?" * len(months))
        rows = cur.execute(
            f"""SELECT amount_minor, currency, date FROM transactions
                WHERE amount_minor < 0
                  AND COALESCE(category, '') NOT IN ({excl_placeholders})
                  AND substr(date, 1, 7) IN ({ym_placeholders})""",
            excl_params + tuple(months),
        ).fetchall()
        return sum(abs(ils_equiv(r["amount_minor"], r["currency"], r["date"])) for r in rows)

    def job_income_for_months(months: list[str]) -> float:
        # Per month: payslip net if present, else fall back to salary transactions.
        # Mixing globally would double-count months that have both (the bank salary
        # deposit and the payslip net are the same money).
        total = 0.0
        for ym in months:
            ps = cur.execute(
                "SELECT net_minor FROM payslips WHERE substr(period_start, 1, 7) = ?",
                (ym,),
            ).fetchall()
            if ps:
                total += sum(r["net_minor"] for r in ps) / 100.0
                continue
            txs = cur.execute(
                """SELECT amount_minor, currency, date FROM transactions
                    WHERE category='salary' AND amount_minor > 0
                      AND substr(date, 1, 7) = ?""",
                (ym,),
            ).fetchall()
            total += sum(ils_equiv(r["amount_minor"], r["currency"], r["date"]) for r in txs)
        return total

    def other_income_for_months(months: list[str]) -> float:
        if not months:
            return 0.0
        cat_placeholders = ",".join("?" * len(OTHER_INCOME_CATEGORIES))
        ym_placeholders = ",".join("?" * len(months))
        rows = cur.execute(
            f"""SELECT amount_minor, currency, date FROM transactions
                WHERE amount_minor > 0
                  AND category IN ({cat_placeholders})
                  AND substr(date, 1, 7) IN ({ym_placeholders})""",
            tuple(OTHER_INCOME_CATEGORIES) + tuple(months),
        ).fetchall()
        return sum(ils_equiv(r["amount_minor"], r["currency"], r["date"]) for r in rows)

    months_3m = last_n_calendar_months(3)
    months_12m = last_n_calendar_months(12)

    spend_3m = expense_total_for_months(months_3m) / 3.0
    spend_12m = expense_total_for_months(months_12m) / 12.0
    job_3m = job_income_for_months(months_3m) / 3.0
    job_12m = job_income_for_months(months_12m) / 12.0
    other_3m = other_income_for_months(months_3m) / 3.0
    other_12m = other_income_for_months(months_12m) / 12.0

    n_months_row = cur.execute(
        "SELECT COUNT(DISTINCT substr(date,1,7)) AS n FROM transactions"
    ).fetchone()
    n_months_alltime = max(1, n_months_row["n"] or 1)

    all_expense_rows = cur.execute(
        f"""SELECT amount_minor, currency, date FROM transactions
            WHERE amount_minor < 0
              AND COALESCE(category, '') NOT IN ({excl_placeholders})""",
        excl_params,
    ).fetchall()
    spend_all = sum(
        abs(ils_equiv(r["amount_minor"], r["currency"], r["date"]))
        for r in all_expense_rows
    ) / n_months_alltime

    # Job all-time: every payslip net, plus salary txns only for months with no
    # payslip (avoids double-counting Mar/Apr 2026 where both exist).
    payslip_total_minor = cur.execute(
        "SELECT COALESCE(SUM(net_minor), 0) AS s FROM payslips"
    ).fetchone()["s"]
    payslip_months = {
        r["ym"]
        for r in cur.execute(
            "SELECT DISTINCT substr(period_start, 1, 7) AS ym FROM payslips"
        ).fetchall()
    }
    salary_outside_payslips_ils = sum(
        ils_equiv(r["amount_minor"], r["currency"], r["date"])
        for r in cur.execute(
            "SELECT amount_minor, currency, date FROM transactions "
            "WHERE category='salary' AND amount_minor > 0"
        ).fetchall()
        if r["date"][:7] not in payslip_months
    )
    job_all = (payslip_total_minor / 100.0 + salary_outside_payslips_ils) / n_months_alltime

    other_cat_placeholders = ",".join("?" * len(OTHER_INCOME_CATEGORIES))
    other_all = sum(
        ils_equiv(r["amount_minor"], r["currency"], r["date"])
        for r in cur.execute(
            f"""SELECT amount_minor, currency, date FROM transactions
                WHERE amount_minor > 0 AND category IN ({other_cat_placeholders})""",
            tuple(OTHER_INCOME_CATEGORIES),
        ).fetchall()
    ) / n_months_alltime

    def dividend_total_for_months(months: list[str]) -> float:
        month_set = set(months)
        return sum(r["amount_ils"] for r in div_past if r["date"][:7] in month_set)

    first_trade_date = cur.execute("SELECT MIN(date) AS d FROM trades").fetchone()["d"]
    stock_months_alltime = month_span_inclusive(first_trade_date, as_of) if first_trade_date else n_months_alltime

    div_3m = dividend_total_for_months(months_3m) / 3.0
    div_12m = dividend_total_for_months(months_12m) / 12.0
    div_all = (sum(r["amount_ils"] for r in div_past) / stock_months_alltime) if div_past else 0.0

    stock_pl_3m_total = stock_pl_between(day_before_month(months_3m[0]), as_of)
    stock_pl_12m_total = stock_pl_between(day_before_month(months_12m[0]), as_of)
    stock_pl_all_total = stock_pl_between(None, as_of)

    def after_tax_stock_pl(v: float) -> float:
        return v * (1.0 - ISRAELI_CAPGAINS_TAX) if v > 0 else v

    stock_pl_3m = after_tax_stock_pl(stock_pl_3m_total) / 3.0
    stock_pl_12m = after_tax_stock_pl(stock_pl_12m_total) / 12.0
    stock_pl_all = after_tax_stock_pl(stock_pl_all_total) / stock_months_alltime

    def cell_signed(v: float, cls_when_zero: str = "") -> str:
        if v == 0:
            return f'<td class="num {cls_when_zero}">—</td>' if cls_when_zero else '<td class="num">0</td>'
        cls = "num pos" if v > 0 else "num neg"
        sign = "+" if v > 0 else "-"
        return f'<td class="{cls}">{sign}{abs(v):,.0f}</td>'

    net_3m = job_3m + other_3m + div_3m + stock_pl_3m - spend_3m
    net_12m = job_12m + other_12m + div_12m + stock_pl_12m - spend_12m
    net_all = job_all + other_all + div_all + stock_pl_all - spend_all

    stock_footnote_extra = (
        " Some boundary prices were missing, so P/L used cost-basis fallback."
        if stock_pl_used_fallback else ""
    )
    dividend_fetch_extra = (
        f" Yahoo dividend fetch failed for {len(dividend_context['fetch_failed'])} ticker(s); "
        "those dividends are omitted."
        if dividend_context["fetch_failed"] else ""
    )

    flow_summary_html = (
        '<table class="ledger flow-summary">'
        '<thead><tr>'
        '<th></th>'
        '<th class="num">3M</th>'
        '<th class="num">12M</th>'
        '<th class="num">all-time</th>'
        '</tr></thead><tbody>'
        f'<tr><td>Spend <span class="muted">(avg/mo)</span></td>'
        f'{cell_signed(-spend_3m)}{cell_signed(-spend_12m)}{cell_signed(-spend_all)}</tr>'
        f'<tr><td>Income — job <span class="muted">(avg/mo)</span></td>'
        f'{cell_signed(job_3m)}{cell_signed(job_12m)}{cell_signed(job_all)}</tr>'
        f'<tr><td>Income — other <span class="muted">(avg/mo)</span></td>'
        f'{cell_signed(other_3m)}{cell_signed(other_12m)}{cell_signed(other_all)}</tr>'
        f'<tr><td>Income — dividends <span class="muted">(avg/mo)</span></td>'
        f'{cell_signed(div_3m, "muted")}{cell_signed(div_12m, "muted")}{cell_signed(div_all, "muted")}</tr>'
        f'<tr><td>Stocks P/L after tax <span class="muted">(avg/mo)</span></td>'
        f'{cell_signed(stock_pl_3m, "muted")}{cell_signed(stock_pl_12m, "muted")}{cell_signed(stock_pl_all, "muted")}</tr>'
        '</tbody><tfoot>'
        f'<tr><td>Net flow <span class="muted">(avg/mo)</span></td>'
        f'{cell_signed(net_3m)}{cell_signed(net_12m)}{cell_signed(net_all)}</tr>'
        '</tfoot></table>'
        f'<p class="footnote">* Dividends use recorded rows plus Yahoo-reconstructed dividends '
        f'for shares held on each event date, net of {ISRAELI_CAPGAINS_TAX:.0%} withholding. '
        f'Stocks P/L uses ending MV + sells − buys − fees − starting MV, excludes dividends, '
        f'then reduces positive P/L by {ISRAELI_CAPGAINS_TAX:.0%} estimated capital-gains tax. '
        f'Negative P/L is not tax-adjusted. All-time stock rows are divided by '
        f'{stock_months_alltime} stock-active months.{stock_footnote_extra}{dividend_fetch_extra}</p>'
    )

    expenses_chart_payload = {
        "categories30": expense_categories_data,
        "daily30": {
            "dates": daily_dates_list,
            "totals": daily_totals_list,
            "txns": daily_txns_list,
            "categories": cat_order,
            "perCatTotals": daily_per_cat,
        },
        "monthlyAlltime": monthly_alltime_data,
    }

    # ----- 12. SQLite debug tabs -----

    all_tables = [r["name"] for r in cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )]
    sqlite_tabs_html = []
    for tname in all_tables:
        total = cur.execute(f"SELECT COUNT(*) AS n FROM {tname}").fetchone()["n"]
        rows = cur.execute(f"SELECT * FROM {tname} LIMIT 50").fetchall()
        if not rows and total == 0:
            cols = [c[1] for c in cur.execute(f"PRAGMA table_info({tname})").fetchall()]
            body = f'<p class="footnote"><em>empty — columns: {", ".join(cols)}</em></p>'
            sqlite_tabs_html.append(
                f'<details class="subsection"><summary>{h(tname)} '
                f'<span class="row-count">(0 rows)</span></summary>{body}</details>'
            )
            continue
        cols = list(rows[0].keys())
        thead = "".join(f'<th>{h(c)}</th>' for c in cols)
        body_rows = []
        for row in rows:
            cells = []
            for c in cols:
                v = row[c]
                if v is None:
                    cells.append('<td class="muted">∅</td>')
                elif isinstance(v, (int, float)):
                    cells.append(f'<td class="num">{h(v)}</td>')
                else:
                    s = str(v)
                    if len(s) > 80:
                        s = s[:77] + "…"
                    cells.append(f'<td dir="auto">{h(s)}</td>')
            body_rows.append(f"<tr>{''.join(cells)}</tr>")
        shown = len(rows)
        rc = f"({total} rows" + (f"; showing first {shown}" if total > shown else "") + ")"
        table_html = (
            f'<table class="ledger"><thead><tr>{thead}</tr></thead>'
            f'<tbody>{"".join(body_rows)}</tbody></table>'
        )
        sqlite_tabs_html.append(
            f'<details class="subsection"><summary>{h(tname)} '
            f'<span class="row-count">{rc}</span></summary>{table_html}</details>'
        )
    sqlite_tabs = "\n".join(sqlite_tabs_html)

    # ----- 13. Chart data -----

    from datetime import timedelta as _td

    deposit_chart_rows = [dict(r) for r in cur.execute(
        """SELECT date, amount_minor/100.0 AS amount
           FROM transactions
           WHERE account_id=? AND category IN ('deposit','transfer') AND amount_minor>0
           ORDER BY date""",
        (primary_brokerage_id,)
    ).fetchall()]
    trade_rows_for_chart = [dict(r) for r in cur.execute(
        """SELECT t.date, t.security_id, t.side, t.shares, s.currency AS sec_ccy
           FROM trades t JOIN securities s ON s.id = t.security_id
           ORDER BY t.date"""
    ).fetchall()]

    def price_close_minor_on(sec_id: int, on_date: str):
        return cur.execute(
            """SELECT close_minor, currency FROM prices
               WHERE security_id = ? AND date <= ?
               ORDER BY date DESC LIMIT 1""",
            (sec_id, on_date),
        ).fetchone()

    def per_share_usd(close_minor: int, ccy: str, on_date: str) -> float:
        if ccy == "USD":
            return close_minor / 100.0
        usd_ils = latest_fx("USD", "ILS", on_date)
        if usd_ils <= 0:
            return 0.0
        if ccy == "GBP":
            return (close_minor / 100.0) * (latest_fx("GBP", "ILS", on_date) / usd_ils)
        if ccy in ("ILA", "ILS"):
            return (close_minor / 100.0) / usd_ils
        return 0.0

    def per_share_ils_on(close_minor: int, ccy: str, on_date: str) -> float:
        if ccy == "USD":
            return (close_minor / 100.0) * latest_fx("USD", "ILS", on_date)
        if ccy == "GBP":
            return (close_minor / 100.0) * latest_fx("GBP", "ILS", on_date)
        if ccy in ("ILA", "ILS"):
            return close_minor / 100.0
        return 0.0

    def brokerage_value_ils_on(on_date: str) -> tuple[float, bool]:
        """Returns (ils_market_value, all_priced). all_priced is False if any open
        position lacked a price ≤ on_date — callers should fall back to cost basis
        rather than reporting a silently-undercounted figure."""
        holdings: dict[int, float] = {}
        for t in trade_rows_for_chart:
            if t["date"] > on_date:
                break
            sgn = 1.0 if t["side"] == "buy" else -1.0
            holdings[t["security_id"]] = holdings.get(t["security_id"], 0.0) + sgn * t["shares"]
        total = 0.0
        all_priced = True
        for sec_id, shares in holdings.items():
            if abs(shares) < 1e-9:
                continue
            pr = price_close_minor_on(sec_id, on_date)
            if not pr:
                all_priced = False
                continue
            total += shares * per_share_ils_on(pr["close_minor"], pr["currency"], on_date)
        return total, all_priced

    def brokerage_cash_usd_on(on_date: str) -> float:
        usd_ils = latest_fx("USD", "ILS", on_date)
        if usd_ils <= 0:
            return 0.0
        total = 0.0
        for ccy, info in brokerage_cash_at(on_date).items():
            amount = info["amount"]
            if abs(amount) < 0.01:
                continue
            if ccy == "USD":
                total += amount
            elif ccy == "GBP":
                total += amount * latest_fx("GBP", "ILS", on_date) / usd_ils
            elif ccy in ("ILS", "ILA"):
                total += amount / usd_ils
        return total

    # ----- 13a. Net worth over time (weekly, all-accounts at as-of) -----

    if deposit_chart_rows:
        nw_start = date.fromisoformat(deposit_chart_rows[0]["date"])
    else:
        earliest_balance = cur.execute("SELECT MIN(as_of) AS d FROM balances").fetchone()["d"]
        nw_start = date.fromisoformat(earliest_balance) if earliest_balance else today

    nw_timeline: list[str] = []
    cursor_d = nw_start
    while cursor_d <= today:
        nw_timeline.append(cursor_d.isoformat())
        cursor_d += _td(days=7)
    if not nw_timeline or nw_timeline[-1] != as_of:
        nw_timeline.append(as_of)

    nw_dates = nw_timeline
    nw_series_values = []
    nw_breakdowns: list[list[list]] = []  # per-date list of [label, ils_amount] pairs
    for d in nw_dates:
        per_account: dict[str, float] = {}
        for r in net_worth_balances_at(d):
            if r["account_id"] == primary_brokerage_id:
                continue  # brokerage cash is derived via brokerage_cash_at to include post-snapshot flows
            acc_name = accounts[r["account_id"]]["name"]
            amt = r["amount_minor"] / 100.0
            ils = amt if r["currency"] == "ILS" else amt * latest_fx(r["currency"], "ILS", d)
            per_account[acc_name] = per_account.get(acc_name, 0.0) + ils
        broker_ils, all_priced = brokerage_value_ils_on(d)
        if not all_priced:
            # Some position lacked a price on this historical date — fall back to
            # honest cost basis (per-trade historical FX) instead of plotting a
            # partial market value.
            broker_ils = cost_basis_ils_at(d)
        if broker_ils > 0 and primary_brokerage_id is not None:
            per_account[accounts[primary_brokerage_id]["name"]] = broker_ils
        brokerage_cash_d = brokerage_cash_at(d)
        brokerage_cash_ils_d = 0.0
        for ccy, info in brokerage_cash_d.items():
            amt = info["amount"]
            if abs(amt) < 0.01:
                continue
            brokerage_cash_ils_d += amt if ccy == "ILS" else amt * latest_fx(ccy, "ILS", d)
        if abs(brokerage_cash_ils_d) > 0.01 and primary_brokerage_id is not None:
            per_account[f'{accounts[primary_brokerage_id]["name"]} · cash'] = brokerage_cash_ils_d
        # Snapshot brokerages (IBKR): add positions MV at this date (carried
        # forward from the latest snapshot ≤ d; nothing before the first one).
        # IBKR cash already merged in via the generic balance loop above, under
        # the same account name, so the two combine into one series entry.
        for aid in snapshot_fed_ids:
            pos_ils_d = 0.0
            for p in snapshot_positions_at(aid, d):
                if p["market_value_minor"] is None:
                    continue
                native = p["market_value_minor"] / 100.0
                ccy = p["currency"]
                pos_ils_d += native if ccy in ("ILS", "ILA") else native * latest_fx(ccy, "ILS", d)
            if abs(pos_ils_d) > 0.01:
                nm = accounts[aid]["name"]
                per_account[nm] = per_account.get(nm, 0.0) + pos_ils_d
        v = sum(per_account.values())
        nw_series_values.append(round(v, 2))
        nw_breakdowns.append(
            [[name, round(amount, 2)]
             for name, amount in sorted(per_account.items(), key=lambda kv: -kv[1])]
        )

    sv_dates: list[str] = []
    sv_deposits: list[float] = []
    sv_stocks: list[Optional[float]] = []
    sv_spy: list[Optional[float]] = []

    if deposit_chart_rows:
        first_dep = date.fromisoformat(deposit_chart_rows[0]["date"])
        timeline_dates: list[str] = []
        cursor_d = first_dep
        while cursor_d <= today:
            timeline_dates.append(cursor_d.isoformat())
            cursor_d += _td(days=7)
        if timeline_dates[-1] != as_of:
            timeline_dates.append(as_of)
    else:
        timeline_dates = [as_of]

    for tl_date in timeline_dates:
        cum_deps_usd = 0.0
        for d in deposit_chart_rows:
            if d["date"] > tl_date:
                break
            cum_deps_usd += d["amount"]
        sv_deposits.append(round(cum_deps_usd, 2))

        holdings: dict[int, float] = {}
        for t in trade_rows_for_chart:
            if t["date"] > tl_date:
                break
            sgn = 1.0 if t["side"] == "buy" else -1.0
            holdings[t["security_id"]] = holdings.get(t["security_id"], 0.0) + sgn * t["shares"]

        portfolio_usd = 0.0
        all_priced = True
        for sec_id, shares in holdings.items():
            if abs(shares) < 1e-9:
                continue
            price_row = price_close_minor_on(sec_id, tl_date)
            if not price_row:
                all_priced = False
                break
            portfolio_usd += shares * per_share_usd(
                price_row["close_minor"], price_row["currency"], tl_date
            )
        portfolio_usd += brokerage_cash_usd_on(tl_date)
        sv_stocks.append(round(portfolio_usd, 2) if all_priced and portfolio_usd > 0 else None)

        spy_shares = 0.0
        for d in deposit_chart_rows:
            if d["date"] > tl_date:
                break
            spy_price_row = price_close_minor_on(spy_id, d["date"])
            if not spy_price_row:
                continue
            spy_price_usd = spy_price_row["close_minor"] / 100.0
            if spy_price_usd > 0:
                spy_shares += d["amount"] / spy_price_usd

        spy_now_row = price_close_minor_on(spy_id, tl_date)
        if spy_shares > 0 and spy_now_row:
            sv_spy.append(round(spy_shares * (spy_now_row["close_minor"] / 100.0), 2))
        else:
            sv_spy.append(None)

        sv_dates.append(tl_date)

    pay_for_chart = cur.execute(
        "SELECT period_start, gross_minor, net_minor FROM payslips ORDER BY period_start"
    ).fetchall()
    pay_for_chart = [dict(r) for r in pay_for_chart]
    pay_months = [r["period_start"][:7] for r in pay_for_chart]
    pay_gross = [r["gross_minor"] / 100.0 for r in pay_for_chart]
    pay_net = [r["net_minor"] / 100.0 for r in pay_for_chart]

    fx_deposits_payload = build_fx_deposits_payload(cur, primary_brokerage_id)

    # ----- 13b. Dividends — display + timeseries + upcoming -----

    # Shared with cashflow above: recorded rows plus Yahoo-reconstructed dividends
    # for every traded security, not just the positions still held today.
    div_recorded = dividend_context["recorded"]
    div_estimated = dividend_context["estimated"]
    div_upcoming = dividend_context["upcoming"]
    will_get_usd = dividend_context["will_get_usd"]
    will_get_ils = dividend_context["will_get_ils"]
    div_past = dividend_context["past"]

    # Timeseries: monthly USD received, stacked by payer (top-5 all-time + Other).
    # USD per event = ILS / (USD→ILS on the pay date): USD-native events round-trip
    # to their native amount, ILS/GBP cross-convert through the shekel correctly.
    def _usd(r: dict) -> float:
        if r["currency"] == "USD":
            return r["amount_native"]
        return r["amount_ils"] / latest_fx("USD", "ILS", r["date"])

    # ----- 13b-i. Dividends by year (received per year + current-year got/will-get) -----
    # div_past already merges recorded + absorbed + estimated, so summing it by
    # calendar year is every dividend that actually landed. USD via _usd (each
    # event's own pay-date FX); ILS is the historical ILS already on the row.
    year_usd: dict[int, float] = {}
    year_ils: dict[int, float] = {}
    for r in div_past:
        yr = int(r["date"][:4])
        year_usd[yr] = year_usd.get(yr, 0.0) + _usd(r)
        year_ils[yr] = year_ils.get(yr, 0.0) + r["amount_ils"]

    cur_year = today.year
    years_shown = sorted(set(year_usd) | {cur_year}, reverse=True)[:5]
    has_div_signal = any(year_usd.get(y) for y in years_shown) or will_get_usd or will_get_ils
    if years_shown and has_div_signal:
        summary_rows = []
        for y in years_shown:
            if y == cur_year:
                wg_usd_cell = money_td(will_get_usd, "USD", decimals=2)
                wg_ils_cell = money_td(will_get_ils, "ILS", decimals=2)
                row_attr = ' class="dividend-year-current"'
            else:
                wg_usd_cell = '<td class="num muted">—</td>'
                wg_ils_cell = '<td class="num muted">—</td>'
                row_attr = ""
            summary_rows.append(
                f'<tr{row_attr}><td class="num">{y}</td>'
                f'{money_td(year_usd.get(y, 0.0), "USD", decimals=2)}'
                f'{money_td(year_ils.get(y, 0.0), "ILS", decimals=2)}'
                f'{wg_usd_cell}{wg_ils_cell}</tr>'
            )
        div_yearly_summary_html = (
            '<div class="dividend-yearly"><p class="chart-title">By year</p>'
            + render_ledger(
                [("Year", "num"), ("Got", "num"), ("(ILS)", "num"),
                 ("Will get", "num"), ("(ILS)", "num")],
                summary_rows,
            )
            + "</div>"
        )
    else:
        div_yearly_summary_html = ""

    if div_past:
        first_dt = date.fromisoformat(min(r["date"] for r in div_past))
    else:
        first_dt = today.replace(day=1)
    div_months: list[str] = []
    y, m = first_dt.year, first_dt.month
    while (y, m) <= (today.year, today.month):
        div_months.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            m = 1
            y += 1
    month_idx = {ym: i for i, ym in enumerate(div_months)}

    # Rank payers by all-time USD; the 5 biggest keep their own band, the rest fold
    # into "Other" — a stable set, so the bar colors never reshuffle between views.
    usd_by_ticker: dict[str, float] = {}
    for r in div_past:
        usd_by_ticker[r["ticker"]] = usd_by_ticker.get(r["ticker"], 0.0) + _usd(r)
    top5 = [t for t, _ in sorted(usd_by_ticker.items(), key=lambda kv: kv[1], reverse=True)[:5]]
    series_order = top5 + (["Other"] if len(usd_by_ticker) > len(top5) else [])
    series_data = {name: [0.0] * len(div_months) for name in series_order}
    for r in div_past:
        idx = month_idx.get(r["date"][:7])
        if idx is None:
            continue
        series_data[r["ticker"] if r["ticker"] in top5 else "Other"][idx] += _usd(r)
    div_series = [
        {"ticker": name, "data": [round(v, 2) for v in series_data[name]]}
        for name in series_order
    ]

    # HTML tables
    def _div_row(r: dict, with_status: bool) -> str:
        disp_ccy = "ILS" if r["currency"] in ("ILS", "ILA") else r["currency"]
        status_cell = ""
        if with_status:
            st = r.get("status", "recorded")
            if st == "recorded" and r.get("confirmed"):
                status_cell = '<td class="muted" title="Itemized from a brokerage statement">✓ recorded</td>'
            elif st == "recorded":
                status_cell = '<td class="muted" title="Itemized — live estimate, not yet on a statement">recorded</td>'
            elif st == "absorbed":
                status_cell = '<td class="muted" title="Paid on/before your last brokerage snapshot — already folded into that cash balance (not double-counted)"><em>absorbed</em></td>'
            else:
                status_cell = '<td class="muted" title="Live Yahoo estimate — paid after your last snapshot, not yet reconciled"><em>est.</em></td>'
        confirmed_recorded = r.get("status") == "recorded" and r.get("confirmed", True)
        row_cls = "" if confirmed_recorded else "dividend-est"
        return (
            f'<tr class="{row_cls}"><td class="num">{h(r["date"])}</td>'
            f'<td>{h(r["ticker"])}</td>'
            f'{money_td(r["amount_native"], disp_ccy, decimals=2)}'
            f'{money_td(r["amount_ils"], "ILS", decimals=2)}'
            f'{status_cell}</tr>'
        )

    div_headers_received = [
        ("Date", "num"), ("Ticker", ""),
        ("Net", "num"), ("Net (ILS)", "num"), ("", ""),
    ]
    div_headers_upcoming = [
        ("Ex-date", "num"), ("Ticker", ""),
        ("Est. net", "num"), ("Est. (ILS)", "num"),
    ]

    if div_past:
        div_recent_5_html = render_ledger(
            div_headers_received,
            [_div_row(r, with_status=True) for r in div_past[:5]],
        )
    else:
        div_recent_5_html = (
            '<p class="footnote"><em>No dividend history — no dividend-paying holdings, or Yahoo data was unavailable this run.</em></p>'
        )

    one_year_ago = (today - _td(days=365)).isoformat()
    div_1y = [r for r in div_past if r["date"] >= one_year_ago]
    if div_1y:
        div_recent_1y_html = render_ledger(
            div_headers_received,
            [_div_row(r, with_status=True) for r in div_1y],
            footer=(
                f'<tr><td>{len(div_1y)} dividends · last 1Y</td><td></td><td></td>'
                f'{money_td(sum(r["amount_ils"] for r in div_1y), "ILS", decimals=2)}<td></td></tr>'
            ),
        )
    else:
        div_recent_1y_html = (
            '<p class="footnote"><em>No dividends in the last year.</em></p>'
        )

    if div_upcoming:
        div_upcoming_html = render_ledger(
            div_headers_upcoming,
            [
                (
                    f'<tr><td class="num">{h(u["date"])}</td>'
                    f'<td>{h(u["ticker"])}</td>'
                    f'{money_td(u["amount_native"], u["currency"], decimals=2)}'
                    f'{money_td(u["amount_ils"], "ILS", decimals=2)}</tr>'
                )
                for u in div_upcoming
            ],
        )
    else:
        div_upcoming_html = (
            '<p class="footnote"><em>No upcoming dividends projected.</em></p>'
        )

    dividends_chart_payload = {
        "months": div_months,
        "currency": "USD",
        "series": div_series,
        "upcoming": [
            {"date": u["date"], "ticker": u["ticker"], "amount_ils": round(u["amount_ils"], 2)}
            for u in div_upcoming
        ],
    }

    chart_data = {
        "netWorth": {
            "dates": nw_dates,
            "values": nw_series_values,
            "breakdowns": nw_breakdowns,
        },
        "stocksVsSpy": {
            "dates": sv_dates, "stocks": sv_stocks, "spy": sv_spy,
            "cumulativeDeposits": sv_deposits,
        },
        "payslips": {"months": pay_months, "gross": pay_gross, "net": pay_net},
        "expenses": expenses_chart_payload,
        "fxDeposits": fx_deposits_payload,
        "dividends": dividends_chart_payload,
    }
    chart_data_json = json.dumps(chart_data, ensure_ascii=False)

    # ----- 14. Assemble HTML -----

    template = (TEMPLATE_DIR / "dashboard.html.tpl").read_text(encoding="utf-8")
    styles_css = (TEMPLATE_DIR / "styles.css").read_text(encoding="utf-8")
    charts_js_app = (TEMPLATE_DIR / "charts.js").read_text(encoding="utf-8")
    chart_js = (VENDOR_DIR / "chart.umd.min.js").read_text(encoding="utf-8")
    chart_adapter_js = (VENDOR_DIR / "chartjs-adapter-date-fns.bundle.min.js").read_text(encoding="utf-8")
    fonts_inline_css = (VENDOR_DIR / "fonts-inline.css").read_text(encoding="utf-8")

    # Drift banner — compare snapshot-pair deltas against captured flows.
    # Same logic as scripts/reconcile_hafenix.py; only the most recent pair per
    # currency surfaces, and only if drift exceeds the threshold.
    drift_thresholds = {"USD": 50.0, "GBP": 50.0, "ILS": 200.0}
    drift_msgs: list[str] = []
    for ccy in ("USD", "GBP", "ILS"):
        component = f"cash_{ccy.lower()}"
        snaps = cur.execute(
            """SELECT as_of, amount_minor/100.0 AS amount FROM balances
               WHERE account_id=? AND component=?
               ORDER BY as_of ASC""",
            (primary_brokerage_id, component),
        ).fetchall()
        if len(snaps) < 2:
            continue
        prev, cur_snap = snaps[-2], snaps[-1]
        txn_sum = cur.execute(
            """SELECT COALESCE(SUM(amount_minor),0)/100.0 AS t FROM transactions
               WHERE account_id=? AND currency=? AND date>? AND date<=?""",
            (primary_brokerage_id, ccy, prev["as_of"], cur_snap["as_of"]),
        ).fetchone()["t"]
        buy_sum = cur.execute(
            """SELECT COALESCE(SUM(shares*price_minor + fees_minor),0)/100.0 AS t
               FROM trades
               WHERE account_id=? AND side='buy' AND currency=?
                 AND date>? AND date<=?""",
            (primary_brokerage_id, ccy, prev["as_of"], cur_snap["as_of"]),
        ).fetchone()["t"]
        captured = txn_sum - buy_sum
        actual = cur_snap["amount"] - prev["amount"]
        diff = actual - captured
        if abs(diff) > drift_thresholds[ccy]:
            sign = "+" if diff >= 0 else ""
            drift_msgs.append(f"{ccy} {sign}{diff:,.0f}")
    drift_banner_html = ""
    if drift_msgs:
        drift_banner_html = (
            '<div class="drift-banner" style="margin-top:10px;padding:6px 0;'
            'font-style:italic;font-size:13px;color:var(--muted-ink)">'
            f'{html.escape(accounts[primary_brokerage_id]["name"])} drift since last snapshot: {html.escape(" · ".join(drift_msgs))} — likely a missing FX / deposit / sell doc.'
            '</div>'
        )

    substitutions = {
        "AS_OF_DATE": as_of,
        "NET_WORTH_ILS": net_worth_str,
        "DRIFT_BANNER": drift_banner_html,
        "OVERVIEW_TABLE": overview_table,
        "FLOW_SUMMARY": flow_summary_html,
        "OVERVIEW_FOOTNOTE": overview_footnote,
        "POSITIONS_TABLE": positions_table,
        "IBKR_POSITIONS_TABLE": ibkr_positions_table,
        "DIVIDENDS_YEARLY_SUMMARY": div_yearly_summary_html,
        "DIVIDENDS_RECENT_5_TABLE": div_recent_5_html,
        "DIVIDENDS_RECENT_1Y_TABLE": div_recent_1y_html,
        "DIVIDENDS_UPCOMING_TABLE": div_upcoming_html,
        "DEPOSITS_TABLE": deposits_table,
        "TRADES_TABLE": trades_table,
        "PAYSLIPS_TABLE": payslips_table,
        "EXPENSES_TABLE": expenses_table,
        "MTD_EXPENSE_NOTE": mtd_note_html,
        "SQLITE_TABS": sqlite_tabs,
        "CHART_DATA_JSON": chart_data_json,
        "STYLES_CSS": styles_css,
        "CHARTS_JS_APP": charts_js_app,
        "CHART_JS": chart_js,
        "CHART_ADAPTER_JS": chart_adapter_js,
        "FONTS_INLINE_CSS": fonts_inline_css,
    }

    out = template
    for k, v in substitutions.items():
        out = out.replace("{{" + k + "}}", v)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(out, encoding="utf-8")

    # ----- Report -----

    size = OUTPUT_PATH.stat().st_size
    n_priced = len(prices_by_security_id)
    n_live_priced = n_priced - len(stored_price_fallbacks)
    print(f"WROTE: {OUTPUT_PATH} ({size:,} bytes)")
    print(f"AS_OF: {as_of}")
    print(f"NET_WORTH: {net_worth_str}")
    print(
        f"PRICED: {n_priced} positions "
        f"({n_live_priced} live, {len(stored_price_fallbacks)} stored), "
        f"2 FX rates (USD/ILS={fx_usd_ils:.4f}, GBP/ILS={fx_gbp_ils:.4f})"
    )
    print(f"BROKERAGE: market_value={brokerage_market_value_ils:,.0f} ILS, "
          f"cost_basis={brokerage_cost_basis_ils:,.0f} ILS, "
          f"deposits={brokerage_usd_deposits:,.2f} USD")
    if failed_tickers:
        print(f"LIVE PRICE FAILED: {', '.join(failed_tickers)}")
    if stored_price_fallbacks:
        stored_desc = ", ".join(
            f"{item['ticker']}@{item['date']}" for item in stored_price_fallbacks
        )
        print(f"USED STORED PRICES: {stored_desc}")
    if cost_basis_fallback_tickers:
        print(f"COST BASIS FALLBACK: {', '.join(cost_basis_fallback_tickers)}")

    # Meta for callers (e.g., Telegram step).
    meta = {
        "as_of": as_of,
        "net_worth_text": net_worth_str,
        "output_path": str(OUTPUT_PATH),
        "failed_tickers": failed_tickers,
        "stored_price_fallbacks": stored_price_fallbacks,
        "cost_basis_fallback_tickers": cost_basis_fallback_tickers,
        "n_priced": n_priced,
        "n_live_priced": n_live_priced,
    }
    Path("/tmp/dashboard_meta.json").write_text(json.dumps(meta), encoding="utf-8")

    con.close()
    return 0


def build_fx_deposits_payload(cur: sqlite3.Cursor, primary_brokerage_id: Optional[int]) -> dict:
    """USD/ILS + GBP/ILS history with brokerage-deposit markers per currency.

    Lines come from `fx_rates` (already refreshed live this run). Markers come
    from positive `deposit`/`transfer` transactions on the trade-fed brokerage;
    ILS deposits are omitted (no FX event to mark on the chart).
    """
    rows = cur.execute(
        """SELECT date, base_currency, rate FROM fx_rates
           WHERE quote_currency='ILS' AND base_currency IN ('USD','GBP')
           ORDER BY date"""
    ).fetchall()
    by_date_usd: dict[str, float] = {}
    by_date_gbp: dict[str, float] = {}
    for r in rows:
        if r["base_currency"] == "USD":
            by_date_usd[r["date"]] = r["rate"]
        else:
            by_date_gbp[r["date"]] = r["rate"]
    all_dates = sorted(set(by_date_usd.keys()) | set(by_date_gbp.keys()))
    usd_series = [by_date_usd.get(d) for d in all_dates]
    gbp_series = [by_date_gbp.get(d) for d in all_dates]

    dep_rows = cur.execute(
        """SELECT date, amount_minor/100.0 AS amount, currency, counterparty, description
           FROM transactions
           WHERE account_id=? AND category IN ('deposit','transfer') AND amount_minor>0 AND currency IN ('USD','GBP')
           ORDER BY date""",
        (primary_brokerage_id,)
    ).fetchall()
    usd_deps: list[dict] = []
    gbp_deps: list[dict] = []
    for r in dep_rows:
        item = {
            "date": r["date"],
            "amount": round(r["amount"], 2),
            "counterparty": r["counterparty"] or "",
            "note": r["description"] or "",
        }
        if r["currency"] == "USD":
            usd_deps.append(item)
        else:
            gbp_deps.append(item)

    return {
        "dates": all_dates,
        "usd": usd_series,
        "gbp": gbp_series,
        "usdDeposits": usd_deps,
        "gbpDeposits": gbp_deps,
    }


def render_ledger(headers: list[tuple[str, str]], rows: list[str],
                  footer: Optional[str] = None) -> str:
    parts = ['<table class="ledger"><thead><tr>']
    for label, cls in headers:
        parts.append(f'<th class="{cls}">{h(label)}</th>')
    parts.append("</tr></thead><tbody>")
    parts.extend(rows)
    parts.append("</tbody>")
    if footer:
        parts.append(f"<tfoot>{footer}</tfoot>")
    parts.append("</table>")
    return "".join(parts)


if __name__ == "__main__":
    sys.exit(main())
