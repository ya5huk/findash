from __future__ import annotations

from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from findash_core.accounting import AccountingEngine  # noqa: E402
from findash_core.db import connect_db  # noqa: E402
from findash_core.ledger.engine import LedgerWriter  # noqa: E402
from findash_core.ledger.reconcile import Reconciler  # noqa: E402
from findash_core.migrations import require_current_schema  # noqa: E402


class ReconcileFixture(unittest.TestCase):
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
        with self.con:
            self.con.executescript(
                """
                INSERT INTO accounts(id,name,kind,institution,currency)
                VALUES (1,'Checking','checking','Bank','ILS'),
                       (2,'Broker','brokerage','Broker','USD');
                INSERT INTO account_feeds(id,account_id,provider,feed_type)
                VALUES (10,1,'bank','api'),(20,2,'broker','api');
                INSERT INTO documents(id,drive_id,drive_path,filename,doc_type,doc_date)
                VALUES (6,'doc-fx','fx-conversions/b.png','b.png','fx_conversion','2026-10-12');
                INSERT INTO fx_rates(date,base_currency,quote_currency,rate,source)
                VALUES ('2026-10-12','USD','ILS',3.0724,'yahoo'),
                       ('2026-10-16','USD','ILS',3.1112,'yahoo');
                """
            )

    def apply(self, *intents: dict) -> dict:
        with self.con:
            outcome = LedgerWriter(self.con).apply(list(intents))
            require_current_schema(self.con)
        return outcome

    def reconcile(self, today: str = "2026-10-26") -> dict:
        with self.con:
            result = Reconciler(self.con, today=today).run()
            require_current_schema(self.con)
        return result

    def cash_snapshots(self, *rows: tuple[str, int]) -> None:
        with self.con:
            for as_of, amount in rows:
                self.con.execute(
                    """INSERT INTO balances(account_id,as_of,component,amount_minor,currency)
                       VALUES (2,?,'cash_usd',?,'USD')""",
                    (as_of, amount),
                )

    def funding(self, key: str, day: str = "2026-10-12") -> dict:
        return {
            "type": "transfer",
            "date": day,
            "amount": "10000",
            "currency": "ILS",
            "from": {"account": "Checking", "evidence": {"feed": "bank", "key": key}},
            "to": {"account": "Broker"},
        }

    @staticmethod
    def kinds(result: dict) -> list[str]:
        return sorted(item["kind"] for item in result["open_items"])


class FundingReconcileTests(ReconcileFixture):
    def test_october_funding_is_confirmed_with_inferred_conversion(self) -> None:
        self.apply(self.funding("tx-1"))
        self.cash_snapshots(("2026-10-15", 72_717), ("2026-10-16", 392_758))

        result = self.reconcile()

        self.assertEqual(result["actions"], {"confirmed_transfer_fx": 1})
        self.assertEqual(result["open_items"], [])
        conversion = self.con.execute("SELECT * FROM fx_conversions").fetchone()
        self.assertAlmostEqual(conversion["actual_rate"], 3.1246, places=4)
        self.assertEqual(
            (conversion["market_rate"], conversion["match_method"]),
            (3.0724, "reconcile:cash-snapshots"),
        )
        buy = self.con.execute(
            "SELECT occurred_on, amount_minor FROM canonical_events WHERE id=?",
            (conversion["buy_event_id"],),
        ).fetchone()
        self.assertEqual(tuple(buy), ("2026-10-16", 320_041))
        flows = AccountingEngine(self.con).external_flows("2026-10-31")
        self.assertEqual([(f.currency, f.native_amount_minor) for f in flows], [("ILS", 1_000_000)])

        self.assertEqual(self.reconcile()["actions"], {})

    def test_document_upgrades_inferred_conversion(self) -> None:
        self.apply(self.funding("tx-1"))
        self.cash_snapshots(("2026-10-15", 72_717), ("2026-10-16", 392_758))
        self.reconcile()

        outcome = self.apply(
            {
                "type": "fx_conversion",
                "account": "Broker",
                "date": "2026-10-12",
                "sell": {"amount": "10000", "currency": "ILS"},
                "buy": {"currency": "USD"},
                "rate": "3.1246",
                "evidence": {"document": "doc-fx", "row": 1},
            }
        )

        self.assertEqual(outcome, {"fx_conversion:confirmed": 1})
        conversion = self.con.execute("SELECT * FROM fx_conversions").fetchall()
        self.assertEqual(len(conversion), 1)
        self.assertEqual(
            (conversion[0]["actual_rate"], conversion[0]["match_method"]), (3.1246, "intent")
        )
        self.assertEqual(
            self.con.execute("SELECT COUNT(*) FROM event_evidence").fetchone()[0], 3
        )

    def test_ambiguous_candidates_stay_open(self) -> None:
        self.apply(self.funding("tx-1"), self.funding("tx-2", "2026-10-13"))
        self.cash_snapshots(("2026-10-15", 72_717), ("2026-10-16", 392_758))

        result = self.reconcile()

        self.assertEqual(result["actions"], {})
        self.assertEqual(
            self.kinds(result), ["expected_leg", "expected_leg", "unexplained_cash"]
        )
        self.assertTrue(all(item["escalate"] for item in result["open_items"]))

    def test_rate_far_from_market_is_not_matched(self) -> None:
        self.apply(self.funding("tx-1"))
        self.cash_snapshots(("2026-10-15", 72_717), ("2026-10-16", 372_758))

        result = self.reconcile(today="2026-10-17")

        self.assertEqual(result["actions"], {})
        self.assertEqual(self.kinds(result), ["expected_leg", "unexplained_cash"])
        self.assertFalse(any(item["escalate"] for item in result["open_items"]))


