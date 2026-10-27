from __future__ import annotations

import argparse
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import sqlite3
import stat
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import findash_db as gateway_module  # noqa: E402
from findash_core.corrections import (  # noqa: E402
    CorrectionError,
    remove_incorrect_balance,
)
from findash_core.migrations import MigrationError  # noqa: E402
from findash_core.ledger.engine import IntentError  # noqa: E402
from findash_db import (  # noqa: E402
    GatewayError,
    apply_batch,
    query_database,
    validate_mutation,
    validate_query,
)


class DatabaseGatewayTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "finance.db"
        con = sqlite3.connect(self.db)
        con.executescript((ROOT / "scripts" / "init-db.sql").read_text())
        con.close()

    def tearDown(self):
        self.temp.cleanup()

    def test_rejects_shell_attach_and_multiple_statements(self):
        for sql in (
            ".shell id",
            "ATTACH DATABASE '/tmp/other' AS other",
            "SELECT 1; SELECT 2",
            "SELECT readfile('/etc/passwd')",
        ):
            with self.subTest(sql=sql), self.assertRaises(GatewayError):
                validate_query(sql)

    def test_update_requires_where_and_schema_changes_are_rejected(self):
        with self.assertRaises(GatewayError):
            validate_mutation("UPDATE accounts SET name='x'")
        with self.assertRaises(GatewayError):
            validate_mutation("DROP TABLE accounts")
        with self.assertRaises(GatewayError):
            validate_mutation("DELETE FROM accounts WHERE id=1")
        with self.assertRaises(GatewayError):
            validate_mutation("INSERT OR REPLACE INTO accounts(id,name) VALUES(1,'x')")
        for statement in (
            "UPDATE schema_migrations SET applied_at='x' WHERE version=11",
            "UPDATE \"schema_migrations\" SET name='x' WHERE version=11",
            "INSERT INTO schema_migrations(version,name) VALUES(99,'future')",
        ):
            with self.subTest(statement=statement), self.assertRaisesRegex(
                GatewayError, "migration metadata"
            ):
                validate_mutation(statement)

    @staticmethod
    def raw(statements: list[dict]) -> dict:
        return {"type": "raw_sql", "reason": "test fixture", "statements": statements}

    def test_requests_hold_only_intents(self):
        for payload in (
            {"statements": [{"sql": "INSERT INTO accounts(name) VALUES('x')"}]},
            {"statements": [], "intents": [{"type": "meta", "key": "a", "value": "b"}]},
            {"intents": []},
        ):
            with self.subTest(payload=payload), self.assertRaises((GatewayError, IntentError)):
                apply_batch(self.db, payload)

    def test_batch_is_atomic_and_foreign_keys_are_on(self):
        payload = {
            "intents": [
                {
                    "type": "account",
                    "name": "Cash",
                    "kind": "checking",
                    "institution": "Bank",
                    "currency": "ILS",
                },
                self.raw(
                    [
                        {
                            "sql": "INSERT INTO transactions(account_id,date,amount_minor,currency) VALUES(?,?,?,?)",
                            "params": [999, "2026-01-01", 100, "ILS"],
                        }
                    ]
                ),
            ],
        }
        with self.assertRaises(sqlite3.IntegrityError):
            apply_batch(self.db, payload)
        self.assertEqual(query_database(self.db, "SELECT COUNT(*) AS n FROM accounts"), [{"n": 0}])

    def test_intent_batch_and_query(self):
        account = {
            "type": "account",
            "name": "Cash",
            "kind": "checking",
            "institution": "Bank",
            "currency": "ILS",
        }
        self.assertEqual(apply_batch(self.db, {"intents": [account]}), {"intents": {"account:created": 1}})
        self.assertEqual(apply_batch(self.db, {"intents": [account]}), {"intents": {"account:unchanged": 1}})
        self.assertEqual(
            query_database(self.db, "SELECT kind FROM accounts WHERE name=?", ["Cash"]),
            [{"kind": "checking"}],
        )

    def test_batch_validates_completed_normalized_event_graph_before_commit(self):
        common = [
            {
                "sql": "INSERT INTO accounts(id,name,kind,institution,currency) VALUES(1,?,?,?,?)",
                "params": ["Cash", "checking", "Bank", "ILS"],
            },
            {
                "sql": "INSERT INTO account_feeds(id,account_id,provider,feed_type) VALUES(1,1,?,?)",
                "params": ["bank", "api"],
            },
            {
                "sql": "INSERT INTO source_events(id,account_feed_id,source_key,event_type,occurred_on) VALUES(1,1,?,?,?)",
                "params": ["source-1", "cash", "2026-01-01"],
            },
            {
                "sql": "INSERT INTO canonical_events(id,account_id,event_type,occurred_on,currency,amount_minor,flow_type) VALUES(1,1,?,?,?,?,?)",
                "params": ["cash", "2026-01-01", "ILS", 100, "income"],
            },
            {
                "sql": "INSERT INTO transactions(id,account_id,date,amount_minor,currency,source_event_id,canonical_event_id) VALUES(1,1,?,?,?,?,?)",
                "params": ["2026-01-01", 100, "ILS", 1, 1],
            },
        ]

        with self.assertRaisesRegex(MigrationError, "transaction_event_pointer=1"):
            apply_batch(self.db, {"intents": [self.raw(common)]})
        self.assertEqual(
            query_database(self.db, "SELECT COUNT(*) AS n FROM accounts"),
            [{"n": 0}],
        )

        result = apply_batch(
            self.db,
            {
                "intents": [
                    self.raw(
                        [
                            *common,
                            {
                                "sql": "INSERT INTO event_evidence(canonical_event_id,source_event_id,evidence_role,match_method) VALUES(1,1,?,?)",
                                "params": ["primary", "provider identity"],
                            },
                        ]
                    )
                ]
            },
        )
        self.assertEqual(result["intents"], {"raw_sql:applied": 1})
        self.assertEqual(
            query_database(
                self.db, "SELECT kind, summary FROM ledger_log ORDER BY id"
            ),
            [
                {"kind": "raw_sql", "summary": "test fixture"},
                {"kind": "intents", "summary": "raw_sql=1"},
            ],
        )

    def test_batch_rejects_trade_cash_links_with_wrong_signed_economics(self):
        def statements(cash_amount_minor: int) -> list[dict]:
            return [
                {
                    "sql": "INSERT INTO accounts(id,name,kind,institution,currency) VALUES(1,?,?,?,?)",
                    "params": ["Broker", "brokerage", "Test", "USD"],
                },
                {
                    "sql": "INSERT INTO securities(id,ticker,currency) VALUES(1,?,?)",
                    "params": ["TEST", "USD"],
                },
                {
                    "sql": "INSERT INTO canonical_events(id,account_id,event_type,occurred_on,currency,amount_minor,flow_type) VALUES(1,1,?,?,?,?,?)",
                    "params": [
                        "cash",
                        "2026-01-02",
                        "USD",
                        cash_amount_minor,
                        "investment",
                    ],
                },
                {
                    "sql": "INSERT INTO canonical_events(id,account_id,event_type,occurred_on,currency,security_id,side,quantity,price_minor,fees_minor) VALUES(2,1,?,?,?,?,?,?,?,?)",
                    "params": [
                        "trade",
                        "2026-01-01",
                        "USD",
                        1,
                        "buy",
                        1,
                        100,
                        1,
                    ],
                },
                {
                    "sql": "INSERT INTO trade_cash_links(cash_event_id,trade_event_id,match_method) VALUES(1,2,?)",
                    "params": ["reviewed provider identities"],
                },
            ]

        with self.assertRaisesRegex(MigrationError, "trade_cash_economics=1"):
            apply_batch(self.db, {"intents": [self.raw(statements(-500))]})
        self.assertEqual(
            query_database(self.db, "SELECT COUNT(*) AS n FROM accounts"),
            [{"n": 0}],
        )

        self.assertEqual(
            apply_batch(self.db, {"intents": [self.raw(statements(-101))]})["intents"],
            {"raw_sql:applied": 1},
        )

    def test_request_files_reject_symlinked_queue_ancestors(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            repo = base / "repo"
            repo.mkdir()
            outside = base / "outside"
            operations = outside / "staging" / "operations"
            operations.mkdir(parents=True)
            request = operations / "request.json"
            request.write_text('{"sql":"SELECT 1"}')
            (repo / "inbox").symlink_to(outside, target_is_directory=True)

            old = (gateway_module.ROOT, gateway_module.BATCH_ROOT)
            gateway_module.ROOT = repo
            gateway_module.BATCH_ROOT = repo / "inbox" / "staging" / "operations"
            try:
                with self.assertRaisesRegex(argparse.ArgumentTypeError, "symlinks"):
                    gateway_module._request_path(str(request))
            finally:
                gateway_module.ROOT, gateway_module.BATCH_ROOT = old

    def test_request_files_reject_nested_and_final_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            operations = repo / "inbox" / "staging" / "operations"
            real = operations / "real"
            real.mkdir(parents=True)
            request = real / "request.json"
            request.write_text('{"sql":"SELECT 1"}')
            nested = operations / "nested"
            nested.symlink_to(real, target_is_directory=True)
            final_link = operations / "linked.json"
            final_link.symlink_to(request)

            old = (gateway_module.ROOT, gateway_module.BATCH_ROOT)
            gateway_module.ROOT = repo
            gateway_module.BATCH_ROOT = operations
            try:
                self.assertEqual(gateway_module._request_path(str(request)), request)
                self.assertEqual(stat.S_IMODE(request.stat().st_mode), 0o600)
                with self.assertRaisesRegex(argparse.ArgumentTypeError, "symlinks"):
                    gateway_module._request_path(str(nested / "request.json"))
                with self.assertRaisesRegex(argparse.ArgumentTypeError, "regular"):
                    gateway_module._request_path(str(final_link))
            finally:
                gateway_module.ROOT, gateway_module.BATCH_ROOT = old

    def test_request_file_size_is_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            operations = repo / "inbox" / "staging" / "operations"
            operations.mkdir(parents=True)
            request = operations / "large.json"
            request.write_text("{}")

            old = (
                gateway_module.ROOT,
                gateway_module.BATCH_ROOT,
                gateway_module.MAX_REQUEST_BYTES,
            )
            gateway_module.ROOT = repo
            gateway_module.BATCH_ROOT = operations
            gateway_module.MAX_REQUEST_BYTES = 1
            try:
                with self.assertRaisesRegex(argparse.ArgumentTypeError, "too large"):
                    gateway_module._request_path(str(request))
            finally:
                (
                    gateway_module.ROOT,
                    gateway_module.BATCH_ROOT,
                    gateway_module.MAX_REQUEST_BYTES,
                ) = old

    def _balance_correction_fixture(self) -> dict:
        connection = sqlite3.connect(self.db)
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            """INSERT INTO documents(id,drive_id,drive_path,filename,notes)
               VALUES(1,'source-1','vault/source.pdf','source.pdf','Original note')"""
        )
        connection.execute(
            """INSERT INTO accounts(id,name,kind,institution,currency)
               VALUES(1,'private-account','savings','Bank','ILS')"""
        )
        connection.execute(
            """INSERT INTO balances(
                   id,account_id,as_of,component,amount_minor,currency,source_doc_id
               ) VALUES(1,1,'2024-01-02',NULL,12345,'ILS',1)"""
        )
        connection.execute(
            """INSERT INTO balance_evidence(
                   id,canonical_balance_id,source_doc_id,source_key,
                   observed_amount_minor,observed_currency,account_snapshot_complete
               ) VALUES(1,1,1,'source-key',12345,'ILS',1)"""
        )
        connection.commit()
        connection.close()
        return {
            "operation": "remove_incorrect_balance",
            "balance_id": 1,
            "expected_account_id": 1,
            "expected_as_of": "2024-01-02",
            "expected_component": None,
            "expected_amount_minor": 12345,
            "expected_currency": "ILS",
            "expected_source_doc_ids": [1],
            "expected_evidence_count": 1,
            "confirmed_incorrect_historical_interpretation": True,
            "confirmed_source_documents_preserved": True,
            "reason": "it duplicated a later observation",
        }

    def test_remove_incorrect_balance_is_guarded_and_preserves_document(self):
        request = self._balance_correction_fixture()
        self.assertEqual(
            remove_incorrect_balance(self.db, request),
            {
                "balances_removed": 1,
                "balance_evidence_removed": 1,
                "documents_preserved": 1,
            },
        )
        self.assertEqual(
            query_database(self.db, "SELECT COUNT(*) AS n FROM balances"), [{"n": 0}]
        )
        self.assertEqual(
            query_database(self.db, "SELECT COUNT(*) AS n FROM balance_evidence"),
            [{"n": 0}],
        )
        rows = query_database(
            self.db, "SELECT filename,notes FROM documents WHERE id=?", [1]
        )
        self.assertEqual(rows[0]["filename"], "source.pdf")
        self.assertIn("Original note", rows[0]["notes"])
        self.assertIn("Source document retained.", rows[0]["notes"])

    def test_remove_incorrect_balance_rolls_back_when_guard_is_stale(self):
        request = self._balance_correction_fixture()
        request["expected_evidence_count"] = 0
        with self.assertRaisesRegex(CorrectionError, "evidence count"):
            remove_incorrect_balance(self.db, request)
        self.assertEqual(
            query_database(self.db, "SELECT COUNT(*) AS n FROM balances"), [{"n": 1}]
        )
        rows = query_database(self.db, "SELECT notes FROM documents WHERE id=?", [1])
        self.assertEqual(rows, [{"notes": "Original note"}])

    def test_remove_balance_command_outputs_counts_only(self):
        payload = self._balance_correction_fixture()
        repo = Path(self.temp.name)
        operations = repo / "inbox" / "staging" / "operations"
        operations.mkdir(parents=True)
        request = operations / "correction.json"
        request.write_text(json.dumps(payload))

        old = (gateway_module.ROOT, gateway_module.BATCH_ROOT, gateway_module.DB)
        gateway_module.ROOT = repo
        gateway_module.BATCH_ROOT = operations
        gateway_module.DB = self.db
        output = io.StringIO()
        errors = io.StringIO()
        try:
            with redirect_stdout(output), redirect_stderr(errors):
                status = gateway_module.main(
                    ["remove-incorrect-balance", str(request)]
                )
        finally:
            gateway_module.ROOT, gateway_module.BATCH_ROOT, gateway_module.DB = old

        self.assertEqual(status, 0)
        self.assertEqual(errors.getvalue(), "")
        result = json.loads(output.getvalue())
        self.assertEqual(result["status"], "ok")
        self.assertTrue(all(isinstance(value, int) for value in result["counts"].values()))
        self.assertNotIn("private-account", output.getvalue())
        self.assertNotIn("12345", output.getvalue())

    def test_reconcile_command_is_fixed_path_and_outputs_counts_only(self):
        connection = sqlite3.connect(self.db)
        connection.executemany(
            """INSERT INTO accounts(name,kind,institution,currency)
               VALUES (?,'brokerage','Broker','USD')""",
            [("private-target",), ("private-source",)],
        )
        connection.commit()
        connection.close()

        repo = Path(self.temp.name)
        operations = repo / "inbox" / "staging" / "operations"
        operations.mkdir(parents=True)
        request = operations / "reconcile.json"
        request.write_text(
            json.dumps(
                {
                    "operation": "merge_duplicate_logical_account",
                    "source_account_id": 2,
                    "target_account_id": 1,
                    "confirmed_same_economic_account": True,
                    "confirmed_reviewed_resolutions": True,
                    "confirmed_unmapped_events_are_distinct": True,
                    "event_resolutions": [],
                    "balance_resolutions": [],
                    "position_resolutions": [],
                }
            )
        )
        old = (
            gateway_module.ROOT,
            gateway_module.BATCH_ROOT,
            gateway_module.DB,
        )
        gateway_module.ROOT = repo
        gateway_module.BATCH_ROOT = operations
        gateway_module.DB = self.db
        output = io.StringIO()
        errors = io.StringIO()
        try:
            with redirect_stdout(output), redirect_stderr(errors):
                status = gateway_module.main(
                    ["reconcile-account", str(request)]
                )
        finally:
            (
                gateway_module.ROOT,
                gateway_module.BATCH_ROOT,
                gateway_module.DB,
            ) = old

        self.assertEqual(status, 0)
        self.assertEqual(errors.getvalue(), "")
        result = json.loads(output.getvalue())
        self.assertEqual(result["status"], "ok")
        self.assertTrue(all(isinstance(value, int) for value in result["counts"].values()))
        self.assertNotIn("source_account_id", result["counts"])
        self.assertNotIn("target_account_id", result["counts"])
        self.assertNotIn("private-source", output.getvalue())
        self.assertNotIn("private-target", output.getvalue())


if __name__ == "__main__":
    unittest.main()
