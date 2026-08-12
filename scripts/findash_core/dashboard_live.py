"""Build the localhost dashboard snapshot with cached live market overlays."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
import sqlite3

from findash_core.currencies import (
    canonical_currency,
    convert_minor,
    major_to_minor,
    minor_unit_factor,
)
from findash_core.errors import MissingRateError
from findash_core.dashboard_model import DashboardModelBuilder, major
from findash_core.db import connect_db


def _latest_security_quotes(connection: sqlite3.Connection) -> dict[int, dict]:
    rows = connection.execute(
        """SELECT observation.security_id,security.ticker,
                  observation.price_minor,observation.currency,
                  observation.observed_at,observation.source
             FROM security_price_observations AS observation
             JOIN securities AS security ON security.id=observation.security_id
            WHERE observation.id=(
                  SELECT candidate.id
                    FROM security_price_observations AS candidate
                   WHERE candidate.security_id=observation.security_id
                   ORDER BY candidate.observed_at DESC,candidate.id DESC
                   LIMIT 1
            )
            ORDER BY security.ticker"""
    ).fetchall()
    return {
        int(row["security_id"]): {
            "ticker": str(row["ticker"]),
            "price_minor": int(row["price_minor"]),
            "currency": canonical_currency(str(row["currency"])),
            "observed_at": str(row["observed_at"]),
            "source": str(row["source"]),
        }
        for row in rows
    }


def _latest_fx_quotes(
    connection: sqlite3.Connection,
) -> dict[tuple[str, str], dict]:
    rows = connection.execute(
        """SELECT observation.base_currency,observation.quote_currency,
                  observation.rate,observation.observed_at,observation.source
             FROM fx_rate_observations AS observation
            WHERE observation.id=(
                  SELECT candidate.id
                    FROM fx_rate_observations AS candidate
                   WHERE candidate.base_currency=observation.base_currency
                     AND candidate.quote_currency=observation.quote_currency
                   ORDER BY candidate.observed_at DESC,candidate.id DESC
                   LIMIT 1
            )
            ORDER BY observation.base_currency,observation.quote_currency"""
    ).fetchall()
    return {
        (
            canonical_currency(str(row["base_currency"])),
            canonical_currency(str(row["quote_currency"])),
        ): {
            "rate": Decimal(str(row["rate"])),
            "observed_at": str(row["observed_at"]),
            "source": str(row["source"]),
        }
        for row in rows
    }


def _live_fx_rate(
    quotes: dict[tuple[str, str], dict], source: str, target: str
) -> tuple[Decimal, str] | None:
    source = canonical_currency(source)
    target = canonical_currency(target)
    if source == target:
        return Decimal("1"), "identity"
    direct = quotes.get((source, target))
    if direct:
        return direct["rate"], direct["observed_at"]
    inverse = quotes.get((target, source))
    if inverse and inverse["rate"] > 0:
        return Decimal("1") / inverse["rate"], inverse["observed_at"]
    return None


def _usd_rate(
    builder: DashboardModelBuilder,
    quotes: dict[tuple[str, str], dict],
    source: str,
    reporting_currency: str,
    as_of: str,
) -> Decimal | None:
    """Resolve a current USD cross without inventing a missing market rate."""

    source = canonical_currency(source)
    if source == "USD":
        return Decimal("1")
    direct = _live_fx_rate(quotes, source, "USD")
    if direct:
        return direct[0]
    source_reporting = _live_fx_rate(quotes, source, reporting_currency)
    usd_reporting = _live_fx_rate(quotes, "USD", reporting_currency)
    if source_reporting and usd_reporting:
        return source_reporting[0] / usd_reporting[0]
    try:
        return builder.accounting.fx.rate(source, "USD", as_of)
    except MissingRateError:
        try:
            source_rate = builder.accounting.fx.rate(
                source, reporting_currency, as_of
            )
            usd_rate = builder.accounting.fx.rate(
                "USD", reporting_currency, as_of
            )
            return source_rate / usd_rate
        except MissingRateError:
            return None


def _security_session(
    connection: sqlite3.Connection,
    security_id: int,
    as_of: str,
    current_price_minor: int | None,
    currency: str,
) -> dict:
    """Return the latest market session, falling back to its daily closes."""

    daily = connection.execute(
        """SELECT date,close_minor,currency
             FROM prices
            WHERE security_id=? AND date<=?
            ORDER BY date DESC
            LIMIT 2""",
        (security_id, as_of),
    ).fetchall()
    if not daily:
        return {"date": None, "prices": [], "previous_close_minor": None}

    latest = daily[0]
    session_date = str(latest["date"])
    session_currency = canonical_currency(str(latest["currency"]))
    if session_currency != canonical_currency(currency):
        return {"date": session_date, "prices": [], "previous_close_minor": None}

    previous_close = int(daily[1]["close_minor"]) if len(daily) > 1 else None
    observations = connection.execute(
        """SELECT observed_at,price_minor
             FROM security_price_observations
            WHERE security_id=?
              AND observed_at>=? || ' 00:00:00'
              AND observed_at<datetime(? || ' 00:00:00','+1 day')
              AND currency=?
            ORDER BY observed_at,id""",
        (security_id, session_date, session_date, session_currency),
    ).fetchall()
    values_minor = [int(row["price_minor"]) for row in observations]
    previous_session = session_date < as_of
    # A quote observed while the market is closed only repeats the last traded
    # value. Anchor movement to the completed session's official close so a new
    # calendar day cannot create an empty or misleading "today" view.
    effective_current = (
        int(latest["close_minor"])
        if previous_session
        else (
            current_price_minor
            if current_price_minor is not None
            else int(latest["close_minor"])
        )
    )
    if not values_minor or values_minor[-1] != effective_current:
        values_minor.append(effective_current)
    if previous_close is not None:
        values_minor.insert(0, previous_close)

    return {
        "date": session_date,
        "is_previous": previous_session,
        "prices": [
            value / minor_unit_factor(session_currency) for value in values_minor
        ],
        "previous_close_minor": previous_close,
        "current_price_minor": effective_current,
    }


def _meta_value(connection: sqlite3.Connection, key: str) -> str | None:
    row = connection.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return str(row[0]) if row and row[0] else None


def build_live_dashboard_snapshot(db: Path, as_of: str) -> dict:
    """Return a JSON-ready dashboard model from SQLite only.

    Daily valuation remains the deterministic baseline. The latest append-only
    quote observations may update current security and FX values for the local
    app; historical charts continue to use the daily caches.
    """

    connection = connect_db(db, readonly=True, migrate=False)
    connection.row_factory = sqlite3.Row
    try:
        builder = DashboardModelBuilder(connection)
        model = builder.build(as_of)
        state = model.pop("state")
        security_quotes = _latest_security_quotes(connection)
        fx_quotes = _latest_fx_quotes(connection)
        reporting_currency = model["reporting_currency"]

        account_deltas: dict[int, Decimal] = defaultdict(lambda: Decimal("0"))
        live_positions: dict[tuple[int, int], dict] = {}
        for row in state.rows:
            native_minor = row.native_value_minor
            quote = (
                security_quotes.get(row.security_id)
                if row.security_id is not None
                else None
            )
            price_observed_at = None
            if quote and row.quantity is not None and quote["currency"] == row.currency:
                native_minor = (
                    row.quantity * Decimal(quote["price_minor"])
                ).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
                price_observed_at = quote["observed_at"]

            fx_point = _live_fx_rate(fx_quotes, row.currency, reporting_currency)
            if fx_point:
                live_reporting_minor = Decimal(
                    convert_minor(
                        int(native_minor),
                        row.currency,
                        reporting_currency,
                        fx_point[0],
                    )
                )
                fx_observed_at = fx_point[1]
            else:
                live_reporting_minor = builder.accounting.fx.convert_minor(
                    native_minor, row.currency, reporting_currency, as_of
                ).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
                fx_observed_at = None

            account_deltas[row.account_id] += (
                live_reporting_minor - row.reporting_value_minor
            )
            if row.security_id is not None:
                live_positions[(row.account_id, row.security_id)] = {
                    "price_minor": int(quote["price_minor"]) if quote else None,
                    "currency": row.currency,
                    "native_minor": int(native_minor),
                    "reporting_minor": int(live_reporting_minor),
                    "price_observed_at": price_observed_at,
                    "fx_observed_at": fx_observed_at,
                }

        for bucket in model["overview"]["buckets"]:
            for account in bucket["accounts"]:
                cached = account["value"]
                delta = major(
                    account_deltas[int(account["account_id"])], reporting_currency
                )
                account["cached_value"] = cached
                account["live_delta"] = delta
                account["value"] = cached + delta
            bucket["cached_total"] = bucket["total"]
            bucket["total"] = sum(account["value"] for account in bucket["accounts"])

        cached_total = model["overview"]["total"]
        model["overview"]["cached_total"] = cached_total
        model["overview"]["total"] = sum(
            bucket["total"] for bucket in model["overview"]["buckets"]
        )
        model["overview"]["live_delta"] = (
            model["overview"]["total"] - cached_total
        )

        for position in model["positions"]:
            live = live_positions.get(
                (int(position["account_id"]), int(position["security_id"]))
            )
            position["cached_last_price"] = position["last_price"]
            position["cached_market_reporting"] = position["market_reporting"]
            if not live:
                position["quote_observed_at"] = None
                position["valuation_source"] = "daily close"
                continue
            if live["price_minor"] is not None:
                position["last_price"] = (
                    live["price_minor"] / minor_unit_factor(live["currency"])
                )
            position["market_native"] = (
                live["native_minor"] / minor_unit_factor(live["currency"])
            )
            position["market_reporting"] = (
                live["reporting_minor"] / minor_unit_factor(reporting_currency)
            )
            cost = position["cost_reporting"]
            pnl = position["market_reporting"] - cost if cost is not None else None
            position["pnl_reporting"] = pnl
            position["pnl_pct"] = (
                pnl / abs(cost) * 100 if cost not in (None, 0) else None
            )
            position["quote_observed_at"] = live["price_observed_at"]
            position["valuation_source"] = (
                "live quote" if live["price_observed_at"] else "live FX"
            )

        for position in model["positions"]:
            security_id = int(position["security_id"])
            currency = canonical_currency(position["currency"])
            quote = security_quotes.get(security_id)
            current_price_minor = (
                int(quote["price_minor"])
                if quote and quote["currency"] == currency
                else (
                    major_to_minor(position["last_price"], currency)
                    if position["last_price"] is not None
                    else None
                )
            )
            session = _security_session(
                connection,
                security_id,
                as_of,
                current_price_minor,
                currency,
            )
            position["session_date"] = session["date"]
            position["session_is_previous"] = session.get("is_previous", False)
            position["session_prices"] = session["prices"]
            previous_close = session["previous_close_minor"]
            effective_current = session.get("current_price_minor")
            change_minor = None
            if previous_close is not None and effective_current is not None:
                change_minor = (
                    Decimal(effective_current - previous_close)
                    * Decimal(str(position["quantity"]))
                ).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
                position["day_change_pct"] = (
                    float(
                        Decimal(effective_current - previous_close)
                        / Decimal(previous_close)
                        * Decimal("100")
                    )
                    if previous_close
                    else None
                )
            else:
                position["day_change_pct"] = None

            average_cost = position["average_cost"]
            pnl_native_minor = None
            if average_cost is None:
                position["pnl_usd_pct"] = None
            else:
                cost_native_minor = (
                    Decimal(str(position["quantity"]))
                    * Decimal(major_to_minor(average_cost, currency))
                ).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
                market_native_minor = major_to_minor(
                    position["market_native"], currency
                )
                pnl_native_minor = Decimal(market_native_minor) - cost_native_minor
                position["pnl_usd_pct"] = (
                    float(
                        pnl_native_minor
                        / abs(cost_native_minor)
                        * Decimal("100")
                    )
                    if cost_native_minor
                    else None
                )

            usd_rate = _usd_rate(
                builder, fx_quotes, currency, reporting_currency, as_of
            )
            if usd_rate is None:
                position["market_usd"] = None
                position["day_change_usd"] = None
                position["pnl_usd"] = None
            else:
                market_native_minor = major_to_minor(
                    position["market_native"], currency
                )
                position["market_usd"] = (
                    convert_minor(
                        market_native_minor, currency, "USD", usd_rate
                    )
                    / minor_unit_factor("USD")
                )
                position["day_change_usd"] = (
                    convert_minor(
                        int(change_minor), currency, "USD", usd_rate
                    )
                    / minor_unit_factor("USD")
                    if change_minor is not None
                    else None
                )
                if pnl_native_minor is None:
                    position["pnl_usd"] = None
                else:
                    position["pnl_usd"] = (
                        convert_minor(
                            int(pnl_native_minor), currency, "USD", usd_rate
                        )
                        / minor_unit_factor("USD")
                    )

        model["positions"].sort(
            key=lambda position: (
                position["market_usd"]
                if position["market_usd"] is not None
                else position["market_reporting"]
            ),
            reverse=True,
        )

        security_rows = [
            {
                "ticker": quote["ticker"],
                "price": quote["price_minor"]
                / minor_unit_factor(quote["currency"]),
                "currency": quote["currency"],
                "observed_at": quote["observed_at"],
                "source": quote["source"],
            }
            for quote in security_quotes.values()
        ]
        fx_rows = [
            {
                "base": base,
                "quote": quote,
                "rate": float(values["rate"]),
                "observed_at": values["observed_at"],
                "source": values["source"],
            }
            for (base, quote), values in fx_quotes.items()
        ]
        observed = [
            row["observed_at"] for row in security_rows + fx_rows
        ]
        model["market"] = {
            "securities": security_rows,
            "fx": fx_rows,
            "latest_observed_at": max(observed, default=None),
        }
        model["freshness"] = {
            "generated_at": datetime.now(timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z"),
            "last_sync_at": _meta_value(connection, "last_sync_at"),
            "market_observed_at": model["market"]["latest_observed_at"],
        }
        return model
    finally:
        connection.close()


__all__ = ["build_live_dashboard_snapshot"]
