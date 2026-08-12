"""Legacy baseline and normalized-evidence migrations (versions 1-2)."""

from __future__ import annotations

from collections import defaultdict
import sqlite3

from .migration_support import MigrationError, _add_column


def _migration_1_legacy_baseline(connection: sqlite3.Connection) -> None:
    """Fold the pre-migration, opportunistic schema guards into one baseline."""

    connection.execute(
        "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    _add_column(connection, "accounts", "live_source", "TEXT")
    _add_column(connection, "trades", "external_id", "TEXT")
    connection.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS idx_trades_external
           ON trades(account_id, external_id) WHERE external_id IS NOT NULL"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS positions (
             id                  INTEGER PRIMARY KEY,
             account_id          INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
             security_id         INTEGER NOT NULL REFERENCES securities(id) ON DELETE CASCADE,
             as_of               TEXT NOT NULL,
             quantity            REAL NOT NULL,
             avg_cost_minor      INTEGER,
             market_price_minor  INTEGER,
             market_value_minor  INTEGER,
             currency            TEXT NOT NULL,
             source_doc_id       INTEGER REFERENCES documents(id) ON DELETE SET NULL,
             UNIQUE(account_id, security_id, as_of)
           )"""
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_positions_account_date ON positions(account_id, as_of)"
    )


def _create_event_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """CREATE TABLE IF NOT EXISTS account_feeds (
             id                    INTEGER PRIMARY KEY,
             account_id            INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
             provider              TEXT NOT NULL,
             feed_type             TEXT NOT NULL CHECK (feed_type IN ('document','api','manual')),
             external_account_ref  TEXT,
             label                 TEXT,
             is_active             INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0,1)),
             created_at            TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
           )"""
    )
    connection.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS uq_account_feeds_provider_ref
           ON account_feeds(provider, external_account_ref)
           WHERE external_account_ref IS NOT NULL"""
    )
    connection.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS uq_account_feeds_default
           ON account_feeds(account_id, provider)
           WHERE external_account_ref IS NULL"""
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_account_feeds_account ON account_feeds(account_id)"
    )

    connection.execute(
        """CREATE TABLE IF NOT EXISTS canonical_events (
             id              INTEGER PRIMARY KEY,
             account_id      INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
             event_type      TEXT NOT NULL CHECK (event_type IN ('cash','trade')),
             occurred_on     TEXT NOT NULL,
             currency        TEXT NOT NULL,
             amount_minor    INTEGER,
             flow_type       TEXT CHECK (flow_type IN
                               ('expense','income','transfer','investment','tax','refund','other')),
             category        TEXT,
             security_id     INTEGER REFERENCES securities(id) ON DELETE RESTRICT,
             side            TEXT CHECK (side IN ('buy','sell')),
             quantity        REAL,
             price_minor     INTEGER,
             fees_minor      INTEGER NOT NULL DEFAULT 0,
             status          TEXT NOT NULL DEFAULT 'confirmed'
                               CHECK (status IN ('confirmed','needs_review','excluded')),
             notes           TEXT,
             created_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
             CHECK (
               (event_type='cash'
                AND amount_minor IS NOT NULL
                AND flow_type IS NOT NULL
                AND security_id IS NULL AND side IS NULL
                AND quantity IS NULL AND price_minor IS NULL)
               OR
               (event_type='trade'
                AND amount_minor IS NULL
                AND flow_type IS NULL
                AND security_id IS NOT NULL AND side IS NOT NULL
                AND quantity IS NOT NULL AND quantity > 0
                AND price_minor IS NOT NULL AND price_minor >= 0)
             )
           )"""
    )
    connection.execute(
        """CREATE INDEX IF NOT EXISTS idx_canonical_events_account_date
           ON canonical_events(account_id, occurred_on)"""
    )
    connection.execute(
        """CREATE INDEX IF NOT EXISTS idx_canonical_events_security_date
           ON canonical_events(security_id, occurred_on)
           WHERE event_type='trade'"""
    )

    connection.execute(
        """CREATE TABLE IF NOT EXISTS source_events (
             id               INTEGER PRIMARY KEY,
             account_feed_id  INTEGER NOT NULL REFERENCES account_feeds(id) ON DELETE CASCADE,
             source_key       TEXT NOT NULL,
             event_type       TEXT NOT NULL CHECK (event_type IN ('cash','trade')),
             occurred_on      TEXT NOT NULL,
             source_doc_id    INTEGER REFERENCES documents(id) ON DELETE RESTRICT,
             payload_hash     TEXT,
             observed_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
             UNIQUE(account_feed_id, source_key)
           )"""
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_source_events_date ON source_events(occurred_on)"
    )

    connection.execute(
        """CREATE TABLE IF NOT EXISTS event_evidence (
             canonical_event_id  INTEGER NOT NULL REFERENCES canonical_events(id) ON DELETE CASCADE,
             source_event_id     INTEGER NOT NULL REFERENCES source_events(id) ON DELETE RESTRICT,
             evidence_role       TEXT NOT NULL DEFAULT 'supporting'
                                   CHECK (evidence_role IN ('primary','supporting','aggregate')),
             match_method        TEXT NOT NULL,
             confidence          REAL CHECK (confidence IS NULL OR
                                              (confidence >= 0 AND confidence <= 1)),
             notes               TEXT,
             linked_at           TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
             PRIMARY KEY (canonical_event_id, source_event_id)
           )"""
    )
    connection.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS uq_event_evidence_primary
           ON event_evidence(canonical_event_id) WHERE evidence_role='primary'"""
    )
    connection.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS uq_event_evidence_nonaggregate_source
           ON event_evidence(source_event_id) WHERE evidence_role!='aggregate'"""
    )
    connection.execute(
        """CREATE TRIGGER IF NOT EXISTS event_evidence_same_account_insert
           BEFORE INSERT ON event_evidence
           BEGIN
             SELECT CASE WHEN NOT EXISTS (
               SELECT 1
               FROM canonical_events c
               JOIN source_events s ON s.id=NEW.source_event_id
               JOIN account_feeds f ON f.id=s.account_feed_id
               WHERE c.id=NEW.canonical_event_id
                 AND c.account_id=f.account_id
                 AND c.event_type=s.event_type
             ) THEN RAISE(ABORT, 'event evidence account/type mismatch') END;
           END"""
    )
    connection.execute(
        """CREATE TRIGGER IF NOT EXISTS event_evidence_same_account_update
           BEFORE UPDATE OF canonical_event_id, source_event_id ON event_evidence
           BEGIN
             SELECT CASE WHEN NOT EXISTS (
               SELECT 1
               FROM canonical_events c
               JOIN source_events s ON s.id=NEW.source_event_id
               JOIN account_feeds f ON f.id=s.account_feed_id
               WHERE c.id=NEW.canonical_event_id
                 AND c.account_id=f.account_id
                 AND c.event_type=s.event_type
             ) THEN RAISE(ABORT, 'event evidence account/type mismatch') END;
           END"""
    )

    connection.execute(
        """CREATE TABLE IF NOT EXISTS balance_evidence (
             id                      INTEGER PRIMARY KEY,
             canonical_balance_id    INTEGER NOT NULL REFERENCES balances(id) ON DELETE CASCADE,
             account_feed_id         INTEGER REFERENCES account_feeds(id) ON DELETE SET NULL,
             source_doc_id           INTEGER REFERENCES documents(id) ON DELETE RESTRICT,
             source_key              TEXT NOT NULL,
             observed_amount_minor   INTEGER NOT NULL,
             observed_currency       TEXT NOT NULL,
             observed_at             TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
           )"""
    )
    connection.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS uq_balance_evidence_source
           ON balance_evidence(COALESCE(account_feed_id, 0), source_key)"""
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_balance_evidence_balance ON balance_evidence(canonical_balance_id)"
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS position_evidence (
             id                       INTEGER PRIMARY KEY,
             canonical_position_id    INTEGER NOT NULL REFERENCES positions(id) ON DELETE CASCADE,
             account_feed_id          INTEGER REFERENCES account_feeds(id) ON DELETE SET NULL,
             source_doc_id            INTEGER REFERENCES documents(id) ON DELETE RESTRICT,
             source_key               TEXT NOT NULL,
             observed_quantity        REAL NOT NULL,
             observed_avg_cost_minor  INTEGER,
             observed_price_minor     INTEGER,
             observed_value_minor     INTEGER,
             observed_currency        TEXT NOT NULL,
             observed_at              TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
           )"""
    )
    connection.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS uq_position_evidence_source
           ON position_evidence(COALESCE(account_feed_id, 0), source_key)"""
    )
    connection.execute(
        """CREATE INDEX IF NOT EXISTS idx_position_evidence_position
           ON position_evidence(canonical_position_id)"""
    )


