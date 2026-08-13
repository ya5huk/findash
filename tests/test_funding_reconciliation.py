from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from findash_core.accounting import AccountingEngine  # noqa: E402
from findash_core.funding_reconciliation import (  # noqa: E402
    FundingReconciliationError,
    infer_brokerage_funding,
)


class FundingReconciliationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "finance.db"
        connection = sqlite3.connect(self.db)
        connection.executescript(
            (ROOT / "scripts" / "init-db.sql").read_text(encoding="utf-8")
        )
        connection.close()

    @staticmethod
    def request(
        outflows: list[int], brokerage: int, before: int, after: int
    ) -> dict:
        return {
            "operation": "infer_brokerage_funding",
            "outflow_event_ids": outflows,
            "brokerage_account_id": brokerage,
            "before_balance_id": before,
            "after_balance_id": after,
            "confirmed_bank_destination": True,
            "confirmed_no_competing_cash_explanation": True,
            "confirmed_complete_snapshot_pair": True,
        }

    def evidence_graph(
        self, *, after_amount: int = 11_000, complete: int = 1
    ) -> tuple[int, int, int, int]:
        connection = sqlite3.connect(self.db)
        source = int(
            connection.execute(
                """INSERT INTO accounts(name,kind,institution,currency)
                   VALUES ('Checking','checking','Bank','ILS') RETURNING id"""
            ).fetchone()[0]
        )
        brokerage = int(
            connection.execute(
                """INSERT INTO accounts(name,kind,institution,currency)
                   VALUES ('Brokerage','brokerage','Broker','ILS') RETURNING id"""
            ).fetchone()[0]
        )
        bank_feed = int(
            connection.execute(
                """INSERT INTO account_feeds(
                       account_id,provider,feed_type,external_account_ref
                   ) VALUES (?,'bank','api','bank-feed') RETURNING id""",
                (source,),
            ).fetchone()[0]
        )
        brokerage_feed = int(
            connection.execute(
                """INSERT INTO account_feeds(
                       account_id,provider,feed_type,external_account_ref
                   ) VALUES (?,'broker-api','api','broker-feed') RETURNING id""",
                (brokerage,),
            ).fetchone()[0]
        )
        source_event = int(
            connection.execute(
                """INSERT INTO source_events(
                       account_feed_id,source_key,event_type,occurred_on
                   ) VALUES (?,'bank-transfer','cash','2026-01-05') RETURNING id""",
                (bank_feed,),
            ).fetchone()[0]
        )
        outflow = int(
            connection.execute(
                """INSERT INTO canonical_events(
                       account_id,event_type,occurred_on,currency,amount_minor,
                       flow_type,category,status
                   ) VALUES (?,'cash','2026-01-05','ILS',-10000,
                             'transfer','transfer','confirmed') RETURNING id""",
                (source,),
            ).fetchone()[0]
        )
        connection.execute(
            """INSERT INTO event_evidence(
                   canonical_event_id,source_event_id,evidence_role,match_method
               ) VALUES (?,?,'primary','provider-id')""",
            (outflow, source_event),
        )
        before = int(
            connection.execute(
                """INSERT INTO balances(
                       account_id,as_of,component,amount_minor,currency
                   ) VALUES (?,'2026-01-01','cash_ils',1000,'ILS') RETURNING id""",
                (brokerage,),
            ).fetchone()[0]
        )
        after = int(
            connection.execute(
                """INSERT INTO balances(
                       account_id,as_of,component,amount_minor,currency
                   ) VALUES (?,'2026-01-10','cash_ils',?,'ILS') RETURNING id""",
                (brokerage, after_amount),
            ).fetchone()[0]
        )
        for balance, source_key, amount in (
            (before, "before", 1000),
            (after, "after", after_amount),
        ):
            connection.execute(
                """INSERT INTO balance_evidence(
                       canonical_balance_id,account_feed_id,source_key,
                       observed_amount_minor,observed_currency,
                       account_snapshot_complete
                   ) VALUES (?,?,?,?,?,?)""",
                (
                    balance,
                    brokerage_feed,
                    source_key,
                    amount,
                    "ILS",
                    complete,
                ),
            )
        connection.commit()
        connection.close()
        return outflow, brokerage, before, after

    def test_infers_idempotent_brokerage_inflow_from_exact_residual(self) -> None:
        outflow, brokerage, before, after = self.evidence_graph()
        payload = self.request([outflow], brokerage, before, after)

        self.assertEqual(
            infer_brokerage_funding(self.db, payload),
            {"inflows_created": 1, "links_created": 1},
        )
        self.assertEqual(
            infer_brokerage_funding(self.db, payload),
            {"inflows_created": 0, "links_created": 0},
        )

        connection = sqlite3.connect(self.db)
        self.addCleanup(connection.close)
        self.assertEqual(
            connection.execute("SELECT COUNT(*) FROM inferred_transfer_links").fetchone()[0],
            1,
        )
        inflow = connection.execute(
            """SELECT amount_minor,flow_type,category,component
                 FROM canonical_events
                WHERE account_id=? AND event_type='cash'""",
            (brokerage,),
        ).fetchone()
        self.assertEqual(inflow, (10_000, "transfer", "deposit", "cash_ils"))
        flows = AccountingEngine(connection).external_flows("2026-01-10")
        self.assertEqual(len(flows), 1)
        self.assertEqual(flows[0].native_amount_minor, 10_000)

    def test_rejects_residual_not_explained_by_bank_transfer(self) -> None:
        outflow, brokerage, before, after = self.evidence_graph(
            after_amount=10_500
        )
        with self.assertRaisesRegex(
            FundingReconciliationError, "do not explain"
        ):
            infer_brokerage_funding(
                self.db, self.request([outflow], brokerage, before, after)
            )
        connection = sqlite3.connect(self.db)
        self.addCleanup(connection.close)
        self.assertEqual(
            connection.execute("SELECT COUNT(*) FROM inferred_transfer_links").fetchone()[0],
            0,
        )

    def test_rejects_partial_snapshot_evidence(self) -> None:
        outflow, brokerage, before, after = self.evidence_graph(complete=0)
        with self.assertRaisesRegex(
            FundingReconciliationError, "lacks complete evidence"
        ):
            infer_brokerage_funding(
                self.db, self.request([outflow], brokerage, before, after)
            )


if __name__ == "__main__":
    unittest.main()
