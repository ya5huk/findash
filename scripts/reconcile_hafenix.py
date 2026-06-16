#!/usr/bin/env python3
"""Reconcile Hafenix cash snapshots against derived flows.

Snapshots in `balances` (component='cash_usd'|'cash_gbp'|'cash_ils') are the
ground truth — same pattern as bank running-balance rows. Between two snapshots
the *expected* balance delta equals the net of recorded flows (transactions on
account 7 in that currency, minus buy trades' cash-leg in that currency). Any
drift between the actual snapshot-to-snapshot delta and the recorded-flow delta
indicates missing source documents (an unrecorded FX, deposit, sell, fee, etc.).

Exits non-zero if any pair's drift exceeds the per-currency threshold.

Stocks reconciliation is intentionally out of scope: visual inspection of the
dashboard's Investments table against the Hafenix app is the canonical check.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "finance.db"

HAFENIX_ACCOUNT_ID = 7
CURRENCIES = ("USD", "GBP", "ILS")
DRIFT_THRESHOLD = {"USD": 50.0, "GBP": 50.0, "ILS": 200.0}


def snapshots(cur: sqlite3.Cursor, ccy: str) -> list[dict]:
    component = f"cash_{ccy.lower()}"
    rows = cur.execute(
        """SELECT as_of, amount_minor/100.0 AS amount
           FROM balances
           WHERE account_id=? AND component=?
           ORDER BY as_of ASC""",
        (HAFENIX_ACCOUNT_ID, component),
    ).fetchall()
    return [dict(r) for r in rows]


def captured_flow(cur: sqlite3.Cursor, ccy: str, lo: str, hi: str) -> float:
    """Net captured flow in `ccy` on account 7 in the half-open interval (lo, hi].

    Positive = inflow (deposits, sell proceeds already net of tax/fee, FX-in).
    Negative = outflow (FX-out, fees, withdrawals).
    Subtracts buy-trade cash-legs (not present in `transactions`).
    """
    txn = cur.execute(
        """SELECT COALESCE(SUM(amount_minor),0)/100.0 AS t FROM transactions
           WHERE account_id=? AND currency=? AND date>? AND date<=?""",
        (HAFENIX_ACCOUNT_ID, ccy, lo, hi),
    ).fetchone()["t"]
    buys = cur.execute(
        """SELECT COALESCE(SUM(shares*price_minor + fees_minor),0)/100.0 AS t
           FROM trades
           WHERE account_id=? AND side='buy' AND currency=?
             AND date>? AND date<=?""",
        (HAFENIX_ACCOUNT_ID, ccy, lo, hi),
    ).fetchone()["t"]
    return txn - buys


def main() -> int:
    if not DB.exists():
        print(f"ERROR: {DB} not found", file=sys.stderr)
        return 1
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    any_blocking = False
    any_snapshot = False
    rows_out: list[tuple[str, ...]] = []

    for ccy in CURRENCIES:
        snaps = snapshots(cur, ccy)
        if not snaps:
            rows_out.append((f"{ccy}", "—", "—", "—", "—", "no snapshots yet"))
            continue
        any_snapshot = True
        if len(snaps) == 1:
            s = snaps[0]
            rows_out.append((
                f"{ccy}",
                f"{s['as_of']}",
                "—",
                f"{s['amount']:,.2f}",
                "—",
                "only 1 snapshot, need ≥2 to reconcile",
            ))
            continue
        for prev, cur_snap in zip(snaps, snaps[1:]):
            captured = captured_flow(cur, ccy, prev["as_of"], cur_snap["as_of"])
            actual = cur_snap["amount"] - prev["amount"]
            drift = actual - captured
            blocking = abs(drift) > DRIFT_THRESHOLD[ccy]
            hint = "missing flow doc?" if blocking else "ok"
            if blocking:
                any_blocking = True
            rows_out.append((
                f"{ccy}",
                f"{prev['as_of']} → {cur_snap['as_of']}",
                f"{captured:+,.2f}",
                f"{actual:+,.2f}",
                f"{drift:+,.2f}",
                hint,
            ))

    if not any_snapshot:
        print("No Hafenix cash snapshots in the database yet.")
        print("Upload a Hafenix balance screenshot (see docs/doc-types.md) and run `sync finance` to add one.")
        return 0

    headers = ("Ccy", "Pair", "Captured Δ", "Actual Δ", "Drift", "Hint")
    widths = [max(len(h), max((len(r[i]) for r in rows_out), default=0)) for i, h in enumerate(headers)]
    sep = "  "
    def line(cells):
        return sep.join(c.ljust(widths[i]) for i, c in enumerate(cells))
    print(line(headers))
    print(sep.join("-" * w for w in widths))
    for r in rows_out:
        print(line(r))
    print()
    if any_blocking:
        print("DRIFT exceeds threshold on one or more pairs — likely missing FX/deposit/sell documents.", file=sys.stderr)
        return 2
    print("All reconciled pairs within tolerance.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