def _balance_identity(row: sqlite3.Row | tuple[object, ...]) -> tuple[int, str, str]:
    return int(row[1]), str(row[2]), str(row[3] or "")


def _normalize_existing_balances(connection: sqlite3.Connection) -> None:
    """Collapse only provably identical duplicate snapshots and retain their evidence.

    Different values on the same logical snapshot require a human source-priority
    decision.  The migration refuses to guess and rolls back atomically.
    """

    rows = list(
        connection.execute(
            """SELECT id, account_id, as_of, component, amount_minor, currency, source_doc_id
               FROM balances ORDER BY id"""
        )
    )
    groups: dict[tuple[int, str, str], list[tuple[object, ...]]] = defaultdict(list)
    for row in rows:
        groups[_balance_identity(row)].append(row)

    conflicts = [
        group
        for group in groups.values()
        if len(group) > 1
        and len({(row[4], str(row[5])) for row in group}) > 1
    ]
    if conflicts:
        raise MigrationError(
            f"{len(conflicts)} balance snapshot conflict(s) need reconciliation before migration"
        )
    feed_by_account = {
        int(row[0]): int(row[1])
        for row in connection.execute(
            """SELECT f.account_id, MIN(f.id)
               FROM account_feeds f
               WHERE f.is_active=1
               GROUP BY f.account_id"""
        )
    }
    for group in groups.values():
        canonical_id = int(group[0][0])
        for row in group:
            account_id = int(row[1])
            connection.execute(
                """INSERT OR IGNORE INTO balance_evidence
                   (canonical_balance_id, account_feed_id, source_doc_id, source_key,
                    observed_amount_minor, observed_currency)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    canonical_id,
                    feed_by_account.get(account_id) if row[6] is None else None,
                    row[6],
                    f"legacy:balance:{int(row[0])}",
                    row[4],
                    str(row[5]),
                ),
            )
        for duplicate in group[1:]:
            connection.execute("DELETE FROM balances WHERE id=?", (int(duplicate[0]),))

    connection.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS uq_balances_account_date_component
           ON balances(account_id, as_of, COALESCE(component, ''))"""
    )
    connection.execute(
        """CREATE TRIGGER IF NOT EXISTS balances_component_not_empty_insert
           BEFORE INSERT ON balances
           WHEN NEW.component=''
           BEGIN
             SELECT RAISE(ABORT, 'balances.component must be NULL or non-empty');
           END"""
    )
    connection.execute(
        """CREATE TRIGGER IF NOT EXISTS balances_component_not_empty_update
           BEFORE UPDATE OF component ON balances
           WHEN NEW.component=''
           BEGIN
             SELECT RAISE(ABORT, 'balances.component must be NULL or non-empty');
           END"""
    )


