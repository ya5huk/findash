"""Cached market-data migrations (versions 12-13)."""

from __future__ import annotations

import sqlite3

from .migration_validation import (
    _currency_check,
    _date_check,
    _integer_check,
    _install_validation_triggers,
)


def _security_distribution_validation_checks() -> list[str]:
    return [
        _date_check("event_date"),
        _integer_check("amount_per_share_minor_numerator")
        + " AND NEW.amount_per_share_minor_numerator>0",
        _integer_check("amount_per_share_minor_denominator")
        + " AND NEW.amount_per_share_minor_denominator>0",
        _currency_check("currency"),
        "typeof(NEW.source)='text' AND length(trim(NEW.source))>0",
        "typeof(NEW.source_key)='text' AND length(trim(NEW.source_key))>0",
        "typeof(NEW.observed_at)='text' AND length(NEW.observed_at)=19 "
        "AND datetime(NEW.observed_at)=NEW.observed_at",
    ]


def _security_distribution_refresh_validation_checks() -> list[str]:
    return [
        "typeof(NEW.source)='text' AND length(trim(NEW.source))>0",
        "typeof(NEW.status)='text' "
        "AND NEW.status IN ('success','empty','failed')",
        "typeof(NEW.observed_at)='text' AND length(NEW.observed_at)=19 "
        "AND datetime(NEW.observed_at)=NEW.observed_at",
    ]


def _migration_12_security_distributions(connection: sqlite3.Connection) -> None:
    """Cache exact per-share market distributions without REAL money."""

    connection.execute(
        """CREATE TABLE IF NOT EXISTS security_distributions (
             id                                         INTEGER PRIMARY KEY,
             security_id                                INTEGER NOT NULL
                                                          REFERENCES securities(id)
                                                          ON DELETE CASCADE,
             event_date                                 TEXT NOT NULL,
             amount_per_share_minor_numerator           INTEGER NOT NULL
                                                          CHECK (amount_per_share_minor_numerator > 0),
             amount_per_share_minor_denominator         INTEGER NOT NULL
                                                          CHECK (amount_per_share_minor_denominator > 0),
             currency                                   TEXT NOT NULL,
             source                                     TEXT NOT NULL
                                                          CHECK (length(trim(source)) > 0),
             source_key                                 TEXT NOT NULL
                                                          CHECK (length(trim(source_key)) > 0),
             observed_at                                TEXT NOT NULL
                                                          DEFAULT CURRENT_TIMESTAMP,
             UNIQUE (security_id, source, source_key)
           )"""
    )
    connection.execute(
        """CREATE INDEX IF NOT EXISTS idx_security_distributions_security_date
           ON security_distributions(security_id, event_date)"""
    )
    _install_validation_triggers(
        connection,
        "security_distributions",
        _security_distribution_validation_checks(),
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS security_distribution_refreshes (
             security_id  INTEGER NOT NULL
                            REFERENCES securities(id) ON DELETE CASCADE,
             source       TEXT NOT NULL CHECK (length(trim(source)) > 0),
             status       TEXT NOT NULL
                            CHECK (status IN ('success','empty','failed')),
             observed_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
             PRIMARY KEY (security_id, source)
           )"""
    )
    _install_validation_triggers(
        connection,
        "security_distribution_refreshes",
        _security_distribution_refresh_validation_checks(),
    )


def _security_price_observation_validation_checks() -> list[str]:
    return [
        _integer_check("price_minor") + " AND NEW.price_minor>=0",
        _currency_check("currency"),
        "typeof(NEW.source)='text' AND length(trim(NEW.source))>0",
        "typeof(NEW.observed_at)='text' AND length(NEW.observed_at)=19 "
        "AND datetime(NEW.observed_at)=NEW.observed_at",
    ]


def _fx_rate_observation_validation_checks() -> list[str]:
    return [
        _currency_check("base_currency"),
        _currency_check("quote_currency"),
        "NEW.base_currency<>NEW.quote_currency",
        "typeof(NEW.rate) IN ('integer','real') AND NEW.rate>0 AND NEW.rate<1e999",
        "typeof(NEW.source)='text' AND length(trim(NEW.source))>0",
        "typeof(NEW.observed_at)='text' AND length(NEW.observed_at)=19 "
        "AND datetime(NEW.observed_at)=NEW.observed_at",
    ]


def _migration_13_market_observations(connection: sqlite3.Connection) -> None:
    """Append live market observations without weakening daily close history."""

    connection.execute(
        """CREATE TABLE IF NOT EXISTS security_price_observations (
             id           INTEGER PRIMARY KEY,
             security_id  INTEGER NOT NULL
                            REFERENCES securities(id) ON DELETE CASCADE,
             observed_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
             price_minor  INTEGER NOT NULL CHECK (price_minor >= 0),
             currency     TEXT NOT NULL,
             source       TEXT NOT NULL CHECK (length(trim(source)) > 0),
             UNIQUE (security_id, observed_at, source)
           )"""
    )
    connection.execute(
        """CREATE INDEX IF NOT EXISTS idx_security_price_observations_latest
           ON security_price_observations(security_id, observed_at DESC)"""
    )
    _install_validation_triggers(
        connection,
        "security_price_observations",
        _security_price_observation_validation_checks(),
    )

    connection.execute(
        """CREATE TABLE IF NOT EXISTS fx_rate_observations (
             id              INTEGER PRIMARY KEY,
             base_currency   TEXT NOT NULL,
             quote_currency  TEXT NOT NULL,
             observed_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
             rate            REAL NOT NULL CHECK (rate > 0),
             source          TEXT NOT NULL CHECK (length(trim(source)) > 0),
             CHECK (base_currency <> quote_currency),
             UNIQUE (base_currency, quote_currency, observed_at, source)
           )"""
    )
    connection.execute(
        """CREATE INDEX IF NOT EXISTS idx_fx_rate_observations_latest
           ON fx_rate_observations(base_currency, quote_currency, observed_at DESC)"""
    )
    _install_validation_triggers(
        connection,
        "fx_rate_observations",
        _fx_rate_observation_validation_checks(),
    )
