from __future__ import annotations

from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from findash_core.db import connect_db  # noqa: E402
from findash_core.ledger.engine import IntentError, LedgerWriter  # noqa: E402
from findash_core.migrations import require_current_schema  # noqa: E402


class ReferenceFixture(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path = Path(temp.name) / "finance.db"
        seed = sqlite3.connect(path)
        seed.executescript((ROOT / "scripts" / "init-db.sql").read_text(encoding="utf-8"))
        seed.close()
        self.con = connect_db(path)
        self.con.row_factory = sqlite3.Row
        self.addCleanup(self.con.close)
        self.apply(
            {"type": "account", "name": "Checking", "kind": "checking",
             "institution": "Bank", "currency": "ILS"},
            {"type": "account", "name": "Broker", "kind": "brokerage",
             "institution": "Broker", "currency": "USD"},
            {"type": "feed", "account": "Broker", "provider": "broker", "feed_type": "api"},
            {"type": "document", "drive_id": "doc-1", "drive_path": "full-statements/a.xlsx",
             "filename": "a.xlsx", "doc_type": "bank_statement", "doc_date": "2026-10-31"},
        )

    def apply(self, *intents: dict) -> dict:
        with self.con:
            outcome = LedgerWriter(self.con).apply(list(intents))
            require_current_schema(self.con)
        return outcome

    def rows(self, sql: str, params: tuple = ()) -> list[tuple]:
        return [tuple(row) for row in self.con.execute(sql, params)]


class DocumentAccountFeedTests(ReferenceFixture):
    def test_document_is_idempotent_and_refinable(self) -> None:
        doc = {"type": "document", "drive_id": "doc-1", "drive_path": "full-statements/a.xlsx",
               "filename": "a.xlsx"}
        self.assertEqual(self.apply(doc), {"document:unchanged": 1})
        self.assertEqual(
            self.apply({**doc, "drive_path": "full-statements/2026-10/a.xlsx", "notes": "filed"}),
            {"document:updated": 1},
        )
        self.assertEqual(
            self.rows("SELECT drive_path, notes, doc_type FROM documents"),
            [("full-statements/2026-10/a.xlsx", "filed", "bank_statement")],
        )
        self.apply({**doc, "raw_hash": "aaa"})
        with self.assertRaisesRegex(IntentError, "different bytes"):
            self.apply({**doc, "raw_hash": "bbb"})

    def test_account_identity_fields_are_fixed(self) -> None:
        account = {"type": "account", "name": "Checking", "kind": "checking",
                   "institution": "Bank", "currency": "ILS"}
        self.assertEqual(self.apply({**account, "closed_on": "2026-12-31"}), {"account:updated": 1})
        with self.assertRaisesRegex(IntentError, "currency of an existing account"):
            self.apply({**account, "currency": "USD"})

    def test_feed_attachment_cannot_move_accounts(self) -> None:
        feed = {"type": "feed", "account": "Broker", "provider": "ibkr",
                "feed_type": "api", "external_account_ref": "U1"}
        self.assertEqual(self.apply(feed), {"feed:created": 1})
        self.assertEqual(self.apply({**feed, "label": "IBKR"}), {"feed:updated": 1})
        with self.assertRaisesRegex(IntentError, "reconcile-account"):
            self.apply({**feed, "account": "Checking"})


class MarketReferenceTests(ReferenceFixture):
    def test_price_never_overwrites_and_meta_stamps(self) -> None:
        price = {"type": "price", "ticker": "RR.LSE", "date": "2026-10-20",
                 "close": "8.4125", "currency": "GBP"}
        self.assertEqual(self.apply(price), {"price:created": 1})
        self.assertEqual(self.apply({**price, "close": "9"}), {"price:unchanged": 1})
        self.assertEqual(self.rows("SELECT close_minor FROM prices"), [(841,)])
        self.apply({"type": "meta", "key": "last_sync_at", "now": True})
        self.assertEqual(len(self.rows("SELECT value FROM meta WHERE key='last_sync_at'")), 1)
        with self.assertRaisesRegex(IntentError, "exactly one"):
            self.apply({"type": "meta", "key": "last_sync_at", "value": "x", "now": True})


class SnapshotTests(ReferenceFixture):
    def balance(self, **overrides) -> dict:
        intent = {"type": "balance", "account": "Checking", "as_of": "2026-10-31",
                  "amount": "1234.56", "currency": "ILS",
                  "evidence": {"document": "doc-1"}}
        intent.update(overrides)
        return intent

    def test_balance_corroboration_and_conflict(self) -> None:
        self.assertEqual(self.apply(self.balance()), {"balance:created": 1})
        self.assertEqual(self.apply(self.balance()), {"balance:unchanged": 1})
        self.apply({"type": "feed", "account": "Checking", "provider": "bank", "feed_type": "api"})
        api = {"feed": "bank", "key": "api-2026-10-31"}
        self.assertEqual(self.apply(self.balance(evidence=api)), {"balance:linked": 1})
        conflicting = self.balance(amount="1200", evidence={"feed": "bank", "key": "api-2"})
        with self.assertRaisesRegex(IntentError, "set prefer"):
            self.apply(conflicting)
        self.assertEqual(self.apply({**conflicting, "prefer": True}), {"balance:replaced": 1})
        self.assertEqual(self.rows("SELECT amount_minor FROM balances"), [(120000,)])
        self.assertEqual(self.rows("SELECT COUNT(*) FROM balance_evidence"), [(3,)])

    def test_complete_snapshot_closes_omitted_holdings_and_cash(self) -> None:
        first = {
            "type": "snapshot", "account": "Broker", "as_of": "2026-10-20",
            "evidence": {"feed": "broker", "key": "broker:2026-10-20"},
            "cash": [{"currency": "USD", "amount": "500"}, {"currency": "GBP", "amount": "10"}],
            "positions": [
                {"ticker": "AAA", "quantity": 5, "currency": "USD", "price": "10.123", "value": "50.62"},
                {"ticker": "BBB", "quantity": 2, "currency": "USD"},
            ],
            "complete": True,
        }
        self.assertEqual(self.apply(first), {"snapshot:created": 1})
        self.assertEqual(self.apply(first), {"snapshot:unchanged": 1})
        second = {
            "type": "snapshot", "account": "Broker", "as_of": "2026-10-21",
            "evidence": {"feed": "broker", "key": "broker:2026-10-21"},
            "cash": [{"currency": "USD", "amount": "700"}],
            "positions": [{"ticker": "AAA", "quantity": 5, "currency": "USD"}],
            "complete": True,
        }
        self.apply(second)
        self.assertEqual(
            self.rows(
                """SELECT component, amount_minor FROM balances
                    WHERE as_of='2026-10-21' ORDER BY component"""
            ),
            [("cash_gbp", 0), ("cash_usd", 70000)],
        )
        self.assertEqual(
            self.rows(
                """SELECT s.ticker, p.quantity FROM positions p
                     JOIN securities s ON s.id=p.security_id
                    WHERE p.as_of='2026-10-21' ORDER BY s.ticker"""
            ),
            [("AAA", 5.0), ("BBB", 0.0)],
        )
        self.assertEqual(
            self.rows(
                """SELECT source_key, account_snapshot_complete FROM position_evidence
                    ORDER BY id"""
            )[-1],
            ("broker:2026-10-21:pos:BBB", 1),
        )

    def test_complete_read_leaves_what_its_feed_never_saw(self) -> None:
        self.apply(
            {"type": "document", "drive_id": "wrapper-1", "drive_path": "investments/w.pdf",
             "filename": "w.pdf"},
            {"type": "balance", "account": "Broker", "as_of": "2026-10-01",
             "amount": "147.73", "currency": "ILS", "evidence": {"document": "wrapper-1"}},
        )
        self.apply({
            "type": "snapshot", "account": "Broker", "as_of": "2026-10-21",
            "evidence": {"feed": "broker", "key": "b1"},
            "cash": [{"currency": "USD", "amount": "700"}],
            "complete": True,
        })
        self.assertEqual(
            self.rows("SELECT as_of, amount_minor FROM balances WHERE currency='ILS'"),
            [("2026-10-01", 14773)],
        )

    def test_partial_snapshot_closes_nothing(self) -> None:
        self.apply({
            "type": "snapshot", "account": "Broker", "as_of": "2026-10-20",
            "evidence": {"feed": "broker", "key": "b1"},
            "positions": [{"ticker": "AAA", "quantity": 5, "currency": "USD"}],
        })
        self.apply({
            "type": "snapshot", "account": "Broker", "as_of": "2026-10-21",
            "evidence": {"feed": "broker", "key": "b2"},
            "cash": [{"currency": "USD", "amount": "1"}],
        })
        self.assertEqual(self.rows("SELECT COUNT(*) FROM positions WHERE as_of='2026-10-21'"), [(0,)])


class PayslipTests(ReferenceFixture):
    def payslip(self, **overrides) -> dict:
        intent = {
            "type": "payslip", "employer": "Employer", "period_start": "2026-09-01",
            "period_end": "2026-09-30", "gross": "20000", "net": "14000",
            "income_tax": "3000", "pension_employee": "1200", "document": "doc-1",
            "line_items": [{"label": "bonus", "amount": "500", "kind": "earning"}],
        }
        intent.update(overrides)
        return intent

    def test_create_rerun_and_pair_with_salary_credit(self) -> None:
        self.assertEqual(self.apply(self.payslip()), {"payslip:created": 1})
        self.assertEqual(self.apply(self.payslip()), {"payslip:unchanged": 1})
        self.assertEqual(self.apply(self.payslip(paid_on="2026-10-09")), {"payslip:updated": 1})
        self.assertEqual(
            self.rows("SELECT gross_minor, net_minor, income_tax_minor, paid_on FROM payslips"),
            [(2_000_000, 1_400_000, 300_000, "2026-10-09")],
        )
        self.assertEqual(self.rows("SELECT label, amount_minor, kind FROM payslip_line_items"),
                         [("bonus", 50_000, "earning")])

    def test_different_amounts_are_rejected(self) -> None:
        self.apply(self.payslip())
        with self.assertRaisesRegex(IntentError, "different amounts"):
            self.apply(self.payslip(net="14001"))
        with self.assertRaisesRegex(IntentError, "different line items"):
            self.apply(self.payslip(line_items=[]))


if __name__ == "__main__":
    unittest.main()