def _migration_2_normalized_evidence(connection: sqlite3.Connection) -> None:
    _create_event_schema(connection)

    _add_column(
        connection,
        "transactions",
        "flow_type",
        "TEXT CHECK (flow_type IN "
        "('expense','income','transfer','investment','tax','refund','other'))",
    )
    _add_column(
        connection,
        "transactions",
        "source_event_id",
        "INTEGER REFERENCES source_events(id) ON DELETE RESTRICT",
    )
    _add_column(
        connection,
        "transactions",
        "canonical_event_id",
        "INTEGER REFERENCES canonical_events(id) ON DELETE SET NULL",
    )
    _add_column(
        connection,
        "trades",
        "source_event_id",
        "INTEGER REFERENCES source_events(id) ON DELETE RESTRICT",
    )
    _add_column(
        connection,
        "trades",
        "canonical_event_id",
        "INTEGER REFERENCES canonical_events(id) ON DELETE SET NULL",
    )
    connection.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS uq_transactions_source_event
           ON transactions(source_event_id) WHERE source_event_id IS NOT NULL"""
    )
    connection.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS uq_trades_source_event
           ON trades(source_event_id) WHERE source_event_id IS NOT NULL"""
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_transactions_canonical_event ON transactions(canonical_event_id)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_trades_canonical_event ON trades(canonical_event_id)"
    )
    connection.execute(
        """CREATE TRIGGER IF NOT EXISTS transactions_event_links_insert
           BEFORE INSERT ON transactions
           BEGIN
             SELECT CASE WHEN NEW.source_event_id IS NOT NULL AND NOT EXISTS (
               SELECT 1 FROM source_events s
               JOIN account_feeds f ON f.id=s.account_feed_id
               WHERE s.id=NEW.source_event_id AND s.event_type='cash'
                 AND f.account_id=NEW.account_id
             ) THEN RAISE(ABORT, 'transaction source event account/type mismatch') END;
             SELECT CASE WHEN NEW.canonical_event_id IS NOT NULL AND NOT EXISTS (
               SELECT 1 FROM canonical_events c
               WHERE c.id=NEW.canonical_event_id AND c.event_type='cash'
                 AND c.account_id=NEW.account_id
             ) THEN RAISE(ABORT, 'transaction canonical event account/type mismatch') END;
           END"""
    )
    connection.execute(
        """CREATE TRIGGER IF NOT EXISTS transactions_event_links_update
           BEFORE UPDATE OF account_id, source_event_id, canonical_event_id ON transactions
           BEGIN
             SELECT CASE WHEN NEW.source_event_id IS NOT NULL AND NOT EXISTS (
               SELECT 1 FROM source_events s
               JOIN account_feeds f ON f.id=s.account_feed_id
               WHERE s.id=NEW.source_event_id AND s.event_type='cash'
                 AND f.account_id=NEW.account_id
             ) THEN RAISE(ABORT, 'transaction source event account/type mismatch') END;
             SELECT CASE WHEN NEW.canonical_event_id IS NOT NULL AND NOT EXISTS (
               SELECT 1 FROM canonical_events c
               WHERE c.id=NEW.canonical_event_id AND c.event_type='cash'
                 AND c.account_id=NEW.account_id
             ) THEN RAISE(ABORT, 'transaction canonical event account/type mismatch') END;
           END"""
    )
    connection.execute(
        """CREATE TRIGGER IF NOT EXISTS trades_event_links_insert
           BEFORE INSERT ON trades
           BEGIN
             SELECT CASE WHEN NEW.source_event_id IS NOT NULL AND NOT EXISTS (
               SELECT 1 FROM source_events s
               JOIN account_feeds f ON f.id=s.account_feed_id
               WHERE s.id=NEW.source_event_id AND s.event_type='trade'
                 AND f.account_id=NEW.account_id
             ) THEN RAISE(ABORT, 'trade source event account/type mismatch') END;
             SELECT CASE WHEN NEW.canonical_event_id IS NOT NULL AND NOT EXISTS (
               SELECT 1 FROM canonical_events c
               WHERE c.id=NEW.canonical_event_id AND c.event_type='trade'
                 AND c.account_id=NEW.account_id
             ) THEN RAISE(ABORT, 'trade canonical event account/type mismatch') END;
           END"""
    )
    connection.execute(
        """CREATE TRIGGER IF NOT EXISTS trades_event_links_update
           BEFORE UPDATE OF account_id, source_event_id, canonical_event_id ON trades
           BEGIN
             SELECT CASE WHEN NEW.source_event_id IS NOT NULL AND NOT EXISTS (
               SELECT 1 FROM source_events s
               JOIN account_feeds f ON f.id=s.account_feed_id
               WHERE s.id=NEW.source_event_id AND s.event_type='trade'
                 AND f.account_id=NEW.account_id
             ) THEN RAISE(ABORT, 'trade source event account/type mismatch') END;
             SELECT CASE WHEN NEW.canonical_event_id IS NOT NULL AND NOT EXISTS (
               SELECT 1 FROM canonical_events c
               WHERE c.id=NEW.canonical_event_id AND c.event_type='trade'
                 AND c.account_id=NEW.account_id
             ) THEN RAISE(ABORT, 'trade canonical event account/type mismatch') END;
           END"""
    )

    # Preserve the legacy marker as a compatibility read, but make the feed mapping
    # explicit.  One logical wrapper account can now receive multiple feed rows.
    connection.execute(
        """INSERT INTO account_feeds(account_id, provider, feed_type, label)
           SELECT a.id, a.live_source, 'api', a.live_source
           FROM accounts a
           WHERE a.live_source IS NOT NULL AND TRIM(a.live_source) != ''
             AND NOT EXISTS (
               SELECT 1 FROM account_feeds f
               WHERE f.account_id=a.id AND f.provider=a.live_source
             )"""
    )

    _normalize_existing_balances(connection)
    feed_by_account = {
        int(row[0]): int(row[1])
        for row in connection.execute(
            """SELECT account_id, MIN(id) FROM account_feeds
               WHERE is_active=1 GROUP BY account_id"""
        )
    }
    for row in connection.execute(
        """SELECT id, account_id, quantity, avg_cost_minor, market_price_minor,
                  market_value_minor, currency, source_doc_id
           FROM positions"""
    ):
        connection.execute(
            """INSERT OR IGNORE INTO position_evidence
               (canonical_position_id, account_feed_id, source_doc_id, source_key,
                observed_quantity, observed_avg_cost_minor, observed_price_minor,
                observed_value_minor, observed_currency)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                int(row[0]),
                feed_by_account.get(int(row[1])) if row[7] is None else None,
                row[7],
                f"legacy:position:{int(row[0])}",
                float(row[2]),
                row[3],
                row[4],
                row[5],
                str(row[6]),
            ),
        )