class DividendReconcileTests(ReconcileFixture):
    def setUp(self) -> None:
        super().setUp()
        with self.con:
            self.con.executescript(
                """
                INSERT INTO securities(id,ticker,currency) VALUES (3,'JEPI','USD');
                INSERT INTO positions(account_id,security_id,as_of,quantity,currency)
                VALUES (2,3,'2026-09-20',100,'USD');
                INSERT INTO security_distributions(
                    security_id,event_date,amount_per_share_minor_numerator,
                    amount_per_share_minor_denominator,currency,source,source_key,observed_at
                ) VALUES (3,'2026-10-01',341,10,'USD','yahoo','k1','2026-10-02 00:00:00');
                """
            )

    def test_expected_dividend_is_confirmed_by_cash(self) -> None:
        self.cash_snapshots(("2026-10-05", 100_000), ("2026-10-08", 102_558))

        result = self.reconcile(today="2026-10-10")

        self.assertEqual(
            result["actions"], {"confirmed_dividend": 1, "dividend_expected": 1}
        )
        event = self.con.execute(
            "SELECT status, amount_minor, occurred_on FROM canonical_events"
        ).fetchone()
        self.assertEqual(tuple(event), ("confirmed", 2_558, "2026-10-06"))
        self.assertEqual(self.reconcile(today="2026-10-10")["actions"], {})

    def test_unpaid_dividend_becomes_overdue(self) -> None:
        self.reconcile(today="2026-10-10")
        result = self.reconcile(today="2026-11-20")
        self.assertEqual(self.kinds(result), ["dividend_overdue"])

    def test_reported_dividend_settles_the_expectation(self) -> None:
        self.reconcile(today="2026-10-10")
        outcome = self.apply(
            {
                "type": "dividend",
                "account": "Broker",
                "ticker": "JEPI",
                "date": "2026-10-07",
                "currency": "USD",
                "gross": "34.10",
                "withholding": "8.53",
                "evidence": {"feed": "broker", "key": "div-1"},
            }
        )
        self.assertEqual(outcome, {"dividend:confirmed": 1})
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM canonical_events").fetchone()[0], 1)
        self.assertEqual(
            self.con.execute("SELECT amount_minor FROM canonical_events").fetchone()[0], 2_557
        )


class OpenItemTests(ReconcileFixture):
    def test_stale_review_and_raw_sql_are_reported(self) -> None:
        self.apply(
            {
                "type": "cash",
                "account": "Checking",
                "date": "2026-09-01",
                "amount": "-5",
                "currency": "ILS",
                "flow_type": "expense",
                "status": "needs_review",
                "evidence": {"feed": "bank", "key": "pending-1"},
            }
        )
        with self.con:
            self.con.execute(
                """INSERT INTO ledger_log(kind,summary,applied_at)
                   VALUES ('raw_sql','manual repair','2026-10-20 10:00:00')"""
            )
        result = self.reconcile()
        self.assertEqual(self.kinds(result), ["raw_sql_used", "stale_review"])
        self.assertEqual(result["escalate_count"], 2)


if __name__ == "__main__":
    unittest.main()
