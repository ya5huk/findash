from __future__ import annotations

from decimal import Decimal
from pathlib import Path
import sqlite3
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from findash_core.accounting import AccountingEngine  # noqa: E402


class TradeCashAccountingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        self.con.executescript(
            (ROOT / "scripts" / "init-db.sql").read_text(encoding="utf-8")
        )
        self.addCleanup(self.con.close)
        self.account_id = int(
            self.con.execute(
                """INSERT INTO accounts(name,kind,institution,currency)
                   VALUES(?,?,?,?)""",
                ("Broker", "brokerage", "Test institution", "USD"),
            ).lastrowid
        )
        self.security_id = int(
            self.con.execute(
                """INSERT INTO securities(ticker,name,asset_class,currency)
                   VALUES(?,?,?,?)""",
                ("TEST", "Test security", "stock", "USD"),
            ).lastrowid
        )
        self.con.execute(
            """INSERT INTO balances(
                   account_id,as_of,amount_minor,currency,component
               ) VALUES(?,?,?,?,?)""",
            (self.account_id, "2026-01-01", 100_000, "USD", "cash_usd"),
        )
        self.con.execute(
            """INSERT INTO prices(security_id,date,close_minor,currency)
               VALUES(?,?,?,?)""",
            (self.security_id, "2026-01-02", 10_000, "USD"),
        )

    def canonical_trade(
        self, *, quantity: int, price_minor: int, fees_minor: int = 0
    ) -> int:
        return int(
            self.con.execute(
                """INSERT INTO canonical_events(
                       account_id,event_type,occurred_on,currency,security_id,
                       side,quantity,price_minor,fees_minor
                   ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    self.account_id,
                    "trade",
                    "2026-01-02",
                    "USD",
                    self.security_id,
                    "buy",
                    quantity,
                    price_minor,
                    fees_minor,
                ),
            ).lastrowid
        )

    def canonical_cash(self, amount_minor: int, *, occurred_on: str = "2026-01-02") -> int:
        return int(
            self.con.execute(
                """INSERT INTO canonical_events(
                       account_id,event_type,occurred_on,currency,amount_minor,
                       flow_type,category,component
                   ) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    self.account_id,
                    "cash",
                    occurred_on,
                    "USD",
                    amount_minor,
                    "investment",
                    "settlement",
                    "cash_usd",
                ),
            ).lastrowid
        )

    def cash_value(self, as_of: str = "2026-01-02") -> Decimal:
        state = AccountingEngine(self.con, "USD").state_at(as_of)
        return next(row.native_value_minor for row in state.rows if row.item_type == "cash")

    def test_one_posted_cash_event_can_replace_several_trade_fills(self):
        first = self.canonical_trade(quantity=1, price_minor=10_000, fees_minor=100)
        second = self.canonical_trade(quantity=2, price_minor=5_000)
        self.canonical_trade(quantity=1, price_minor=7_000)
        cash = self.canonical_cash(-20_100)
        self.con.executemany(
            """INSERT INTO trade_cash_links(
                   cash_event_id,trade_event_id,match_method
               ) VALUES(?,?,?)""",
            [
                (cash, first, "reviewed source identities and fill economics"),
                (cash, second, "reviewed source identities and fill economics"),
            ],
        )

        # The linked aggregate cash posts once; only the remaining trade derives cash.
        self.assertEqual(self.cash_value(), Decimal("72900"))

    def test_future_posted_cash_link_does_not_rewrite_trade_date_history(self):
        trade = self.canonical_trade(quantity=1, price_minor=10_000)
        cash = self.canonical_cash(-10_000, occurred_on="2026-01-04")
        self.con.execute(
            """INSERT INTO trade_cash_links(
                   cash_event_id,trade_event_id,match_method
               ) VALUES(?,?,?)""",
            (cash, trade, "reviewed settlement"),
        )

        # Before the posted settlement exists in the requested history, the trade
        # derives its own cash leg. On posting day the explicit cash replaces it.
        self.assertEqual(self.cash_value("2026-01-02"), Decimal("90000"))
        self.assertEqual(self.cash_value("2026-01-03"), Decimal("90000"))
        self.assertEqual(self.cash_value("2026-01-04"), Decimal("90000"))

    def test_cash_anchor_after_trade_already_contains_later_linked_settlement(self):
        trade = self.canonical_trade(quantity=1, price_minor=10_000)
        self.con.execute(
            """INSERT INTO balances(
                   account_id,as_of,amount_minor,currency,component
               ) VALUES(?,?,?,?,?)""",
            (self.account_id, "2026-01-03", 90_000, "USD", "cash_usd"),
        )
        cash = self.canonical_cash(-10_000, occurred_on="2026-01-04")
        self.con.execute(
            """INSERT INTO trade_cash_links(
                   cash_event_id,trade_event_id,match_method
               ) VALUES(?,?,?)""",
            (cash, trade, "reviewed settlement"),
        )

        self.assertEqual(self.cash_value("2026-01-03"), Decimal("90000"))
        self.assertEqual(self.cash_value("2026-01-04"), Decimal("90000"))

    def test_unlinked_cash_and_trade_are_separate_movements(self):
        self.canonical_trade(quantity=1, price_minor=10_000)
        self.canonical_cash(-5_000)

        self.assertEqual(self.cash_value(), Decimal("85000"))

    def test_legacy_raw_trade_pair_keeps_conservative_old_treatment(self):
        self.con.execute(
            """INSERT INTO trades(
                   account_id,security_id,date,side,shares,price_minor,
                   fees_minor,currency
               ) VALUES(?,?,?,?,?,?,?,?)""",
            (
                self.account_id,
                self.security_id,
                "2026-01-02",
                "buy",
                1,
                10_000,
                0,
                "USD",
            ),
        )
        self.con.execute(
            """INSERT INTO transactions(
                   account_id,date,amount_minor,currency,category,flow_type,
                   component
               ) VALUES(?,?,?,?,?,?,?)""",
            (
                self.account_id,
                "2026-01-02",
                -10_000,
                "USD",
                "settlement",
                "investment",
                "cash_usd",
            ),
        )

        self.assertEqual(self.cash_value(), Decimal("90000"))

    def test_isolated_legacy_investment_cash_is_not_dropped(self):
        self.con.execute(
            """INSERT INTO transactions(
                   account_id,date,amount_minor,currency,category,flow_type,
                   component
               ) VALUES(?,?,?,?,?,?,?)""",
            (
                self.account_id,
                "2026-01-02",
                -10_000,
                "USD",
                "brokerage adjustment",
                "investment",
                "cash_usd",
            ),
        )

        self.assertEqual(self.cash_value(), Decimal("90000"))


if __name__ == "__main__":
    unittest.main()
