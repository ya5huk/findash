from __future__ import annotations

from pathlib import Path
import sqlite3
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = (ROOT / "scripts" / "init-db.sql").read_text(encoding="utf-8")


class TradeCashSchemaTest(unittest.TestCase):
    def setUp(self) -> None:
        self.con = sqlite3.connect(":memory:")
        self.con.execute("PRAGMA foreign_keys=ON")
        self.con.executescript(SCHEMA)
        self.addCleanup(self.con.close)
        self._account_number = 0
        self.first_account = self.account("USD")
        self.second_account = self.account("USD")
        self.usd_security = self.security("USD")
        self.eur_security = self.security("EUR")

    def account(self, currency: str) -> int:
        self._account_number += 1
        return int(
            self.con.execute(
                """INSERT INTO accounts(name,kind,institution,currency)
                   VALUES(?,?,?,?)""",
                (
                    f"Broker {self._account_number}",
                    "brokerage",
                    "Test institution",
                    currency,
                ),
            ).lastrowid
        )

    def security(self, currency: str) -> int:
        return int(
            self.con.execute(
                """INSERT INTO securities(ticker,name,asset_class,currency)
                   VALUES(?,?,?,?)""",
                (
                    f"TEST{currency}",
                    f"Test security {currency}",
                    "stock",
                    currency,
                ),
            ).lastrowid
        )

    def cash(
        self,
        *,
        account_id: int | None = None,
        currency: str = "USD",
        flow_type: str = "investment",
        status: str = "confirmed",
        occurred_on: str = "2026-01-02",
    ) -> int:
        return int(
            self.con.execute(
                """INSERT INTO canonical_events(
                       account_id,event_type,occurred_on,currency,amount_minor,
                       flow_type,status
                   ) VALUES(?,?,?,?,?,?,?)""",
                (
                    account_id or self.first_account,
                    "cash",
                    occurred_on,
                    currency,
                    -10_000,
                    flow_type,
                    status,
                ),
            ).lastrowid
        )

    def trade(
        self,
        *,
        account_id: int | None = None,
        currency: str = "USD",
        security_id: int | None = None,
        status: str = "confirmed",
        occurred_on: str = "2026-01-02",
    ) -> int:
        return int(
            self.con.execute(
                """INSERT INTO canonical_events(
                       account_id,event_type,occurred_on,currency,security_id,
                       side,quantity,price_minor,fees_minor,status
                   ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    account_id or self.first_account,
                    "trade",
                    occurred_on,
                    currency,
                    security_id or self.usd_security,
                    "buy",
                    1,
                    10_000,
                    0,
                    status,
                ),
            ).lastrowid
        )

    def link(self, cash_event_id: int, trade_event_id: int) -> None:
        self.con.execute(
            """INSERT INTO trade_cash_links(
                   cash_event_id,trade_event_id,match_method
               ) VALUES(?,?,?)""",
            (cash_event_id, trade_event_id, "reviewed source identities"),
        )

    def test_link_requires_confirmed_investment_cash_on_same_account_currency(self):
        valid_cash = self.cash()
        valid_trade = self.trade()
        self.link(valid_cash, valid_trade)

        invalid_pairs = [
            (self.cash(flow_type="transfer"), self.trade()),
            (self.cash(status="needs_review"), self.trade()),
            (self.cash(), self.trade(status="needs_review")),
            (self.cash(), self.trade(account_id=self.second_account)),
            (
                self.cash(),
                self.trade(currency="EUR", security_id=self.eur_security),
            ),
        ]
        for cash_event, trade_event in invalid_pairs:
            with self.subTest(cash_event=cash_event, trade_event=trade_event):
                with self.assertRaisesRegex(sqlite3.IntegrityError, "invalid trade cash link"):
                    self.link(cash_event, trade_event)

    def test_cash_settlement_cannot_precede_its_trade(self):
        cash = self.cash(occurred_on="2026-01-01")
        trade = self.trade(occurred_on="2026-01-02")

        with self.assertRaisesRegex(sqlite3.IntegrityError, "invalid trade cash link"):
            self.link(cash, trade)

    def test_linked_economics_are_immutable_but_notes_and_category_remain_editable(self):
        cash = self.cash()
        trade = self.trade()
        self.link(cash, trade)

        self.con.execute(
            "UPDATE canonical_events SET notes=?,category=? WHERE id=?",
            ("review note", "posted settlement", cash),
        )
        self.assertEqual(
            self.con.execute(
                "SELECT notes,category FROM canonical_events WHERE id=?", (cash,)
            ).fetchone(),
            ("review note", "posted settlement"),
        )

        blocked_updates = (
            ("UPDATE canonical_events SET occurred_on='2026-01-03' WHERE id=?", cash),
            ("UPDATE canonical_events SET amount_minor=-9999 WHERE id=?", cash),
            ("UPDATE canonical_events SET status='excluded' WHERE id=?", cash),
            ("UPDATE canonical_events SET side='sell' WHERE id=?", trade),
            ("UPDATE canonical_events SET quantity=2 WHERE id=?", trade),
            ("UPDATE canonical_events SET price_minor=9999 WHERE id=?", trade),
            ("UPDATE canonical_events SET fees_minor=1 WHERE id=?", trade),
        )
        for statement, event_id in blocked_updates:
            with self.subTest(statement=statement), self.assertRaisesRegex(
                sqlite3.IntegrityError, "economics are immutable"
            ):
                self.con.execute(statement, (event_id,))

        with self.assertRaises(sqlite3.IntegrityError):
            self.con.execute("DELETE FROM canonical_events WHERE id=?", (trade,))

    def test_one_trade_cannot_be_split_across_cash_events(self):
        trade = self.trade()
        self.link(self.cash(), trade)

        with self.assertRaises(sqlite3.IntegrityError):
            self.link(self.cash(), trade)


if __name__ == "__main__":
    unittest.main()
