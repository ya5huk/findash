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
from findash_core.ledger.engine import IntentError, LedgerWriter  # noqa: E402
from findash_core.migrations import require_current_schema  # noqa: E402


class LedgerFixture(unittest.TestCase):
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
                VALUES (5,'doc-bank','bank/a.xlsx','a.xlsx','bank_statement','2026-10-31'),
                       (6,'doc-fx','fx-conversions/b.png','b.png','fx_conversion','2026-10-12');
                INSERT INTO fx_rates(date,base_currency,quote_currency,rate,source)
                VALUES ('2026-10-12','USD','ILS',3.0724,'yahoo'),
                       ('2026-10-15','USD','ILS',3.1031,'yahoo');
                """
            )

    def apply(self, *intents: dict) -> dict:
        with self.con:
            outcome = LedgerWriter(self.con).apply(list(intents))
            require_current_schema(self.con)
        return outcome

    def one(self, sql: str, params: tuple = ()):
        return self.con.execute(sql, params).fetchone()

    def count(self, table: str) -> int:
        return int(self.one(f"SELECT COUNT(*) FROM {table}")[0])

    @staticmethod
    def bank_outflow(key: str = "tx-1") -> dict:
        return {
            "type": "transfer",
            "date": "2026-10-12",
            "amount": "10000",
            "currency": "ILS",
            "from": {"account": "Checking", "evidence": {"feed": "bank", "key": key}},
            "to": {"account": "Broker"},
            "description": "Transfer to own brokerage",
        }


class CashIntentTests(LedgerFixture):
    def intent(self, **overrides) -> dict:
        intent = {
            "type": "cash",
            "account": 1,
            "date": "2026-10-03",
            "amount": "-42.50",
            "currency": "ILS",
            "flow_type": "expense",
            "category": "groceries",
            "evidence": {"document": "doc-bank", "row": 7},
        }
        intent.update(overrides)
        return intent

    def test_records_all_rows_and_rerun_is_a_noop(self) -> None:
        self.assertEqual(self.apply(self.intent()), {"cash:created": 1})
        self.assertEqual(self.apply(self.intent()), {"cash:unchanged": 1})
        raw = self.one("SELECT * FROM transactions")
        event = self.one("SELECT * FROM canonical_events")
        self.assertEqual((raw["amount_minor"], event["amount_minor"]), (-4250, -4250))
        self.assertEqual(raw["canonical_event_id"], event["id"])
        self.assertEqual(raw["source_doc_id"], 5)
        self.assertEqual(self.count("event_evidence"), 1)
        self.assertEqual(
            [row["kind"] for row in self.con.execute("SELECT kind FROM ledger_log")],
            ["intents", "intents"],
        )

    def test_contradicting_facts_and_extra_precision_are_rejected(self) -> None:
        self.apply(self.intent())
        with self.assertRaisesRegex(IntentError, "different facts"):
            self.apply(self.intent(amount="-42.51"))
        with self.assertRaisesRegex(IntentError, "more decimals"):
            self.apply(self.intent(amount="-1.005", evidence={"document": "doc-bank", "row": 8}))
        with self.assertRaisesRegex(IntentError, "unknown colour"):
            self.apply(self.intent(colour="red"))

    def test_second_feed_supports_existing_event(self) -> None:
        self.apply(self.intent())
        event_id = self.one("SELECT id FROM canonical_events")[0]
        outcome = self.apply(
            self.intent(evidence={"feed": "bank", "key": "api-7"}, same_as_event=event_id)
        )
        self.assertEqual(outcome, {"cash:linked": 1})
        self.assertEqual(self.count("canonical_events"), 1)
        self.assertEqual(self.count("transactions"), 2)

    def test_brokerage_cash_gets_currency_component(self) -> None:
        self.apply(
            self.intent(
                account="Broker",
                currency="USD",
                amount="-1.25",
                flow_type="expense",
                category="fee",
                evidence={"feed": "broker", "key": "fee-1"},
            )
        )
        self.assertEqual(self.one("SELECT component FROM canonical_events")[0], "cash_usd")


class TransferIntentTests(LedgerFixture):
    def test_unseen_destination_becomes_expected_leg(self) -> None:
        self.assertEqual(self.apply(self.bank_outflow()), {"transfer:created": 1})
        self.assertEqual(self.apply(self.bank_outflow()), {"transfer:unchanged": 1})
        link = self.one("SELECT * FROM transfer_links")
        inflow = self.one("SELECT * FROM canonical_events WHERE id=?", (link["inflow_event_id"],))
        self.assertEqual(
            (inflow["account_id"], inflow["amount_minor"], inflow["status"], inflow["component"]),
            (2, 1_000_000, "needs_review", "cash_ils"),
        )
        self.assertEqual(link["match_method"], "intent:expected")
        flows = AccountingEngine(self.con).external_flows("2026-10-31")
        self.assertEqual(flows, [], "an expected leg is not yet external funding")

    def test_destination_evidence_fills_expected_leg(self) -> None:
        self.apply(self.bank_outflow())
        filled = dict(self.bank_outflow())
        filled["to"] = {
            "account": "Broker",
            "date": "2026-10-13",
            "evidence": {"feed": "broker", "key": "deposit-1"},
        }
        self.assertEqual(self.apply(filled), {"transfer:confirmed": 1})
        self.assertEqual(self.apply(filled), {"transfer:unchanged": 1})
        inflow = self.one(
            "SELECT * FROM canonical_events WHERE account_id=2 AND flow_type='transfer'"
        )
        self.assertEqual((inflow["status"], inflow["occurred_on"]), ("confirmed", "2026-10-13"))
        self.assertEqual(self.count("transfer_links"), 1)
        flows = AccountingEngine(self.con).external_flows("2026-10-31")
        self.assertEqual([(f.currency, f.native_amount_minor) for f in flows], [("ILS", 1_000_000)])

    def test_asserted_counterpart_is_confirmed(self) -> None:
        intent = self.bank_outflow()
        intent["to"]["confirmed_by"] = "conversion records draw on this cash"
        self.apply(intent)
        inflow = self.one("SELECT status, notes FROM canonical_events WHERE account_id=2")
        self.assertEqual(inflow["status"], "confirmed")
        self.assertIn("conversion records", inflow["notes"])

    def test_adopts_existing_raw_bank_row(self) -> None:
        with self.con:
            raw_id = self.con.execute(
                """INSERT INTO transactions(account_id,date,amount_minor,currency,category,source_doc_id)
                   VALUES (1,'2026-10-12',-1000000,'ILS','transfer',5) RETURNING id"""
            ).fetchone()[0]
        intent = self.bank_outflow()
        intent["from"]["evidence"] = {"transaction": raw_id}
        self.apply(intent)
        raw = self.one("SELECT * FROM transactions WHERE id=?", (raw_id,))
        self.assertEqual(raw["flow_type"], "transfer")
        self.assertIsNotNone(raw["canonical_event_id"])
        self.assertEqual(self.apply(intent), {"transfer:unchanged": 1})

    def test_rejects_same_account_and_missing_evidence(self) -> None:
        same = self.bank_outflow()
        same["to"] = {"account": "Checking"}
        with self.assertRaisesRegex(IntentError, "two different accounts"):
            self.apply(same)
        blind = self.bank_outflow()
        blind["from"] = {"account": "Checking"}
        with self.assertRaisesRegex(IntentError, "needs evidence"):
            self.apply(blind)

    def test_exclude_removes_link_and_unobserved_counterpart(self) -> None:
        self.apply(self.bank_outflow())
        outflow = self.one("SELECT outflow_event_id FROM transfer_links")[0]
        self.assertEqual(
            self.apply({"type": "exclude", "event": outflow, "reason": "not a transfer"}),
            {"exclude:excluded": 1},
        )
        self.assertEqual(self.count("transfer_links"), 0)
        self.assertEqual(
            {row[0] for row in self.con.execute("SELECT status FROM canonical_events")},
            {"excluded"},
        )


class FxConversionIntentTests(LedgerFixture):
    def intent(self, **overrides) -> dict:
        intent = {
            "type": "fx_conversion",
            "account": "Broker",
            "date": "2026-10-12",
            "settled_on": "2026-10-16",
            "sell": {"amount": "10000", "currency": "ILS"},
            "buy": {"currency": "USD"},
            "rate": "3.1246",
            "evidence": {"document": "doc-fx", "row": 1},
        }
        intent.update(overrides)
        return intent

    def test_derives_missing_amount_and_both_rates(self) -> None:
        self.assertEqual(self.apply(self.intent()), {"fx_conversion:created": 1})
        self.assertEqual(self.apply(self.intent()), {"fx_conversion:unchanged": 1})
        conversion = self.one("SELECT * FROM fx_conversions")
        self.assertEqual(
            (
                conversion["base_currency"],
                conversion["quote_currency"],
                conversion["actual_rate"],
                conversion["market_rate"],
                conversion["market_rate_date"],
            ),
            ("USD", "ILS", 3.1246, 3.0724, "2026-10-12"),
        )
        legs = self.con.execute(
            """SELECT occurred_on, amount_minor, currency, component FROM canonical_events
               ORDER BY amount_minor"""
        ).fetchall()
        self.assertEqual(
            [tuple(row) for row in legs],
            [
                ("2026-10-12", -1_000_000, "ILS", "cash_ils"),
                ("2026-10-16", 320_041, "USD", "cash_usd"),
            ],
        )
        self.assertEqual(AccountingEngine(self.con).external_flows("2026-10-31"), [])

    def test_all_three_values_must_agree(self) -> None:
        self.apply(self.intent(buy={"currency": "USD", "amount": "3200.41"}))
        with self.assertRaisesRegex(IntentError, "contradict"):
            self.apply(
                self.intent(
                    buy={"currency": "USD", "amount": "3150.00"},
                    evidence={"document": "doc-fx", "row": 2},
                )
            )

    def test_amounts_alone_imply_the_rate(self) -> None:
        self.apply(
            self.intent(rate=None, buy={"currency": "USD", "amount": "3200.41"})
        )
        rate = self.one("SELECT actual_rate FROM fx_conversions")[0]
        self.assertAlmostEqual(rate, 3.12460, places=5)

    def test_adopts_legacy_deposit_row_as_buy_leg(self) -> None:
        with self.con:
            raw_id = self.con.execute(
                """INSERT INTO transactions(account_id,date,amount_minor,currency,category,
                                            description,source_doc_id)
                   VALUES (2,'2026-10-12',320041,'USD','deposit','legacy sheet row',6)
                   RETURNING id"""
            ).fetchone()[0]
        intent = self.intent(
            settled_on=None,
            buy={"currency": "USD", "amount": "3200.41", "evidence": {"transaction": raw_id}},
        )
        self.apply(intent)
        raw = self.one("SELECT category, flow_type, component FROM transactions WHERE id=?", (raw_id,))
        self.assertEqual(tuple(raw), ("fx", "investment", "cash_usd"))
        self.assertEqual(self.count("canonical_events"), 2)

    def test_asserted_conversion_rerun_is_a_noop(self) -> None:
        intent = self.intent(evidence=None, confirmed_by="user-reported rate")
        self.assertEqual(self.apply(intent), {"fx_conversion:created": 1})
        self.assertEqual(self.apply(intent), {"fx_conversion:unchanged": 1})
        self.assertEqual(self.count("fx_conversions"), 1)
        self.assertEqual(self.count("canonical_events"), 2)


class TradeAndDividendIntentTests(LedgerFixture):
    def trade(self, **overrides) -> dict:
        intent = {
            "type": "trade",
            "account": "Broker",
            "date": "2026-10-21",
            "ticker": "SPYM",
            "side": "buy",
            "shares": 41,
            "price": "90.52",
            "fees": "-0.35",
            "currency": "USD",
            "evidence": {"feed": "broker", "key": "fill-1"},
        }
        intent.update(overrides)
        return intent

    def test_trade_creates_security_and_canonical_trade(self) -> None:
        self.assertEqual(self.apply(self.trade()), {"trade:created": 1})
        self.assertEqual(self.apply(self.trade()), {"trade:unchanged": 1})
        event = self.one("SELECT * FROM canonical_events WHERE event_type='trade'")
        self.assertEqual(
            (event["quantity"], event["price_minor"], event["fees_minor"]), (41.0, 9052, 35)
        )
        raw = self.one("SELECT * FROM trades")
        self.assertEqual(raw["canonical_event_id"], event["id"])
        with self.assertRaisesRegex(IntentError, "different facts"):
            self.apply(self.trade(shares=40))

    def test_dividend_from_gross_and_withholding(self) -> None:
        intent = {
            "type": "dividend",
            "account": "Broker",
            "ticker": "JEPI",
            "date": "2026-10-07",
            "ex_date": "2026-10-01",
            "currency": "USD",
            "gross": "59.30",
            "withholding": "14.83",
            "evidence": {"feed": "broker", "key": "div-1"},
        }
        self.assertEqual(self.apply(intent), {"dividend:created": 1})
        self.assertEqual(self.apply(intent), {"dividend:unchanged": 1})
        event = self.one("SELECT * FROM canonical_events")
        self.assertEqual((event["amount_minor"], event["flow_type"]), (4447, "income"))
        detail = self.one("SELECT * FROM dividend_events")
        self.assertEqual((detail["gross_minor"], detail["withholding_minor"]), (5930, 1483))

    def test_confirm_moves_expected_leg_and_records_evidence(self) -> None:
        self.apply(self.bank_outflow())
        inflow = self.one("SELECT inflow_event_id FROM transfer_links")[0]
        outcome = self.apply(
            {
                "type": "confirm",
                "event": inflow,
                "date": "2026-10-14",
                "evidence": {"feed": "broker", "key": "deposit-9"},
            }
        )
        self.assertEqual(outcome, {"confirm:confirmed": 1})
        event = self.one("SELECT status, occurred_on FROM canonical_events WHERE id=?", (inflow,))
        self.assertEqual(tuple(event), ("confirmed", "2026-10-14"))
        with self.assertRaisesRegex(IntentError, "fixed"):
            self.apply({"type": "confirm", "event": inflow, "amount": "1"})


if __name__ == "__main__":
    unittest.main()
