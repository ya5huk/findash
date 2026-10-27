from __future__ import annotations

from contextlib import redirect_stderr
import io
import sqlite3
import stat
import sys
import tempfile
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from findash_core.db import connect_db  # noqa: E402
from findash_core.migrations import (  # noqa: E402
    LATEST_SCHEMA_VERSION,
    MigrationError,
    UninitializedDatabaseError,
    apply_migrations,
    schema_version,
)
from findash_core.migration_transfers import (  # noqa: E402
    _migration_14_inferred_account_transfers,
    _migration_15_funding_fx_links,
)
from migrate_db import initialize  # noqa: E402
import migrate_db as migrate_db_module  # noqa: E402


LEGACY_SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE documents (
  id INTEGER PRIMARY KEY, drive_id TEXT UNIQUE NOT NULL, drive_path TEXT NOT NULL,
  filename TEXT NOT NULL, doc_type TEXT, doc_date TEXT, ingested_at TEXT,
  raw_hash TEXT, notes TEXT
);
CREATE TABLE accounts (
  id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, kind TEXT NOT NULL,
  institution TEXT NOT NULL, external_id TEXT, currency TEXT NOT NULL,
  opened_on TEXT, closed_on TEXT, investment_track TEXT, liquidity_date TEXT, notes TEXT
);
CREATE TABLE balances (
  id INTEGER PRIMARY KEY,
  account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  as_of TEXT NOT NULL, component TEXT, amount_minor INTEGER NOT NULL,
  currency TEXT NOT NULL, source_doc_id INTEGER REFERENCES documents(id) ON DELETE SET NULL,
  UNIQUE(account_id, as_of, component)
);
CREATE TABLE transactions (
  id INTEGER PRIMARY KEY,
  account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  date TEXT NOT NULL, value_date TEXT, amount_minor INTEGER NOT NULL,
  currency TEXT NOT NULL, category TEXT, component TEXT, counterparty TEXT,
  reference TEXT, description TEXT,
  source_doc_id INTEGER REFERENCES documents(id) ON DELETE SET NULL
);
CREATE TABLE securities (
  id INTEGER PRIMARY KEY, ticker TEXT UNIQUE NOT NULL, name TEXT,
  asset_class TEXT, currency TEXT NOT NULL
);
CREATE TABLE trades (
  id INTEGER PRIMARY KEY, date TEXT NOT NULL,
  account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  security_id INTEGER NOT NULL REFERENCES securities(id) ON DELETE CASCADE,
  side TEXT NOT NULL CHECK(side IN ('buy','sell')), shares REAL NOT NULL,
  price_minor INTEGER NOT NULL, fees_minor INTEGER NOT NULL DEFAULT 0,
  currency TEXT NOT NULL, source_doc_id INTEGER REFERENCES documents(id) ON DELETE SET NULL
);
CREATE TABLE prices (
  security_id INTEGER NOT NULL REFERENCES securities(id) ON DELETE CASCADE,
  date TEXT NOT NULL, close_minor INTEGER NOT NULL, currency TEXT NOT NULL,
  PRIMARY KEY(security_id, date)
);
CREATE TABLE fx_rates (
  date TEXT NOT NULL, base_currency TEXT NOT NULL, quote_currency TEXT NOT NULL,
  rate REAL NOT NULL, source TEXT NOT NULL DEFAULT 'yahoo',
  PRIMARY KEY(date, base_currency, quote_currency)
);
CREATE TABLE payslips (
  id INTEGER PRIMARY KEY, period_start TEXT NOT NULL, period_end TEXT NOT NULL,
  paid_on TEXT, employer TEXT NOT NULL, gross_minor INTEGER NOT NULL,
  net_minor INTEGER NOT NULL, income_tax_minor INTEGER NOT NULL DEFAULT 0,
  social_security_minor INTEGER NOT NULL DEFAULT 0,
  health_insurance_minor INTEGER NOT NULL DEFAULT 0,
  pension_employee_minor INTEGER NOT NULL DEFAULT 0,
  pension_employer_minor INTEGER NOT NULL DEFAULT 0,
  study_fund_employee_minor INTEGER NOT NULL DEFAULT 0,
  study_fund_employer_minor INTEGER NOT NULL DEFAULT 0,
  currency TEXT NOT NULL DEFAULT 'ILS',
  source_doc_id INTEGER REFERENCES documents(id) ON DELETE SET NULL,
  UNIQUE(period_start, period_end, employer)
);
CREATE TABLE payslip_line_items (
  id INTEGER PRIMARY KEY,
  payslip_id INTEGER NOT NULL REFERENCES payslips(id) ON DELETE CASCADE,
  label TEXT NOT NULL, amount_minor INTEGER NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('earning','deduction','info'))
);
"""


V11_TRIGGER_NAMES = {
    "canonical_events_trade_cash_identity_update",
    "event_evidence_raw_pointer_insert",
    "event_evidence_raw_pointer_update",
    "trade_cash_links_valid_insert",
    "trade_cash_links_valid_update",
    "trades_event_pointer_insert",
    "trades_event_pointer_update",
    "transactions_event_pointer_insert",
    "transactions_event_pointer_update",
}

V11_BASELINE_INDEX_NAMES = {
    "idx_accounts_institution",
    "idx_accounts_kind",
    "idx_balances_account_date",
    "idx_documents_doc_date",
    "idx_documents_doc_type",
    "idx_event_evidence_source",
    "idx_payslip_line_items_payslip",
    "idx_payslips_period",
    "idx_trades_account_security",
    "idx_trades_date",
    "idx_transactions_account_date",
    "idx_transactions_category",
    "idx_transactions_reference",
}


class SchemaMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.db = Path(self.tempdir.name) / "finance.db"

    def create_legacy(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db)
        connection.executescript(LEGACY_SCHEMA)
        connection.execute(
            "INSERT INTO accounts(name,kind,institution,currency) VALUES ('Broker','brokerage','Wrapper','USD')"
        )
        connection.commit()
        return connection

    def create_current(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db)
        connection.executescript((SCRIPTS / "init-db.sql").read_text(encoding="utf-8"))
        connection.close()
        return connect_db(self.db)

    def test_legacy_database_migrates_idempotently(self) -> None:
        connection = self.create_legacy()
        connection.execute("ALTER TABLE accounts ADD COLUMN live_source TEXT")
        connection.execute("UPDATE accounts SET live_source='ibkr'")
        connection.commit()
        connection.close()

        migrated = connect_db(self.db)
        self.addCleanup(migrated.close)
        self.assertEqual(schema_version(migrated), LATEST_SCHEMA_VERSION)
        self.assertEqual(
            migrated.execute(
                "SELECT provider FROM account_feeds WHERE account_id=1"
            ).fetchone()[0],
            "ibkr",
        )
        self.assertIn(
            "flow_type",
            {row[1] for row in migrated.execute("PRAGMA table_info(transactions)")},
        )
        self.assertIn(
            "component",
            {row[1] for row in migrated.execute("PRAGMA table_info(canonical_events)")},
        )
        self.assertIn(
            "account_snapshot_complete",
            {row[1] for row in migrated.execute("PRAGMA table_info(position_evidence)")},
        )
        self.assertIn(
            "account_snapshot_complete",
            {row[1] for row in migrated.execute("PRAGMA table_info(balance_evidence)")},
        )
        self.assertEqual(apply_migrations(migrated), ())

    def test_initializer_creates_current_private_database(self) -> None:
        initialize(self.db)
        connection = connect_db(self.db, readonly=True)
        self.addCleanup(connection.close)
        self.assertEqual(schema_version(connection), LATEST_SCHEMA_VERSION)
        self.assertEqual(stat.S_IMODE(self.db.stat().st_mode), 0o600)
        self.assertEqual(
            connection.execute("PRAGMA foreign_key_check").fetchall(), []
        )

    def test_initializer_failure_does_not_publish_partial_database(self) -> None:
        broken_schema = Path(self.tempdir.name) / "broken.sql"
        broken_schema.write_text(
            "CREATE TABLE partial(value TEXT); SELECT * FROM missing_table;",
            encoding="utf-8",
        )
        original_schema = migrate_db_module.SCHEMA
        migrate_db_module.SCHEMA = broken_schema
        try:
            with self.assertRaises(sqlite3.Error):
                initialize(self.db)
        finally:
            migrate_db_module.SCHEMA = original_schema

        self.assertFalse(self.db.exists())
        self.assertEqual(list(Path(self.tempdir.name).glob(".finance.db.*.init")), [])

    def test_connection_refuses_missing_database_without_creating_it(self) -> None:
        missing = Path(self.tempdir.name) / "missing.db"
        with self.assertRaises(UninitializedDatabaseError):
            connect_db(missing)
        self.assertFalse(missing.exists())

    def test_connection_refuses_symlinked_database_or_sidecar(self) -> None:
        target = Path(self.tempdir.name) / "target.db"
        initialize(target)
        link = Path(self.tempdir.name) / "linked.db"
        link.symlink_to(target)
        with self.assertRaisesRegex(UninitializedDatabaseError, "regular file"):
            connect_db(link)

        sidecar_target = Path(self.tempdir.name) / "outside-wal"
        sidecar_target.write_bytes(b"leave me")
        Path(f"{target}-wal").symlink_to(sidecar_target)
        with self.assertRaisesRegex(UninitializedDatabaseError, "sidecar"):
            connect_db(target, readonly=True)
        self.assertEqual(sidecar_target.read_bytes(), b"leave me")

    def test_connection_and_initializer_refuse_symlinked_data_parent(self) -> None:
        repo = Path(self.tempdir.name) / "repo"
        repo.mkdir()
        outside = Path(self.tempdir.name) / "outside"
        outside.mkdir()
        target = outside / "finance.db"
        initialize(target)
        before = target.read_bytes()
        (repo / "data").symlink_to(outside, target_is_directory=True)

        with self.assertRaisesRegex(UninitializedDatabaseError, "directory"):
            connect_db(repo / "data" / "finance.db")
        with self.assertRaisesRegex(UninitializedDatabaseError, "directory"):
            initialize(repo / "data" / "new.db")
        self.assertEqual(target.read_bytes(), before)
        self.assertFalse((outside / "new.db").exists())

    def test_migrate_check_rejects_current_database_with_orphans(self) -> None:
        initialize(self.db)
        connection = sqlite3.connect(self.db)
        connection.execute(
            """INSERT INTO transactions(
                   account_id,date,amount_minor,currency,description
               ) VALUES(999,'2026-01-01',100,'ILS','private-sentinel')"""
        )
        connection.commit()
        connection.close()

        old_default = migrate_db_module.DEFAULT_DB
        migrate_db_module.DEFAULT_DB = self.db
        errors = io.StringIO()
        try:
            with redirect_stderr(errors):
                status = migrate_db_module.main(["--check"])
        finally:
            migrate_db_module.DEFAULT_DB = old_default

        self.assertEqual(status, 1)
        self.assertIn("foreign-key violations", errors.getvalue())
        self.assertNotIn("private-sentinel", errors.getvalue())

    def test_readonly_connection_handles_uri_characters_in_path(self) -> None:
        unusual = Path(self.tempdir.name) / "finance ?#.db"
        initialize(unusual)
        connection = connect_db(unusual, readonly=True)
        self.addCleanup(connection.close)
        self.assertEqual(schema_version(connection), LATEST_SCHEMA_VERSION)

    def test_fresh_validation_triggers_match_migration(self) -> None:
        connection = self.create_current()
        self.addCleanup(connection.close)

        def validation_sql() -> dict[str, str]:
            return {
                str(name): " ".join(str(sql).split())
                for name, sql in connection.execute(
                    """SELECT name,sql FROM sqlite_master
                       WHERE type='trigger' AND name LIKE 'validate_%'"""
                )
            }

        fresh = validation_sql()
        for name in fresh:
            connection.execute(f'DROP TRIGGER "{name}"')
        connection.execute("DELETE FROM schema_migrations WHERE version>=3")
        connection.commit()

        applied = apply_migrations(connection)
        self.assertEqual(
            [migration.version for migration in applied],
            [3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16],
        )
        self.assertEqual(validation_sql(), fresh)

    def test_fresh_evidence_guards_match_migration(self) -> None:
        connection = self.create_current()
        self.addCleanup(connection.close)
        names = {
            "account_feeds_retain_evidence",
            "balance_evidence_same_account_insert",
            "balance_evidence_same_account_update",
            "position_evidence_same_account_insert",
            "position_evidence_same_account_update",
        }

        def guard_sql() -> dict[str, str]:
            placeholders = ",".join("?" for _ in names)
            return {
                str(name): " ".join(str(sql).split())
                for name, sql in connection.execute(
                    f"SELECT name,sql FROM sqlite_master "
                    f"WHERE type='trigger' AND name IN ({placeholders})",
                    tuple(sorted(names)),
                )
            }

        fresh = guard_sql()
        self.assertEqual(set(fresh), names)
        for name in names:
            connection.execute(f'DROP TRIGGER "{name}"')
        connection.execute("DELETE FROM schema_migrations WHERE version>=5")
        connection.commit()

        applied = apply_migrations(connection)
        self.assertEqual(
            [migration.version for migration in applied],
            [5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16],
        )
        self.assertEqual(guard_sql(), fresh)

    def test_fresh_parent_identity_guards_match_migration(self) -> None:
        connection = self.create_current()
        self.addCleanup(connection.close)
        names = {
            "account_feeds_identity_update",
            "source_events_identity_update",
            "canonical_events_identity_update",
            "balances_identity_update",
            "positions_identity_update",
        }

        def guard_sql() -> dict[str, str]:
            placeholders = ",".join("?" for _ in names)
            return {
                str(name): " ".join(str(sql).split())
                for name, sql in connection.execute(
                    f"SELECT name,sql FROM sqlite_master "
                    f"WHERE type='trigger' AND name IN ({placeholders})",
                    tuple(sorted(names)),
                )
            }

        fresh = guard_sql()
        self.assertEqual(set(fresh), names)
        for name in names:
            connection.execute(f'DROP TRIGGER "{name}"')
        connection.execute("DELETE FROM schema_migrations WHERE version>=6")
        connection.commit()

        applied = apply_migrations(connection)
        self.assertEqual(
            [migration.version for migration in applied],
            [6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16],
        )
        self.assertEqual(guard_sql(), fresh)

    def test_fresh_feed_scoped_trade_index_matches_migration(self) -> None:
        connection = self.create_current()
        self.addCleanup(connection.close)

        def index_sql() -> str:
            sql = connection.execute(
                """SELECT sql FROM sqlite_master
                   WHERE type='index' AND name='idx_trades_external'"""
            ).fetchone()[0]
            return " ".join(str(sql).split())

        fresh = index_sql()
        connection.execute("DROP INDEX idx_trades_external")
        connection.execute("DELETE FROM schema_migrations WHERE version>=7")
        connection.commit()

        applied = apply_migrations(connection)
        self.assertEqual(
            [migration.version for migration in applied],
            [7, 8, 9, 10, 11, 12, 13, 14, 15, 16],
        )
        self.assertEqual(index_sql(), fresh)

    def test_fresh_trade_cash_schema_matches_migration(self) -> None:
        connection = self.create_current()
        self.addCleanup(connection.close)
        names = {
            "trade_cash_links_valid_insert",
            "trade_cash_links_valid_update",
            "canonical_events_trade_cash_identity_update",
        }

        def schema_sql() -> dict[str, str]:
            placeholders = ",".join("?" for _ in names)
            rows = connection.execute(
                f"""SELECT type,name,sql FROM sqlite_master
                    WHERE (type='table' AND name='trade_cash_links')
                       OR (type='index' AND name='uq_trade_cash_links_trade')
                       OR (type='trigger' AND name IN ({placeholders}))""",
                tuple(sorted(names)),
            )
            return {
                f"{kind}:{name}": " ".join(str(sql).split())
                for kind, name, sql in rows
            }

        fresh = schema_sql()
        for name in names:
            connection.execute(f'DROP TRIGGER "{name}"')
        connection.execute("DROP TABLE trade_cash_links")
        connection.execute("DELETE FROM schema_migrations WHERE version>=8")
        connection.commit()

        applied = apply_migrations(connection)
        self.assertEqual(
            [migration.version for migration in applied],
            [8, 9, 10, 11, 12, 13, 14, 15, 16],
        )
        self.assertEqual(schema_sql(), fresh)

    def test_fresh_v11_relationship_triggers_match_migration(self) -> None:
        connection = self.create_current()
        self.addCleanup(connection.close)

        def trigger_sql() -> dict[str, str]:
            placeholders = ",".join("?" for _ in V11_TRIGGER_NAMES)
            return {
                str(name): " ".join(str(sql).split())
                for name, sql in connection.execute(
                    f"SELECT name,sql FROM sqlite_master "
                    f"WHERE type='trigger' AND name IN ({placeholders})",
                    tuple(sorted(V11_TRIGGER_NAMES)),
                )
            }

        fresh = trigger_sql()
        self.assertEqual(set(fresh), V11_TRIGGER_NAMES)
        for name in V11_TRIGGER_NAMES:
            connection.execute(f'DROP TRIGGER "{name}"')
        connection.execute("DELETE FROM schema_migrations WHERE version>=11")
        connection.commit()

        applied = apply_migrations(connection)

        self.assertEqual(
            [migration.version for migration in applied], [11, 12, 13, 14, 15, 16]
        )
        self.assertEqual(trigger_sql(), fresh)

    def test_fresh_v12_security_distribution_schema_matches_migration(self) -> None:
        connection = self.create_current()
        self.addCleanup(connection.close)

        def schema_sql() -> dict[str, str]:
            return {
                f"{kind}:{name}": " ".join(str(sql).split())
                for kind, name, sql in connection.execute(
                    """SELECT type,name,sql FROM sqlite_master
                       WHERE (type='table' AND name IN (
                                'security_distributions',
                                'security_distribution_refreshes'
                             ))
                          OR (type='index'
                              AND name='idx_security_distributions_security_date')
                          OR (type='trigger'
                              AND name IN (
                                'validate_security_distributions_insert',
                                'validate_security_distributions_update',
                                'validate_security_distribution_refreshes_insert',
                                'validate_security_distribution_refreshes_update'
                              ))"""
                )
            }

        fresh = schema_sql()
        connection.execute("DROP TABLE security_distribution_refreshes")
        connection.execute("DROP TABLE security_distributions")
        connection.execute("DELETE FROM schema_migrations WHERE version>=12")
        connection.commit()

        applied = apply_migrations(connection)

        self.assertEqual(
            [migration.version for migration in applied], [12, 13, 14, 15, 16]
        )
        self.assertEqual(schema_sql(), fresh)

    def test_v12_security_distributions_preserve_exact_rational_values(self) -> None:
        connection = self.create_current()
        self.addCleanup(connection.close)
        security_id = connection.execute(
            """INSERT INTO securities(ticker,currency)
               VALUES ('DIST','USD') RETURNING id"""
        ).fetchone()[0]
        connection.execute(
            """INSERT INTO security_distributions(
                   security_id,event_date,
                   amount_per_share_minor_numerator,
                   amount_per_share_minor_denominator,
                   currency,source,source_key
               ) VALUES (?,'2026-01-02',421,10,'USD','provider','event-1')""",
            (security_id,),
        )

        stored = connection.execute(
            """SELECT amount_per_share_minor_numerator,
                      amount_per_share_minor_denominator,
                      typeof(amount_per_share_minor_numerator),
                      typeof(amount_per_share_minor_denominator)
               FROM security_distributions"""
        ).fetchone()
        self.assertEqual(stored, (421, 10, "integer", "integer"))

        for invalid in (0, -1):
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """INSERT INTO security_distributions(
                           security_id,event_date,
                           amount_per_share_minor_numerator,
                           amount_per_share_minor_denominator,
                           currency,source,source_key
                       ) VALUES (?,'2026-01-03',?,1,'USD','provider',?)""",
                    (security_id, invalid, f"invalid-{invalid}"),
                )

        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute(
                """INSERT INTO security_distributions(
                       security_id,event_date,
                       amount_per_share_minor_numerator,
                       amount_per_share_minor_denominator,
                       currency,source,source_key
                   ) VALUES (?,'2026-01-03',1,0,'USD','provider','event-2')""",
                (security_id,),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute(
                """INSERT INTO security_distributions(
                       security_id,event_date,
                       amount_per_share_minor_numerator,
                       amount_per_share_minor_denominator,
                       currency,source,source_key
                   ) VALUES (?,'not-a-date',1,1,'USD','provider','event-3')""",
                (security_id,),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute(
                """INSERT INTO security_distributions(
                       security_id,event_date,
                       amount_per_share_minor_numerator,
                       amount_per_share_minor_denominator,
                       currency,source,source_key
                   ) VALUES (?,'2026-01-04',1,1,'USD','provider','event-1')""",
                (security_id,),
            )

        connection.execute("DELETE FROM securities WHERE id=?", (security_id,))
        self.assertEqual(
            connection.execute(
                "SELECT COUNT(*) FROM security_distributions"
            ).fetchone()[0],
            0,
        )

    def test_fresh_v13_market_observation_schema_matches_migration(self) -> None:
        connection = self.create_current()
        self.addCleanup(connection.close)

        def schema_sql() -> dict[str, str]:
            return {
                f"{kind}:{name}": " ".join(str(sql).split())
                for kind, name, sql in connection.execute(
                    """SELECT type,name,sql FROM sqlite_master
                       WHERE (type='table' AND name IN (
                                'security_price_observations',
                                'fx_rate_observations'
                             ))
                          OR (type='index' AND name IN (
                                'idx_security_price_observations_latest',
                                'idx_fx_rate_observations_latest'
                             ))
                          OR (type='trigger' AND name IN (
                                'validate_security_price_observations_insert',
                                'validate_security_price_observations_update',
                                'validate_fx_rate_observations_insert',
                                'validate_fx_rate_observations_update'
                             ))"""
                )
            }

        fresh = schema_sql()
        connection.execute("DROP TABLE fx_rate_observations")
        connection.execute("DROP TABLE security_price_observations")
        connection.execute("DELETE FROM schema_migrations WHERE version>=13")
        connection.commit()

        applied = apply_migrations(connection)

        self.assertEqual(
            [migration.version for migration in applied], [13, 14, 15, 16]
        )
        self.assertEqual(schema_sql(), fresh)

    V16_OBJECTS = (
        "transfer_links",
        "transfer_links_valid_insert",
        "transfer_links_valid_update",
        "fx_conversions",
        "fx_conversions_valid_insert",
        "fx_conversions_valid_update",
        "idx_fx_conversions_account_date",
        "dividend_events",
        "dividend_events_valid_insert",
        "dividend_events_valid_update",
        "uq_dividend_events_entitlement",
        "ledger_log",
        "idx_ledger_log_kind_time",
        "canonical_events_transfer_identity_update",
        "canonical_events_fx_conversion_identity_update",
        "canonical_events_dividend_identity_update",
    )

    def rewind_to_v15(self, connection: sqlite3.Connection) -> None:
        for trigger in (
            "canonical_events_transfer_identity_update",
            "canonical_events_fx_conversion_identity_update",
            "canonical_events_dividend_identity_update",
        ):
            connection.execute(f"DROP TRIGGER {trigger}")
        for table in ("transfer_links", "fx_conversions", "dividend_events", "ledger_log"):
            connection.execute(f"DROP TABLE {table}")
        _migration_14_inferred_account_transfers(connection)
        _migration_15_funding_fx_links(connection)
        connection.execute("DELETE FROM schema_migrations WHERE version=16")
        connection.commit()

    def v16_schema_sql(self, connection: sqlite3.Connection) -> dict[str, str]:
        placeholders = ",".join("?" for _ in self.V16_OBJECTS)
        return {
            f"{kind}:{name}": " ".join(str(sql).split())
            for kind, name, sql in connection.execute(
                f"""SELECT type,name,sql FROM sqlite_master
                     WHERE name IN ({placeholders})""",
                self.V16_OBJECTS,
            )
        }

    def test_fresh_v16_ledger_engine_schema_matches_migration(self) -> None:
        connection = self.create_current()
        self.addCleanup(connection.close)
        fresh = self.v16_schema_sql(connection)
        self.assertEqual(len(fresh), len(self.V16_OBJECTS))

        self.rewind_to_v15(connection)
        applied = apply_migrations(connection)

        self.assertEqual([migration.version for migration in applied], [16])
        self.assertEqual(self.v16_schema_sql(connection), fresh)
        retired = connection.execute(
            """SELECT COUNT(*) FROM sqlite_master
                WHERE name LIKE '%funding_fx%' OR name LIKE '%inferred_transfer%'"""
        ).fetchone()[0]
        self.assertEqual(retired, 0)

    def test_v16_moves_funding_fx_links_into_ledger_relationships(self) -> None:
        connection = self.create_current()
        self.addCleanup(connection.close)
        self.rewind_to_v15(connection)

        def insert(sql: str, params: tuple = ()) -> int:
            return int(connection.execute(sql + " RETURNING id", params).fetchone()[0])

        bank = insert(
            """INSERT INTO accounts(name,kind,institution,currency)
               VALUES ('Checking','checking','Bank','ILS')"""
        )
        broker = insert(
            """INSERT INTO accounts(name,kind,institution,currency)
               VALUES ('Broker','brokerage','Broker','USD')"""
        )
        document = insert(
            """INSERT INTO documents(drive_id,drive_path,filename,doc_type,doc_date)
               VALUES ('fx','fx-conversions/2026/a.png','a.png','fx_conversion','2026-01-06')"""
        )
        bank_feed = insert(
            """INSERT INTO account_feeds(account_id,provider,feed_type)
               VALUES (?,'document-ledger','document')""",
            (bank,),
        )
        broker_feed = insert(
            """INSERT INTO account_feeds(account_id,provider,feed_type)
               VALUES (?,'document-ledger','document')""",
            (broker,),
        )

        def event(account: int, day: str, amount: int, currency: str, flow: str,
                  category: str, feed: int | None, key: str | None) -> int:
            event_id = insert(
                """INSERT INTO canonical_events(
                       account_id,event_type,occurred_on,currency,amount_minor,
                       flow_type,category,component,status
                   ) VALUES (?,'cash',?,?,?,?,?,?,'confirmed')""",
                (
                    account, day, currency, amount, flow, category,
                    None if account == bank else f"cash_{currency.lower()}",
                ),
            )
            if feed is not None:
                source = insert(
                    """INSERT INTO source_events(
                           account_feed_id,source_key,event_type,occurred_on,source_doc_id
                       ) VALUES (?,?,'cash',?,?)""",
                    (feed, key, day, document),
                )
                connection.execute(
                    """INSERT INTO event_evidence(
                           canonical_event_id,source_event_id,evidence_role,match_method
                       ) VALUES (?,?,'primary','document-row')""",
                    (event_id, source),
                )
            return event_id

        outflow = event(bank, "2026-01-05", -600000, "ILS", "transfer", "transfer", bank_feed, "b")
        inflow = event(broker, "2026-01-05", 600000, "ILS", "transfer", "deposit", None, None)
        sell = event(broker, "2026-01-06", -600000, "ILS", "investment", "fx", broker_feed, "s")
        buy = event(broker, "2026-01-06", 200000, "USD", "investment", "fx", broker_feed, "d")
        connection.execute(
            """INSERT INTO funding_fx_links(
                   bank_outflow_event_id,brokerage_inflow_event_id,
                   fx_source_event_id,fx_destination_event_id,match_method
               ) VALUES (?,?,?,?,'reviewed')""",
            (outflow, inflow, sell, buy),
        )
        connection.executemany(
            """INSERT INTO fx_rates(date,base_currency,quote_currency,rate,source)
               VALUES (?,?,?,?,?)""",
            [
                ("2026-01-06", "USD", "ILS", 3.0, "document"),
                ("2026-01-05", "USD", "ILS", 2.95, "yahoo"),
            ],
        )
        connection.commit()

        applied = apply_migrations(connection)

        self.assertEqual([migration.version for migration in applied], [16])
        self.assertEqual(
            connection.execute(
                "SELECT outflow_event_id,inflow_event_id FROM transfer_links"
            ).fetchall(),
            [(outflow, inflow)],
        )
        self.assertEqual(
            connection.execute(
                """SELECT account_id,converted_on,sell_event_id,buy_event_id,
                          base_currency,quote_currency,actual_rate,market_rate
                     FROM fx_conversions"""
            ).fetchall(),
            [(broker, "2026-01-06", sell, buy, "USD", "ILS", 3.0, None)],
        )
        self.assertEqual(
            connection.execute(
                "SELECT date,source FROM fx_rates ORDER BY date"
            ).fetchall(),
            [("2026-01-05", "yahoo")],
        )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
            connection.execute(
                "UPDATE canonical_events SET amount_minor=1 WHERE id=?", (buy,)
            )

    def test_v12_distribution_refresh_status_is_validated(self) -> None:
        connection = self.create_current()
        self.addCleanup(connection.close)
        security_id = connection.execute(
            """INSERT INTO securities(ticker,currency)
               VALUES ('DIST-STATUS','USD') RETURNING id"""
        ).fetchone()[0]
        connection.execute(
            """INSERT INTO security_distribution_refreshes(
                   security_id,source,status
               ) VALUES (?,'provider','empty')""",
            (security_id,),
        )
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute(
                """UPDATE security_distribution_refreshes
                      SET status='unknown'
                    WHERE security_id=?""",
                (security_id,),
            )

        connection.execute("DELETE FROM securities WHERE id=?", (security_id,))
        self.assertEqual(
            connection.execute(
                "SELECT COUNT(*) FROM security_distribution_refreshes"
            ).fetchone()[0],
            0,
        )

    def test_v11_restores_baseline_indexes_for_legacy_database(self) -> None:
        connection = self.create_legacy()
        self.addCleanup(connection.close)
        connection.commit()

        applied = apply_migrations(connection)
        indexes = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            )
        }

        self.assertEqual(
            [migration.version for migration in applied],
            list(range(1, LATEST_SCHEMA_VERSION + 1)),
        )
        self.assertTrue(V11_BASELINE_INDEX_NAMES <= indexes)

    def test_fresh_snapshot_provenance_guards_match_migration(self) -> None:
        connection = self.create_current()
        self.addCleanup(connection.close)
        names = {
            "balance_evidence_require_provenance_insert",
            "balance_evidence_require_provenance_update",
            "position_evidence_require_provenance_insert",
            "position_evidence_require_provenance_update",
        }

        def guard_sql() -> dict[str, str]:
            placeholders = ",".join("?" for _ in names)
            return {
                str(name): " ".join(str(sql).split())
                for name, sql in connection.execute(
                    f"SELECT name,sql FROM sqlite_master "
                    f"WHERE type='trigger' AND name IN ({placeholders})",
                    tuple(sorted(names)),
                )
            }

        fresh = guard_sql()
        self.assertEqual(set(fresh), names)
        for name in names:
            connection.execute(f'DROP TRIGGER "{name}"')
        connection.execute("DELETE FROM schema_migrations WHERE version>=9")
        connection.commit()

        applied = apply_migrations(connection)
        self.assertEqual(
            [migration.version for migration in applied], [9, 10, 11, 12, 13, 14, 15, 16]
        )
        self.assertEqual(guard_sql(), fresh)

    def test_v10_snapshot_completeness_defaults_partial_and_matches_fresh_schema(self) -> None:
        connection = self.create_current()
        self.addCleanup(connection.close)

        account_id = connection.execute(
            """INSERT INTO accounts(name,kind,institution,currency)
               VALUES ('Broker','brokerage','Test','USD') RETURNING id"""
        ).fetchone()[0]
        feed_id = connection.execute(
            """INSERT INTO account_feeds(account_id,provider,feed_type)
               VALUES (?,'test-feed','api') RETURNING id""",
            (account_id,),
        ).fetchone()[0]
        security_id = connection.execute(
            "INSERT INTO securities(ticker,currency) VALUES ('COMPLETE','USD') RETURNING id"
        ).fetchone()[0]
        position_id = connection.execute(
            """INSERT INTO positions(account_id,security_id,as_of,quantity,currency)
               VALUES (?,?,'2026-01-01',1,'USD') RETURNING id""",
            (account_id, security_id),
        ).fetchone()[0]
        evidence_id = connection.execute(
            """INSERT INTO position_evidence(
                   canonical_position_id,account_feed_id,source_key,
                   observed_quantity,observed_currency
               ) VALUES (?,?,'position',1,'USD') RETURNING id""",
            (position_id, feed_id),
        ).fetchone()[0]
        balance_id = connection.execute(
            """INSERT INTO balances(account_id,as_of,amount_minor,currency)
               VALUES (?,'2026-01-01',100,'USD') RETURNING id""",
            (account_id,),
        ).fetchone()[0]
        balance_evidence_id = connection.execute(
            """INSERT INTO balance_evidence(
                   canonical_balance_id,account_feed_id,source_key,
                   observed_amount_minor,observed_currency
               ) VALUES (?,?,'balance',100,'USD') RETURNING id""",
            (balance_id, feed_id),
        ).fetchone()[0]
        self.assertEqual(
            connection.execute(
                "SELECT account_snapshot_complete FROM position_evidence WHERE id=?",
                (evidence_id,),
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            connection.execute(
                "SELECT account_snapshot_complete FROM balance_evidence WHERE id=?",
                (balance_evidence_id,),
            ).fetchone()[0],
            0,
        )
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE position_evidence SET account_snapshot_complete=2 WHERE id=?",
                (evidence_id,),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE balance_evidence SET account_snapshot_complete=2 WHERE id=?",
                (balance_evidence_id,),
            )

        def validation_sql() -> dict[str, str]:
            return {
                str(name): " ".join(str(sql).split())
                for name, sql in connection.execute(
                    """SELECT name,sql FROM sqlite_master
                       WHERE type='trigger'
                         AND name IN ('validate_balance_evidence_insert',
                                      'validate_balance_evidence_update',
                                      'validate_position_evidence_insert',
                                      'validate_position_evidence_update')"""
                )
            }

        fresh = validation_sql()
        for name in fresh:
            connection.execute(f'DROP TRIGGER "{name}"')
        connection.execute("DELETE FROM schema_migrations WHERE version>=10")
        connection.commit()

        applied = apply_migrations(connection)

        self.assertEqual(
            [migration.version for migration in applied], [10, 11, 12, 13, 14, 15, 16]
        )
        self.assertEqual(validation_sql(), fresh)

    def test_v10_rejects_ambiguous_split_trade_settlements(self) -> None:
        connection = self.create_current()
        self.addCleanup(connection.close)
        connection.execute("DROP INDEX uq_trade_cash_links_trade")
        connection.execute("DELETE FROM schema_migrations WHERE version>=10")
        account_id = connection.execute(
            """INSERT INTO accounts(name,kind,institution,currency)
               VALUES ('Broker','brokerage','Test','USD') RETURNING id"""
        ).fetchone()[0]
        security_id = connection.execute(
            "INSERT INTO securities(ticker,currency) VALUES ('SPLIT','USD') RETURNING id"
        ).fetchone()[0]
        trade_id = connection.execute(
            """INSERT INTO canonical_events(
                   account_id,event_type,occurred_on,currency,security_id,
                   side,quantity,price_minor
               ) VALUES (?,'trade','2026-01-01','USD',?,'buy',1,10000)
               RETURNING id""",
            (account_id, security_id),
        ).fetchone()[0]
        cash_ids = [
            connection.execute(
                """INSERT INTO canonical_events(
                       account_id,event_type,occurred_on,currency,amount_minor,flow_type
                   ) VALUES (?,'cash',?,'USD',-5000,'investment') RETURNING id""",
                (account_id, occurred_on),
            ).fetchone()[0]
            for occurred_on in ("2026-01-02", "2026-01-03")
        ]
        connection.executemany(
            """INSERT INTO trade_cash_links(
                   cash_event_id,trade_event_id,match_method
               ) VALUES(?,?,'legacy reviewed link')""",
            [(cash_id, trade_id) for cash_id in cash_ids],
        )
        connection.commit()

        with self.assertRaisesRegex(MigrationError, "split trade settlements"):
            apply_migrations(connection)

        self.assertEqual(schema_version(connection), 9)

    def test_v11_preflight_reports_only_relationship_violation_counts(self) -> None:
        connection = self.create_current()
        self.addCleanup(connection.close)
        for name in V11_TRIGGER_NAMES:
            connection.execute(f'DROP TRIGGER IF EXISTS "{name}"')
        connection.execute("DELETE FROM schema_migrations WHERE version>=11")

        account_id = connection.execute(
            """INSERT INTO accounts(name,kind,institution,currency)
               VALUES ('Broker','brokerage','Test','USD') RETURNING id"""
        ).fetchone()[0]
        feed_id = connection.execute(
            """INSERT INTO account_feeds(account_id,provider,feed_type)
               VALUES (?,'test-feed','api') RETURNING id""",
            (account_id,),
        ).fetchone()[0]
        source_id = connection.execute(
            """INSERT INTO source_events(
                   account_feed_id,source_key,event_type,occurred_on
               ) VALUES (?,'private-source-key','cash','2026-01-01') RETURNING id""",
            (feed_id,),
        ).fetchone()[0]
        pointer_id = connection.execute(
            """INSERT INTO canonical_events(
                   account_id,event_type,occurred_on,currency,
                   amount_minor,flow_type,category
               ) VALUES (?,'cash','2026-01-01','USD',8675309,'income',
                         'private-category') RETURNING id""",
            (account_id,),
        ).fetchone()[0]
        evidence_id = connection.execute(
            """INSERT INTO canonical_events(
                   account_id,event_type,occurred_on,currency,
                   amount_minor,flow_type
               ) VALUES (?,'cash','2026-01-01','USD',1,'income') RETURNING id""",
            (account_id,),
        ).fetchone()[0]
        connection.execute(
            """INSERT INTO transactions(
                   account_id,date,amount_minor,currency,category,flow_type,
                   source_event_id,canonical_event_id
               ) VALUES (?,'2026-01-01',8675309,'USD','private-category',
                         'income',?,?)""",
            (account_id, source_id, pointer_id),
        )
        connection.execute(
            """INSERT INTO event_evidence(
                   canonical_event_id,source_event_id,evidence_role,match_method
               ) VALUES (?,?,'supporting','private-method')""",
            (evidence_id, source_id),
        )

        security_id = connection.execute(
            "INSERT INTO securities(ticker,currency) VALUES ('DATED','USD') RETURNING id"
        ).fetchone()[0]
        trade_id = connection.execute(
            """INSERT INTO canonical_events(
                   account_id,event_type,occurred_on,currency,security_id,
                   side,quantity,price_minor,fees_minor
               ) VALUES (?,'trade','2026-01-02','USD',?,'buy',1,100,0)
               RETURNING id""",
            (account_id, security_id),
        ).fetchone()[0]
        cash_id = connection.execute(
            """INSERT INTO canonical_events(
                   account_id,event_type,occurred_on,currency,
                   amount_minor,flow_type
               ) VALUES (?,'cash','2026-01-01','USD',-100,'investment')
               RETURNING id""",
            (account_id,),
        ).fetchone()[0]
        connection.execute(
            """INSERT INTO trade_cash_links(
                   cash_event_id,trade_event_id,match_method
               ) VALUES (?,?,'private-link-method')""",
            (cash_id, trade_id),
        )
        connection.commit()

        with self.assertRaises(MigrationError) as caught:
            apply_migrations(connection)

        message = str(caught.exception)
        self.assertIn("invalid existing rows block migration v11", message)
        self.assertIn("trade_cash_links=1", message)
        self.assertIn("transaction_event_pointer=1", message)
        self.assertNotIn("private-source-key", message)
        self.assertNotIn("private-category", message)
        self.assertNotIn("private-method", message)
        self.assertNotIn("8675309", message)
        self.assertEqual(schema_version(connection), 10)

    def test_v11_rules_are_not_retroactively_applied_by_older_migrations(self) -> None:
        connection = self.create_current()
        self.addCleanup(connection.close)
        connection.execute("DELETE FROM schema_migrations WHERE version>=6")
        account_id = connection.execute(
            """INSERT INTO accounts(name,kind,institution,currency)
               VALUES ('Cash','checking','Test','ILS') RETURNING id"""
        ).fetchone()[0]
        feed_id = connection.execute(
            """INSERT INTO account_feeds(account_id,provider,feed_type)
               VALUES (?,'test-feed','api') RETURNING id""",
            (account_id,),
        ).fetchone()[0]
        source_id = connection.execute(
            """INSERT INTO source_events(
                   account_feed_id,source_key,event_type,occurred_on
               ) VALUES (?,'source','cash','2026-01-01') RETURNING id""",
            (feed_id,),
        ).fetchone()[0]
        canonical_id = connection.execute(
            """INSERT INTO canonical_events(
                   account_id,event_type,occurred_on,currency,
                   amount_minor,flow_type
               ) VALUES (?,'cash','2026-01-01','ILS',100,'income')
               RETURNING id""",
            (account_id,),
        ).fetchone()[0]
        connection.execute(
            """INSERT INTO transactions(
                   account_id,date,amount_minor,currency,
                   source_event_id,canonical_event_id
               ) VALUES (?,'2026-01-01',100,'ILS',?,?)""",
            (account_id, source_id, canonical_id),
        )
        connection.commit()

        with self.assertRaisesRegex(MigrationError, "migration v11"):
            apply_migrations(connection)

        self.assertEqual(schema_version(connection), 10)

    def test_normal_connection_enforces_foreign_keys(self) -> None:
        connection = self.create_current()
        self.addCleanup(connection.close)
        self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute(
                """INSERT INTO balances(account_id,as_of,amount_minor,currency)
                   VALUES (999,'2026-01-01',100,'ILS')"""
            )

    def test_connection_rejects_preexisting_orphans_without_echoing_rows(self) -> None:
        connection = self.create_current()
        connection.close()
        unsafe = sqlite3.connect(self.db)
        unsafe.execute("PRAGMA foreign_keys=OFF")
        unsafe.execute(
            """INSERT INTO transactions
               (account_id,date,amount_minor,currency,counterparty)
               VALUES (999,'2026-01-01',8675309,'ILS','private-sentinel')"""
        )
        unsafe.commit()
        unsafe.close()

        with self.assertRaises(MigrationError) as caught:
            connect_db(self.db)
        message = str(caught.exception)
        self.assertIn("transactions=1", message)
        self.assertNotIn("private-sentinel", message)
        self.assertNotIn("8675309", message)

    def test_null_component_duplicate_is_blocked(self) -> None:
        connection = self.create_current()
        self.addCleanup(connection.close)
        connection.execute(
            "INSERT INTO accounts(name,kind,institution,currency) VALUES ('Bank','checking','Bank','ILS')"
        )
        connection.execute(
            """INSERT INTO balances(account_id,as_of,component,amount_minor,currency)
               VALUES (1,'2026-01-01',NULL,100,'ILS')"""
        )
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute(
                """INSERT INTO balances(account_id,as_of,component,amount_minor,currency)
                   VALUES (1,'2026-01-01',NULL,100,'ILS')"""
            )

    def test_migration_collapses_only_identical_duplicates_and_keeps_evidence(self) -> None:
        connection = self.create_legacy()
        connection.executemany(
            """INSERT INTO balances(account_id,as_of,component,amount_minor,currency)
               VALUES (1,'2026-01-01',NULL,?, 'USD')""",
            [(100,), (100,)],
        )
        connection.commit()
        connection.close()

        migrated = connect_db(self.db)
        self.addCleanup(migrated.close)
        self.assertEqual(migrated.execute("SELECT COUNT(*) FROM balances").fetchone()[0], 1)
        self.assertEqual(
            migrated.execute("SELECT COUNT(*) FROM balance_evidence").fetchone()[0], 2
        )

    def test_migration_refuses_conflicting_duplicate_balances_atomically(self) -> None:
        connection = self.create_legacy()
        connection.executemany(
            """INSERT INTO balances(account_id,as_of,component,amount_minor,currency)
               VALUES (1,'2026-01-01',NULL,?, 'USD')""",
            [(100,), (200,)],
        )
        connection.commit()
        connection.close()

        with self.assertRaisesRegex(MigrationError, "need reconciliation"):
            connect_db(self.db)

        check = sqlite3.connect(self.db)
        self.addCleanup(check.close)
        self.assertEqual(check.execute("SELECT COUNT(*) FROM balances").fetchone()[0], 2)
        self.assertEqual(schema_version(check), 1)
        self.assertNotIn(
            "flow_type",
            {row[1] for row in check.execute("PRAGMA table_info(transactions)")},
        )

    def test_migration_rejects_invalid_existing_rows_without_echoing_values(self) -> None:
        connection = self.create_legacy()
        connection.execute(
            """INSERT INTO balances(account_id,as_of,amount_minor,currency)
               VALUES (1,'2026-01-01',123.45,'USD')"""
        )
        connection.execute(
            """INSERT INTO transactions
               (account_id,date,amount_minor,currency,component,counterparty)
               VALUES (1,'2026-02-30',100,'usd','   ','private-sentinel')"""
        )
        connection.execute(
            """INSERT INTO fx_rates(date,base_currency,quote_currency,rate)
               VALUES ('2026-01-01','USD','ILS',0)"""
        )
        security_id = connection.execute(
            "INSERT INTO securities(ticker,currency) VALUES ('TEST','USD') RETURNING id"
        ).fetchone()[0]
        connection.execute(
            """INSERT INTO trades
               (date,account_id,security_id,side,shares,price_minor,fees_minor,currency)
               VALUES ('2026-01-01',1,?,'buy',-2,100,0,'USD')""",
            (security_id,),
        )
        connection.commit()
        connection.close()

        with self.assertRaises(MigrationError) as caught:
            connect_db(self.db)
        message = str(caught.exception)
        self.assertIn("balances=1", message)
        self.assertIn("balance_evidence=1", message)
        self.assertIn("transactions=1", message)
        self.assertIn("trades=1", message)
        self.assertIn("fx_rates=1", message)
        self.assertNotIn("private-sentinel", message)
        self.assertNotIn("123.45", message)

        check = sqlite3.connect(self.db)
        self.addCleanup(check.close)
        self.assertEqual(schema_version(check), 2)

    def test_v5_preflight_rejects_cross_account_snapshot_evidence(self) -> None:
        connection = self.create_current()
        self.addCleanup(connection.close)
        connection.executemany(
            """INSERT INTO accounts(name,kind,institution,currency)
               VALUES (?,'checking','Bank','ILS')""",
            [("One",), ("Two",)],
        )
        feed_id = connection.execute(
            """INSERT INTO account_feeds(account_id,provider,feed_type)
               VALUES (2,'bank-api','api') RETURNING id"""
        ).fetchone()[0]
        balance_id = connection.execute(
            """INSERT INTO balances(account_id,as_of,amount_minor,currency)
               VALUES (1,'2026-01-01',100,'ILS') RETURNING id"""
        ).fetchone()[0]
        connection.execute("DROP TRIGGER balance_evidence_same_account_insert")
        connection.execute("DELETE FROM schema_migrations WHERE version>=5")
        connection.execute(
            """INSERT INTO balance_evidence
               (canonical_balance_id,account_feed_id,source_key,
                observed_amount_minor,observed_currency)
               VALUES (?,?,'private-source-key',100,'ILS')""",
            (balance_id, feed_id),
        )
        connection.commit()

        with self.assertRaises(MigrationError) as caught:
            apply_migrations(connection)
        self.assertIn("balance_evidence=1", str(caught.exception))
        self.assertNotIn("private-source-key", str(caught.exception))
        self.assertEqual(schema_version(connection), 4)

    def test_v6_preflight_rejects_invalidated_parent_identity(self) -> None:
        connection = self.create_current()
        self.addCleanup(connection.close)
        connection.executemany(
            """INSERT INTO accounts(name,kind,institution,currency)
               VALUES (?,'checking','Bank','ILS')""",
            [("One",), ("Two",)],
        )
        feed_id = connection.execute(
            """INSERT INTO account_feeds(account_id,provider,feed_type)
               VALUES (1,'bank-api','api') RETURNING id"""
        ).fetchone()[0]
        balance_id = connection.execute(
            """INSERT INTO balances(account_id,as_of,amount_minor,currency)
               VALUES (1,'2026-01-01',100,'ILS') RETURNING id"""
        ).fetchone()[0]
        connection.execute(
            """INSERT INTO balance_evidence
               (canonical_balance_id,account_feed_id,source_key,
                observed_amount_minor,observed_currency)
               VALUES (?,?,'private-source-key',100,'ILS')""",
            (balance_id, feed_id),
        )
        connection.execute("DROP TRIGGER account_feeds_identity_update")
        connection.execute("DELETE FROM schema_migrations WHERE version>=6")
        connection.execute(
            "UPDATE account_feeds SET account_id=2 WHERE id=?", (feed_id,)
        )
        connection.commit()

        with self.assertRaises(MigrationError) as caught:
            apply_migrations(connection)
        self.assertIn("balance_evidence=1", str(caught.exception))
        self.assertNotIn("private-source-key", str(caught.exception))
        self.assertEqual(schema_version(connection), 5)

    def test_feed_backed_trade_ids_are_scoped_by_source_event(self) -> None:
        connection = self.create_current()
        self.addCleanup(connection.close)
        account_id = connection.execute(
            """INSERT INTO accounts(name,kind,institution,currency)
               VALUES ('Broker','brokerage','Broker','USD') RETURNING id"""
        ).fetchone()[0]
        feed_ids = [
            connection.execute(
                """INSERT INTO account_feeds
                   (account_id,provider,feed_type,external_account_ref)
                   VALUES (?,?,'api',?) RETURNING id""",
                (account_id, provider, reference),
            ).fetchone()[0]
            for provider, reference in (("one", "one"), ("two", "two"))
        ]
        source_ids = [
            connection.execute(
                """INSERT INTO source_events
                   (account_feed_id,source_key,event_type,occurred_on)
                   VALUES (?,'provider-local-id','trade','2026-01-01') RETURNING id""",
                (feed_id,),
            ).fetchone()[0]
            for feed_id in feed_ids
        ]
        security_id = connection.execute(
            "INSERT INTO securities(ticker,currency) VALUES ('TEST','USD') RETURNING id"
        ).fetchone()[0]
        for source_id in source_ids:
            connection.execute(
                """INSERT INTO trades
                   (date,account_id,security_id,side,shares,price_minor,fees_minor,
                    currency,external_id,source_event_id)
                   VALUES ('2026-01-01',?,?,'buy',1,100,0,'USD',
                           'provider-local-id',?)""",
                (account_id, security_id, source_id),
            )

        connection.execute(
            """INSERT INTO trades
               (date,account_id,security_id,side,shares,price_minor,fees_minor,
                currency,external_id)
               VALUES ('2026-01-01',?,?,'buy',1,100,0,'USD','legacy-id')""",
            (account_id, security_id),
        )
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute(
                """INSERT INTO trades
                   (date,account_id,security_id,side,shares,price_minor,fees_minor,
                    currency,external_id)
                   VALUES ('2026-01-02',?,?,'buy',1,100,0,'USD','legacy-id')""",
                (account_id, security_id),
            )

    def test_v9_backfills_and_guards_snapshot_provenance(self) -> None:
        connection = self.create_current()
        self.addCleanup(connection.close)
        for name in (
            "balance_evidence_require_provenance_insert",
            "balance_evidence_require_provenance_update",
            "position_evidence_require_provenance_insert",
            "position_evidence_require_provenance_update",
        ):
            connection.execute(f'DROP TRIGGER "{name}"')
        connection.execute("DELETE FROM schema_migrations WHERE version>=9")

        account_id = connection.execute(
            """INSERT INTO accounts(name,kind,institution,currency)
               VALUES ('Legacy','brokerage','Broker','USD') RETURNING id"""
        ).fetchone()[0]
        security_id = connection.execute(
            "INSERT INTO securities(ticker,currency) VALUES ('TEST','USD') RETURNING id"
        ).fetchone()[0]
        balance_id = connection.execute(
            """INSERT INTO balances(account_id,as_of,amount_minor,currency)
               VALUES (?,'2026-01-01',100,'USD') RETURNING id""",
            (account_id,),
        ).fetchone()[0]
        position_id = connection.execute(
            """INSERT INTO positions(account_id,security_id,as_of,quantity,currency)
               VALUES (?,?,'2026-01-01',1,'USD') RETURNING id""",
            (account_id, security_id),
        ).fetchone()[0]
        connection.execute(
            """INSERT INTO balance_evidence
               (canonical_balance_id,source_key,observed_amount_minor,observed_currency)
               VALUES (?,'private-balance-key',100,'USD')""",
            (balance_id,),
        )
        connection.execute(
            """INSERT INTO position_evidence
               (canonical_position_id,source_key,observed_quantity,observed_currency)
               VALUES (?,'private-position-key',1,'USD')""",
            (position_id,),
        )
        connection.commit()

        applied = apply_migrations(connection)
        self.assertEqual(
            [migration.version for migration in applied], [9, 10, 11, 12, 13, 14, 15, 16]
        )
        feeds = connection.execute(
            """SELECT DISTINCT feed.account_id,feed.provider,feed.feed_type
               FROM account_feeds AS feed
               JOIN balance_evidence AS evidence
                 ON evidence.account_feed_id=feed.id
               WHERE evidence.canonical_balance_id=?""",
            (balance_id,),
        ).fetchall()
        self.assertEqual(
            feeds, [(account_id, "findash-legacy-snapshot", "manual")]
        )
        position_feed = connection.execute(
            """SELECT account_feed_id FROM position_evidence
               WHERE canonical_position_id=?""",
            (position_id,),
        ).fetchone()[0]
        balance_feed = connection.execute(
            """SELECT account_feed_id FROM balance_evidence
               WHERE canonical_balance_id=?""",
            (balance_id,),
        ).fetchone()[0]
        self.assertEqual(position_feed, balance_feed)

        with self.assertRaisesRegex(sqlite3.IntegrityError, "requires provenance"):
            connection.execute(
                """INSERT INTO balance_evidence
                   (canonical_balance_id,source_key,
                    observed_amount_minor,observed_currency)
                   VALUES (?,'new-key',100,'USD')""",
                (balance_id,),
            )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "requires provenance"):
            connection.execute(
                """INSERT INTO position_evidence
                   (canonical_position_id,source_key,
                    observed_quantity,observed_currency)
                   VALUES (?,'new-position-key',1,'USD')""",
                (position_id,),
            )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "requires provenance"):
            connection.execute(
                """UPDATE balance_evidence
                   SET account_feed_id=NULL,source_doc_id=NULL
                   WHERE canonical_balance_id=?""",
                (balance_id,),
            )

    def test_fact_validation_blocks_invalid_future_writes(self) -> None:
        connection = self.create_current()
        self.addCleanup(connection.close)
        account_id = connection.execute(
            """INSERT INTO accounts(name,kind,institution,currency)
               VALUES ('Bank','checking','Bank','ILS') RETURNING id"""
        ).fetchone()[0]
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute(
                """INSERT INTO accounts
                   (name,kind,institution,currency,opened_on,closed_on)
                   VALUES ('Backwards','checking','Bank','ILS','2026-02-01','2026-01-01')"""
            )

        invalid_transactions = (
            ("2026-01-01", 1.5, "ILS", None),
            ("2026-02-30", 100, "ILS", None),
            ("2026-01-01", 100, "ils", None),
            ("2026-01-01", 100, "ILS", "   "),
        )
        for row in invalid_transactions:
            with self.subTest(transaction=row), self.assertRaises(
                sqlite3.IntegrityError
            ):
                connection.execute(
                    """INSERT INTO transactions
                       (account_id,date,amount_minor,currency,component)
                       VALUES (?,?,?,?,?)""",
                    (account_id, *row),
                )

        transaction_id = connection.execute(
            """INSERT INTO transactions(account_id,date,amount_minor,currency)
               VALUES (?,'2026-01-01',-100,'ILS') RETURNING id""",
            (account_id,),
        ).fetchone()[0]
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE transactions SET amount_minor=1.5 WHERE id=?",
                (transaction_id,),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute(
                """INSERT INTO fx_rates(date,base_currency,quote_currency,rate)
                   VALUES ('2026-01-01','USD','ILS',0)"""
            )
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute(
                """INSERT INTO payslips
                   (period_start,period_end,employer,gross_minor,net_minor,currency)
                   VALUES ('2026-02-01','2026-01-31','Employer',100,90,'ILS')"""
            )

        security_id = connection.execute(
            "INSERT INTO securities(ticker,currency) VALUES ('TEST','USD') RETURNING id"
        ).fetchone()[0]
        canonical_cash_id = connection.execute(
            """INSERT INTO canonical_events
               (account_id,event_type,occurred_on,currency,amount_minor,flow_type,component)
               VALUES (?,'cash','2026-01-01','ILS',100,'transfer','cash_ils')
               RETURNING id""",
            (account_id,),
        ).fetchone()[0]
        self.assertEqual(
            connection.execute(
                "SELECT component FROM canonical_events WHERE id=?",
                (canonical_cash_id,),
            ).fetchone()[0],
            "cash_ils",
        )
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute(
                """INSERT INTO canonical_events
                   (account_id,event_type,occurred_on,currency,amount_minor,flow_type,component)
                   VALUES (?,'cash','2026-01-01','ILS',100,'transfer','   ')""",
                (account_id,),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute(
                """INSERT INTO canonical_events
                   (account_id,event_type,occurred_on,currency,security_id,side,
                    quantity,price_minor,fees_minor,component)
                   VALUES (?,'trade','2026-01-01','USD',?,'buy',1,100,0,'cash_usd')""",
                (account_id, security_id),
            )
        for price, fee in ((-1, 0), (100, -1)):
            with self.subTest(price=price, fee=fee), self.assertRaises(
                sqlite3.IntegrityError
            ):
                connection.execute(
                    """INSERT INTO trades
                       (date,account_id,security_id,side,shares,price_minor,fees_minor,currency)
                       VALUES ('2026-01-01',?,?,'buy',1,?,?,'USD')""",
                    (account_id, security_id, price, fee),
                )

        for shares in (0, -1, float("inf")):
            with self.subTest(shares=shares), self.assertRaises(
                sqlite3.IntegrityError
            ):
                connection.execute(
                    """INSERT INTO trades
                       (date,account_id,security_id,side,shares,price_minor,fees_minor,currency)
                       VALUES ('2026-01-01',?,?,'buy',?,100,0,'USD')""",
                    (account_id, security_id, shares),
                )

        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute(
                """INSERT INTO positions
                   (account_id,security_id,as_of,quantity,currency)
                   VALUES (?,?,'2026-01-01',?,'USD')""",
                (account_id, security_id, float("inf")),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute(
                """INSERT INTO canonical_events
                   (account_id,event_type,occurred_on,currency,security_id,side,
                    quantity,price_minor,fees_minor)
                   VALUES (?,'trade','2026-01-01','USD',?,'buy',?,100,0)""",
                (account_id, security_id, float("inf")),
            )

        self.assertEqual(
            connection.execute(
                "SELECT amount_minor,currency FROM transactions WHERE id=?",
                (transaction_id,),
            ).fetchone(),
            (-100, "ILS"),
        )

    def test_two_feeds_support_one_event_without_deleting_raw_rows(self) -> None:
        connection = self.create_current()
        self.addCleanup(connection.close)
        account_id = connection.execute(
            """INSERT INTO accounts(name,kind,institution,currency)
               VALUES ('Wrapper account','brokerage','Wrapper','USD') RETURNING id"""
        ).fetchone()[0]
        wrapper_feed = connection.execute(
            """INSERT INTO account_feeds(account_id,provider,feed_type,label)
               VALUES (?,'wrapper-statement','document','Wrapper') RETURNING id""",
            (account_id,),
        ).fetchone()[0]
        ibkr_feed = connection.execute(
            """INSERT INTO account_feeds(account_id,provider,feed_type,label)
               VALUES (?,'ibkr','api','IBKR') RETURNING id""",
            (account_id,),
        ).fetchone()[0]
        canonical_id = connection.execute(
            """INSERT INTO canonical_events
               (account_id,event_type,occurred_on,currency,amount_minor,flow_type,category)
               VALUES (?,'cash','2026-01-02','USD',10000,'transfer','deposit') RETURNING id""",
            (account_id,),
        ).fetchone()[0]

        source_ids = []
        # Source ids are scoped to a feed: two providers may coincidentally use the
        # same key, while a rerun of either provider may not insert it twice.
        for feed_id, key in ((wrapper_feed, "event:1"), (ibkr_feed, "event:1")):
            source_id = connection.execute(
                """INSERT INTO source_events(account_feed_id,source_key,event_type,occurred_on)
                   VALUES (?,?,'cash','2026-01-02') RETURNING id""",
                (feed_id, key),
            ).fetchone()[0]
            source_ids.append(source_id)
            connection.execute(
                """INSERT INTO transactions
                   (account_id,date,amount_minor,currency,category,flow_type,
                    source_event_id,canonical_event_id)
                   VALUES (?,'2026-01-02',10000,'USD','deposit','transfer',?,?)""",
                (account_id, source_id, canonical_id),
            )

        connection.execute(
            """INSERT INTO event_evidence
               (canonical_event_id,source_event_id,evidence_role,match_method,confidence)
               VALUES (?,?,'primary','source-detail',1.0)""",
            (canonical_id, source_ids[0]),
        )
        connection.execute(
            """INSERT INTO event_evidence
               (canonical_event_id,source_event_id,evidence_role,match_method,confidence)
               VALUES (?,?,'supporting','matched-fields',1.0)""",
            (canonical_id, source_ids[1]),
        )

        self.assertEqual(connection.execute("SELECT COUNT(*) FROM transactions").fetchone()[0], 2)
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM canonical_events").fetchone()[0], 1)
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM event_evidence").fetchone()[0], 2)
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute(
                """INSERT INTO source_events(account_feed_id,source_key,event_type,occurred_on)
                   VALUES (?,'event:1','cash','2026-01-02')""",
                (ibkr_feed,),
            )

    def test_raw_pointers_and_evidence_must_agree_in_both_insert_orders(self) -> None:
        connection = self.create_current()
        self.addCleanup(connection.close)
        account_id = connection.execute(
            """INSERT INTO accounts(name,kind,institution,currency)
               VALUES ('Broker','brokerage','Test','USD') RETURNING id"""
        ).fetchone()[0]
        feed_id = connection.execute(
            """INSERT INTO account_feeds(account_id,provider,feed_type)
               VALUES (?,'test-feed','api') RETURNING id""",
            (account_id,),
        ).fetchone()[0]
        security_id = connection.execute(
            "INSERT INTO securities(ticker,currency) VALUES ('POINTER','USD') RETURNING id"
        ).fetchone()[0]

        def source(event_type: str, key: str) -> int:
            return int(
                connection.execute(
                    """INSERT INTO source_events(
                           account_feed_id,source_key,event_type,occurred_on
                       ) VALUES (?,?,?,'2026-01-02') RETURNING id""",
                    (feed_id, key, event_type),
                ).fetchone()[0]
            )

        def canonical(event_type: str) -> int:
            if event_type == "cash":
                return int(
                    connection.execute(
                        """INSERT INTO canonical_events(
                               account_id,event_type,occurred_on,currency,
                               amount_minor,flow_type
                           ) VALUES (?,'cash','2026-01-02','USD',100,'income')
                           RETURNING id""",
                        (account_id,),
                    ).fetchone()[0]
                )
            return int(
                connection.execute(
                    """INSERT INTO canonical_events(
                           account_id,event_type,occurred_on,currency,security_id,
                           side,quantity,price_minor,fees_minor
                       ) VALUES (?,'trade','2026-01-02','USD',?,'buy',1,100,0)
                       RETURNING id""",
                    (account_id, security_id),
                ).fetchone()[0]
            )

        def raw(
            event_type: str, source_id: int | None, canonical_id: int
        ) -> tuple[str, int]:
            if event_type == "cash":
                row_id = connection.execute(
                    """INSERT INTO transactions(
                           account_id,date,amount_minor,currency,flow_type,
                           source_event_id,canonical_event_id
                       ) VALUES (?,'2026-01-02',100,'USD','income',?,?)
                       RETURNING id""",
                    (account_id, source_id, canonical_id),
                ).fetchone()[0]
                return "transactions", int(row_id)
            row_id = connection.execute(
                """INSERT INTO trades(
                       account_id,security_id,date,side,shares,price_minor,
                       fees_minor,currency,source_event_id,canonical_event_id
                   ) VALUES (?,?,'2026-01-02','buy',1,100,0,'USD',?,?)
                   RETURNING id""",
                (account_id, security_id, source_id, canonical_id),
            ).fetchone()[0]
            return "trades", int(row_id)

        for event_type in ("cash", "trade"):
            with self.subTest(event_type=event_type):
                first_canonical = canonical(event_type)
                second_canonical = canonical(event_type)

                with self.assertRaisesRegex(
                    sqlite3.IntegrityError,
                    "raw event pointer contradicts evidence",
                ):
                    raw(event_type, None, first_canonical)

                raw_first_source = source(event_type, f"{event_type}-raw-first")
                raw(event_type, raw_first_source, first_canonical)
                with self.assertRaisesRegex(
                    sqlite3.IntegrityError,
                    "event evidence contradicts raw pointer",
                ):
                    connection.execute(
                        """INSERT INTO event_evidence(
                               canonical_event_id,source_event_id,
                               evidence_role,match_method
                           ) VALUES (?,?,'supporting','reviewed')""",
                        (second_canonical, raw_first_source),
                    )
                connection.execute(
                    """INSERT INTO event_evidence(
                           canonical_event_id,source_event_id,
                           evidence_role,match_method
                       ) VALUES (?,?,'supporting','reviewed')""",
                    (first_canonical, raw_first_source),
                )
                with self.assertRaisesRegex(
                    sqlite3.IntegrityError,
                    "event evidence contradicts raw pointer",
                ):
                    connection.execute(
                        """UPDATE event_evidence SET evidence_role='aggregate'
                           WHERE canonical_event_id=? AND source_event_id=?""",
                        (first_canonical, raw_first_source),
                    )

                evidence_first_source = source(
                    event_type, f"{event_type}-evidence-first"
                )
                connection.execute(
                    """INSERT INTO event_evidence(
                           canonical_event_id,source_event_id,
                           evidence_role,match_method
                       ) VALUES (?,?,'supporting','reviewed')""",
                    (second_canonical, evidence_first_source),
                )
                with self.assertRaisesRegex(
                    sqlite3.IntegrityError,
                    "raw event pointer contradicts evidence",
                ):
                    raw(event_type, evidence_first_source, first_canonical)
                table, row_id = raw(
                    event_type, evidence_first_source, second_canonical
                )
                with self.assertRaisesRegex(
                    sqlite3.IntegrityError,
                    "raw event pointer contradicts evidence",
                ):
                    connection.execute(
                        f"UPDATE {table} SET canonical_event_id=? WHERE id=?",
                        (first_canonical, row_id),
                    )

    def test_evidence_cannot_cross_logical_accounts(self) -> None:
        connection = self.create_current()
        self.addCleanup(connection.close)
        connection.executemany(
            """INSERT INTO accounts(name,kind,institution,currency)
               VALUES (?,'checking','Bank','ILS')""",
            [("One",), ("Two",)],
        )
        feed_id = connection.execute(
            """INSERT INTO account_feeds(account_id,provider,feed_type)
               VALUES (2,'bank-api','api') RETURNING id"""
        ).fetchone()[0]
        source_id = connection.execute(
            """INSERT INTO source_events(account_feed_id,source_key,event_type,occurred_on)
               VALUES (?,'event-1','cash','2026-01-01') RETURNING id""",
            (feed_id,),
        ).fetchone()[0]
        canonical_id = connection.execute(
            """INSERT INTO canonical_events
               (account_id,event_type,occurred_on,currency,amount_minor,flow_type)
               VALUES (1,'cash','2026-01-01','ILS',100,'income') RETURNING id"""
        ).fetchone()[0]
        with self.assertRaisesRegex(sqlite3.IntegrityError, "account/type mismatch"):
            connection.execute(
                """INSERT INTO event_evidence
                   (canonical_event_id,source_event_id,match_method)
                   VALUES (?,?,'matched-fields')""",
                (canonical_id, source_id),
            )

        balance_id = connection.execute(
            """INSERT INTO balances(account_id,as_of,amount_minor,currency)
               VALUES (1,'2026-01-01',100,'ILS') RETURNING id"""
        ).fetchone()[0]
        with self.assertRaisesRegex(sqlite3.IntegrityError, "account mismatch"):
            connection.execute(
                """INSERT INTO balance_evidence
                   (canonical_balance_id,account_feed_id,source_key,
                    observed_amount_minor,observed_currency)
                   VALUES (?,?,'balance-1',100,'ILS')""",
                (balance_id, feed_id),
            )

        security_id = connection.execute(
            "INSERT INTO securities(ticker,currency) VALUES ('TEST','USD') RETURNING id"
        ).fetchone()[0]
        position_id = connection.execute(
            """INSERT INTO positions(account_id,security_id,as_of,quantity,currency)
               VALUES (1,?,'2026-01-01',1,'USD') RETURNING id""",
            (security_id,),
        ).fetchone()[0]
        with self.assertRaisesRegex(sqlite3.IntegrityError, "account mismatch"):
            connection.execute(
                """INSERT INTO position_evidence
                   (canonical_position_id,account_feed_id,source_key,
                    observed_quantity,observed_currency)
                   VALUES (?,?,'position-1',1,'USD')""",
                (position_id, feed_id),
            )

        with self.assertRaisesRegex(sqlite3.IntegrityError, "feed with evidence"):
            connection.execute("DELETE FROM account_feeds WHERE id=?", (feed_id,))

    def test_linked_parent_identities_are_immutable(self) -> None:
        connection = self.create_current()
        self.addCleanup(connection.close)
        connection.executemany(
            """INSERT INTO accounts(name,kind,institution,currency)
               VALUES (?,'brokerage','Broker','USD')""",
            [("One",), ("Two",)],
        )
        feed_one = connection.execute(
            """INSERT INTO account_feeds(account_id,provider,feed_type)
               VALUES (1,'provider-one','api') RETURNING id"""
        ).fetchone()[0]
        feed_two = connection.execute(
            """INSERT INTO account_feeds(account_id,provider,feed_type)
               VALUES (2,'provider-two','api') RETURNING id"""
        ).fetchone()[0]
        source_id = connection.execute(
            """INSERT INTO source_events
               (account_feed_id,source_key,event_type,occurred_on)
               VALUES (?,'source-1','cash','2026-01-01') RETURNING id""",
            (feed_one,),
        ).fetchone()[0]
        canonical_id = connection.execute(
            """INSERT INTO canonical_events
               (account_id,event_type,occurred_on,currency,amount_minor,flow_type)
               VALUES (1,'cash','2026-01-01','USD',100,'income') RETURNING id"""
        ).fetchone()[0]
        connection.execute(
            """INSERT INTO transactions
               (account_id,date,amount_minor,currency,flow_type,
                source_event_id,canonical_event_id)
               VALUES (1,'2026-01-01',100,'USD','income',?,?)""",
            (source_id, canonical_id),
        )
        connection.execute(
            """INSERT INTO event_evidence
               (canonical_event_id,source_event_id,evidence_role,match_method)
               VALUES (?,?,'primary','provider-id')""",
            (canonical_id, source_id),
        )
        balance_id = connection.execute(
            """INSERT INTO balances(account_id,as_of,amount_minor,currency)
               VALUES (1,'2026-01-01',100,'USD') RETURNING id"""
        ).fetchone()[0]
        connection.execute(
            """INSERT INTO balance_evidence
               (canonical_balance_id,account_feed_id,source_key,
                observed_amount_minor,observed_currency)
               VALUES (?,?,'balance-1',100,'USD')""",
            (balance_id, feed_one),
        )
        security_id = connection.execute(
            "INSERT INTO securities(ticker,currency) VALUES ('TEST','USD') RETURNING id"
        ).fetchone()[0]
        second_security_id = connection.execute(
            "INSERT INTO securities(ticker,currency) VALUES ('NEXT','USD') RETURNING id"
        ).fetchone()[0]
        position_id = connection.execute(
            """INSERT INTO positions(account_id,security_id,as_of,quantity,currency)
               VALUES (1,?,'2026-01-01',1,'USD') RETURNING id""",
            (security_id,),
        ).fetchone()[0]
        connection.execute(
            """INSERT INTO position_evidence
               (canonical_position_id,account_feed_id,source_key,
                observed_quantity,observed_currency)
               VALUES (?,?,'position-1',1,'USD')""",
            (position_id, feed_one),
        )

        blocked_updates = (
            (
                "account feed identity",
                "UPDATE account_feeds SET account_id=2 WHERE id=?",
                (feed_one,),
            ),
            (
                "account feed identity",
                "UPDATE account_feeds SET provider='changed' WHERE id=?",
                (feed_one,),
            ),
            (
                "account feed identity",
                "UPDATE account_feeds SET external_account_ref='changed' WHERE id=?",
                (feed_one,),
            ),
            (
                "source event identity",
                "UPDATE source_events SET account_feed_id=? WHERE id=?",
                (feed_two, source_id),
            ),
            (
                "source event identity",
                "UPDATE source_events SET event_type='trade' WHERE id=?",
                (source_id,),
            ),
            (
                "source event identity",
                "UPDATE source_events SET source_key='changed' WHERE id=?",
                (source_id,),
            ),
            (
                "canonical event identity",
                "UPDATE canonical_events SET account_id=2 WHERE id=?",
                (canonical_id,),
            ),
            (
                "canonical event identity",
                "UPDATE canonical_events SET event_type='trade' WHERE id=?",
                (canonical_id,),
            ),
            (
                "balance identity",
                "UPDATE balances SET account_id=2 WHERE id=?",
                (balance_id,),
            ),
            (
                "balance identity",
                "UPDATE balances SET as_of='2026-01-02' WHERE id=?",
                (balance_id,),
            ),
            (
                "balance identity",
                "UPDATE balances SET component='cash_usd' WHERE id=?",
                (balance_id,),
            ),
            (
                "position identity",
                "UPDATE positions SET account_id=2 WHERE id=?",
                (position_id,),
            ),
            (
                "position identity",
                "UPDATE positions SET security_id=? WHERE id=?",
                (second_security_id, position_id),
            ),
            (
                "position identity",
                "UPDATE positions SET as_of='2026-01-02' WHERE id=?",
                (position_id,),
            ),
        )
        for message, statement, parameters in blocked_updates:
            with self.subTest(statement=statement), self.assertRaisesRegex(
                sqlite3.IntegrityError, message
            ):
                connection.execute(statement, parameters)

        self.assertEqual(
            connection.execute(
                "SELECT account_id FROM account_feeds WHERE id=?", (feed_one,)
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            connection.execute(
                "SELECT account_feed_id,event_type FROM source_events WHERE id=?",
                (source_id,),
            ).fetchone(),
            (feed_one, "cash"),
        )
        self.assertEqual(
            connection.execute(
                "SELECT account_id,event_type FROM canonical_events WHERE id=?",
                (canonical_id,),
            ).fetchone(),
            (1, "cash"),
        )
        self.assertEqual(
            connection.execute("SELECT account_id FROM balances WHERE id=?", (balance_id,)).fetchone()[0],
            1,
        )
        self.assertEqual(
            connection.execute("SELECT account_id FROM positions WHERE id=?", (position_id,)).fetchone()[0],
            1,
        )

    def test_newer_schema_version_is_rejected(self) -> None:
        connection = self.create_current()
        connection.execute(
            "INSERT INTO schema_migrations(version,name) VALUES (99,'future')"
        )
        connection.commit()
        connection.close()
        with self.assertRaisesRegex(MigrationError, "newer than"):
            connect_db(self.db)


if __name__ == "__main__":
    unittest.main()
