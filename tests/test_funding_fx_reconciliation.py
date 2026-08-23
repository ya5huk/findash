from __future__ import annotations

from decimal import Decimal
import sqlite3
import sys
import tempfile
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from findash_core.accounting import AccountingEngine  # noqa: E402
from findash_core.dashboard_model import DashboardModelBuilder  # noqa: E402
from findash_core.funding_fx_reconciliation import (  # noqa: E402
    FundingFxReconciliationError,
    reconcile_funding_fx,
)


class FundingFxReconciliationTests(unittest.TestCase):
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
    def request(bank_transaction: int, destination: int, brokerage: int) -> dict:
        return {
            "operation": "reconcile_funding_fx",
            "bank_transaction_id": bank_transaction,
            "fx_destination_transaction_id": destination,
            "brokerage_account_id": brokerage,
            "confirmed_same_owned_transfer": True,
            "confirmed_complete_conversion": True,
        }

    def evidence_graph(self, *, rate: float = 3.000007) -> tuple[int, int, int]:
        connection = sqlite3.connect(self.db)
        bank_account = int(
            connection.execute(
                """INSERT INTO accounts(name,kind,institution,currency)
                   VALUES ('Checking','checking','Bank','ILS') RETURNING id"""
            ).fetchone()[0]
        )
        brokerage = int(
            connection.execute(
                """INSERT INTO accounts(name,kind,institution,currency)
                   VALUES ('Brokerage','brokerage','Broker','USD') RETURNING id"""
            ).fetchone()[0]
        )
        bank_document = int(
            connection.execute(
                """INSERT INTO documents(
                       drive_id,drive_path,filename,doc_type,doc_date
                   ) VALUES ('bank-evidence','bank/2026/sample.xlsx','sample.xlsx',
                             'bank_statement','2026-01-05') RETURNING id"""
            ).fetchone()[0]
        )
        fx_document = int(
            connection.execute(
                """INSERT INTO documents(
                       drive_id,drive_path,filename,doc_type,doc_date
                   ) VALUES ('fx-evidence','fx-conversions/2026/sample.png','sample.png',
                             'fx_conversion','2026-01-06') RETURNING id"""
            ).fetchone()[0]
        )
        bank_transaction = int(
            connection.execute(
                """INSERT INTO transactions(
                       account_id,date,amount_minor,currency,category,
                       description,source_doc_id
                   ) VALUES (?,'2026-01-05',-600000,'ILS','transfer',
                             'Reviewed owned-account transfer',?) RETURNING id""",
                (bank_account, bank_document),
            ).fetchone()[0]
        )
        destination = int(
            connection.execute(
                """INSERT INTO transactions(
                       account_id,date,amount_minor,currency,category,
                       description,source_doc_id
                   ) VALUES (?,'2026-01-06',200000,'USD','deposit',
                             'Documented FX destination',?) RETURNING id""",
                (brokerage, fx_document),
            ).fetchone()[0]
        )
        connection.execute(
            """INSERT INTO fx_rates(
                   date,base_currency,quote_currency,rate,source
               ) VALUES ('2026-01-06','USD','ILS',?,'document')""",
            (rate,),
        )
        connection.commit()
        connection.close()
        return bank_transaction, destination, brokerage

    def test_records_external_funding_and_both_internal_fx_legs(self) -> None:
        bank_transaction, destination, brokerage = self.evidence_graph()
        payload = self.request(bank_transaction, destination, brokerage)

        self.assertEqual(
            reconcile_funding_fx(self.db, payload),
            {
                "funding_inflows_created": 1,
                "fx_source_legs_created": 1,
                "links_created": 1,
            },
        )
        self.assertEqual(
            reconcile_funding_fx(self.db, payload),
            {
                "funding_inflows_created": 0,
                "fx_source_legs_created": 0,
                "links_created": 0,
            },
        )

        connection = sqlite3.connect(self.db)
        connection.row_factory = sqlite3.Row
        self.addCleanup(connection.close)
        rows = connection.execute(
            """SELECT amount_minor,currency,flow_type,category,component
                 FROM canonical_events
                WHERE account_id=? AND event_type='cash'
                ORDER BY occurred_on,id""",
            (brokerage,),
        ).fetchall()
        self.assertEqual(
            [tuple(row) for row in rows],
            [
                (600_000, "ILS", "transfer", "deposit", "cash_ils"),
                (200_000, "USD", "investment", "fx", "cash_usd"),
                (-600_000, "ILS", "investment", "fx", "cash_ils"),
            ],
        )
        self.assertEqual(
            connection.execute("SELECT COUNT(*) FROM funding_fx_links").fetchone()[0],
            1,
        )

        engine = AccountingEngine(connection)
        flows = engine.external_flows("2026-01-06")
        self.assertEqual(len(flows), 1)
        self.assertEqual(flows[0].native_amount_minor, 600_000)
        state = engine.state_at("2026-01-06")
        brokerage_rows = [row for row in state.rows if row.account_id == brokerage]
        by_currency = {row.currency: row.native_value_minor for row in brokerage_rows}
        self.assertEqual(by_currency, {"ILS": Decimal(0), "USD": Decimal(200_000)})

        builder = DashboardModelBuilder(connection)
        forex = builder._forex_activity(
            builder._deposits(flows), "2026-01-06"
        )
        self.assertEqual(forex["funding_count"], 1)
        self.assertEqual(forex["purchase_count"], 1)
        self.assertEqual(
            forex["rows"][0]["cumulative_usd_purchased_amount"], 2_000.0
        )
        self.assertEqual(
            forex["rows"][0]["cumulative_external_reporting_amount"], 6_000.0
        )

    def test_rate_mismatch_is_atomic(self) -> None:
        bank_transaction, destination, brokerage = self.evidence_graph(rate=2.5)

        with self.assertRaisesRegex(
            FundingFxReconciliationError, "does not explain"
        ):
            reconcile_funding_fx(
                self.db,
                self.request(bank_transaction, destination, brokerage),
            )

        connection = sqlite3.connect(self.db)
        self.addCleanup(connection.close)
        self.assertEqual(
            connection.execute("SELECT COUNT(*) FROM funding_fx_links").fetchone()[0],
            0,
        )
        self.assertEqual(
            connection.execute("SELECT COUNT(*) FROM canonical_events").fetchone()[0],
            0,
        )


if __name__ == "__main__":
    unittest.main()
