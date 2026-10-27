from __future__ import annotations

from decimal import Decimal
from pathlib import Path
import sqlite3
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from findash_core.accounting import AccountingEngine, AccountingError, FxBook  # noqa: E402


def database() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript((ROOT / "scripts" / "init-db.sql").read_text())
    return con


def account(con: sqlite3.Connection, name: str, kind: str, currency: str = "ILS") -> int:
    cur = con.execute(
        "INSERT INTO accounts(name,kind,institution,currency) VALUES(?,?,?,?)",
        (name, kind, "Test institution", currency),
    )
    return int(cur.lastrowid)


def security(con: sqlite3.Connection, ticker: str, currency: str = "USD") -> int:
    cur = con.execute(
        "INSERT INTO securities(ticker,name,asset_class,currency) VALUES(?,?,?,?)",
        (ticker, ticker, "stock", currency),
    )
    return int(cur.lastrowid)


class AccountingEngineTest(unittest.TestCase):
    def open_database(self) -> sqlite3.Connection:
        con = database()
        self.addCleanup(con.close)
        return con

    def test_empty_ils_database_never_requires_usd_fx(self):
        con = self.open_database()
        aid = account(con, "Checking", "checking")
        con.execute(
            "INSERT INTO balances(account_id,as_of,amount_minor,currency) VALUES(?,?,?,?)",
            (aid, "2026-01-01", 123_45, "ILS"),
        )

        state = AccountingEngine(con).state_at("2026-01-02")

        self.assertEqual(state.total_minor, Decimal("12345"))
        self.assertEqual(len(state.rows), 1)

    def test_credit_card_liability_is_in_same_state_as_overview(self):
        con = self.open_database()
        cash = account(con, "Cash", "checking")
        card = account(con, "Card", "credit_card")
        con.execute(
            "INSERT INTO balances(account_id,as_of,amount_minor,currency) VALUES(?,?,?,?)",
            (cash, "2026-01-01", 100_00, "ILS"),
        )
        con.execute(
            "INSERT INTO balances(account_id,as_of,amount_minor,currency) VALUES(?,?,?,?)",
            (card, "2026-01-01", -20_00, "ILS"),
        )

        state = AccountingEngine(con).state_at("2026-01-01")

        self.assertEqual(state.total_minor, Decimal("8000"))
        self.assertEqual(state.bucket_total_minor("Cash"), Decimal("10000"))
        self.assertEqual(state.bucket_total_minor("Liabilities"), Decimal("-2000"))
        self.assertEqual(
            sum((state.bucket_total_minor(name) for name in ("Cash", "Locked", "Investments", "Liabilities")), Decimal("0")),
            state.total_minor,
        )

    def test_unanchored_credit_card_charges_do_not_become_a_liability_balance(self):
        con = self.open_database()
        card = account(con, "Card", "credit_card")
        con.execute(
            """INSERT INTO transactions(
                   account_id,date,amount_minor,currency,category,flow_type
               ) VALUES(?,?,?,?,?,?)""",
            (card, "2026-01-01", -5_000, "ILS", "purchase", "expense"),
        )

        state = AccountingEngine(con).state_at("2026-01-01")

        self.assertEqual(state.account_total_minor(card), Decimal("0"))
        self.assertIn(
            "credit-card transaction history lacks payment coverage",
            state.rows[0].coverage.warnings,
        )

    def test_credit_card_charges_advance_a_real_balance_anchor(self):
        con = self.open_database()
        card = account(con, "Card", "credit_card")
        con.execute(
            "INSERT INTO balances(account_id,as_of,amount_minor,currency) VALUES(?,?,?,?)",
            (card, "2026-01-01", -2_000, "ILS"),
        )
        con.execute(
            """INSERT INTO transactions(
                   account_id,date,amount_minor,currency,category,flow_type
               ) VALUES(?,?,?,?,?,?)""",
            (card, "2026-01-02", -500, "ILS", "purchase", "expense"),
        )

        state = AccountingEngine(con).state_at("2026-01-02")

        self.assertEqual(state.account_total_minor(card), Decimal("-2500"))

    def test_savings_moves_from_locked_to_cash_on_liquidity_date(self):
        con = self.open_database()
        aid = account(con, "Term savings", "savings")
        con.execute(
            "UPDATE accounts SET liquidity_date=? WHERE id=?",
            ("2026-01-10", aid),
        )
        con.execute(
            "INSERT INTO balances(account_id,as_of,amount_minor,currency) VALUES(?,?,?,?)",
            (aid, "2026-01-01", 100_00, "ILS"),
        )

        before = AccountingEngine(con).state_at("2026-01-09")
        matured = AccountingEngine(con).state_at("2026-01-10")

        self.assertEqual(before.bucket_total_minor("Locked"), Decimal("10000"))
        self.assertEqual(matured.bucket_total_minor("Cash"), Decimal("10000"))
        self.assertEqual(matured.bucket_total_minor("Locked"), Decimal("0"))

    def test_study_fund_moves_from_locked_to_investments_when_liquid(self):
        con = self.open_database()
        aid = account(con, "Study fund", "study_fund")
        con.execute(
            "UPDATE accounts SET liquidity_date=? WHERE id=?",
            ("2026-01-10", aid),
        )
        con.executemany(
            """INSERT INTO balances(
                   account_id,as_of,component,amount_minor,currency
               ) VALUES(?,?,?,?,?)""",
            [
                (aid, "2026-01-01", "tagmul_employee", 40_000, "ILS"),
                (aid, "2026-01-01", "tagmul_employer", 60_000, "ILS"),
            ],
        )

        before = AccountingEngine(con).state_at("2026-01-09")
        liquid = AccountingEngine(con).state_at("2026-01-10")

        self.assertEqual(before.bucket_total_minor("Locked"), Decimal("100000"))
        self.assertEqual(liquid.bucket_total_minor("Investments"), Decimal("100000"))

    def test_short_snapshot_with_unsigned_market_value_is_rejected(self):
        con = self.open_database()
        aid = account(con, "Broker", "brokerage", "USD")
        sid = security(con, "SHORT")
        con.execute(
            """INSERT INTO positions(
                   account_id,security_id,as_of,quantity,avg_cost_minor,
                   market_value_minor,currency
               ) VALUES(?,?,?,?,?,?,?)""",
            (aid, sid, "2026-01-01", -10, 10_000, 80_000, "USD"),
        )
        con.execute(
            "INSERT INTO fx_rates(date,base_currency,quote_currency,rate) VALUES(?,?,?,?)",
            ("2026-01-01", "USD", "ILS", 4),
        )

        with self.assertRaisesRegex(AccountingError, "negative per-unit"):
            AccountingEngine(con).state_at("2026-01-01")

    def test_locked_account_uses_latest_coherent_snapshot(self):
        con = self.open_database()
        aid = account(con, "Pension", "pension")
        con.executemany(
            """INSERT INTO balances(
                   account_id,as_of,component,amount_minor,currency
               ) VALUES(?,?,?,?,?)""",
            [
                (aid, "2026-01-01", "tagmul_employee", 30_000, "ILS"),
                (aid, "2026-01-01", "tagmul_employer", 30_000, "ILS"),
                (aid, "2026-01-01", "pitsuyim", 30_000, "ILS"),
                (aid, "2026-02-01", None, 120_000, "ILS"),
                # A later partial breakdown must not displace the total.
                (aid, "2026-03-01", "tagmul_employee", 50_000, "ILS"),
                # A later complete breakdown may replace it.
                (aid, "2026-04-01", "tagmul_employee", 60_000, "ILS"),
                (aid, "2026-04-01", "tagmul_employer", 50_000, "ILS"),
                (aid, "2026-04-01", "pitsuyim", 40_000, "ILS"),
            ],
        )

        aggregate = AccountingEngine(con).state_at("2026-03-31")
        detailed = AccountingEngine(con).state_at("2026-04-30")

        self.assertEqual(aggregate.total_minor, Decimal("120000"))
        self.assertEqual(len(aggregate.rows), 1)
        self.assertEqual(detailed.total_minor, Decimal("150000"))
        self.assertEqual(len(detailed.rows), 3)

    def test_balance_snapshot_advances_only_with_later_events(self):
        con = self.open_database()
        aid = account(con, "Checking", "checking")
        con.execute(
            "INSERT INTO balances(account_id,as_of,amount_minor,currency) VALUES(?,?,?,?)",
            (aid, "2026-01-10", 100_00, "ILS"),
        )
        con.executemany(
            """INSERT INTO transactions(account_id,date,amount_minor,currency,category)
               VALUES(?,?,?,?,?)""",
            [
                (aid, "2026-01-10", -10_00, "ILS", "groceries"),
                (aid, "2026-01-11", -20_00, "ILS", "groceries"),
            ],
        )

        state = AccountingEngine(con).state_at("2026-01-11")

        self.assertEqual(state.total_minor, Decimal("8000"))
        self.assertEqual(state.rows[0].coverage.anchor_date, "2026-01-10")

    def test_position_snapshot_is_anchor_for_partial_trade_history(self):
        con = self.open_database()
        aid = account(con, "Broker", "brokerage", "USD")
        sid = security(con, "TEST")
        con.execute(
            """INSERT INTO positions(
                   account_id,security_id,as_of,quantity,avg_cost_minor,
                   market_price_minor,market_value_minor,currency
               ) VALUES(?,?,?,?,?,?,?,?)""",
            (aid, sid, "2024-01-01", 10, 10_000, 12_000, 120_000, "USD"),
        )
        con.execute(
            """INSERT INTO trades(
                   account_id,security_id,date,side,shares,price_minor,fees_minor,currency
               ) VALUES(?,?,?,?,?,?,?,?)""",
            (aid, sid, "2026-01-01", "sell", 5, 15_000, 0, "USD"),
        )
        con.execute(
            "INSERT INTO prices(security_id,date,close_minor,currency) VALUES(?,?,?,?)",
            (sid, "2026-01-02", 16_000, "USD"),
        )
        con.execute(
            "INSERT INTO fx_rates(date,base_currency,quote_currency,rate) VALUES(?,?,?,?)",
            ("2024-01-01", "USD", "ILS", 4),
        )

        state = AccountingEngine(con).state_at("2026-01-02")
        position = state.positions[0]
        security_row = next(row for row in state.rows if row.item_type == "security")

        self.assertEqual(position.quantity, Decimal("5"))
        self.assertEqual(position.cost_basis_minor, Decimal("50000"))
        self.assertEqual(position.average_cost_minor, Decimal("10000"))
        self.assertEqual(security_row.native_value_minor, Decimal("80000"))

    def test_two_brokerages_with_same_security_remain_separate(self):
        con = self.open_database()
        first = account(con, "Broker one", "brokerage", "USD")
        second = account(con, "Broker two", "brokerage", "USD")
        sid = security(con, "SHARED")
        con.execute(
            "INSERT INTO fx_rates(date,base_currency,quote_currency,rate) VALUES(?,?,?,?)",
            ("2026-01-01", "USD", "ILS", 3.5),
        )
        con.execute(
            "INSERT INTO prices(security_id,date,close_minor,currency) VALUES(?,?,?,?)",
            (sid, "2026-01-01", 2_000, "USD"),
        )
        for aid, qty in ((first, 2), (second, 3)):
            con.execute(
                """INSERT INTO positions(
                       account_id,security_id,as_of,quantity,avg_cost_minor,
                       market_price_minor,market_value_minor,currency
                   ) VALUES(?,?,?,?,?,?,?,?)""",
                (aid, sid, "2026-01-01", qty, 1_000, 2_000, qty * 2_000, "USD"),
            )

        state = AccountingEngine(con).state_at("2026-01-01")

        values = {
            row.account_id: row.native_value_minor
            for row in state.rows
            if row.item_type == "security"
        }
        self.assertEqual(values, {first: Decimal("4000"), second: Decimal("6000")})

    def test_external_flows_keep_currency_and_sign(self):
        con = self.open_database()
        aid = account(con, "Broker", "brokerage", "USD")
        con.execute(
            "INSERT INTO fx_rates(date,base_currency,quote_currency,rate) VALUES(?,?,?,?)",
            ("2026-01-01", "USD", "ILS", 4),
        )
        con.executemany(
            """INSERT INTO transactions(account_id,date,amount_minor,currency,category)
               VALUES(?,?,?,?,?)""",
            [
                (aid, "2026-01-01", 100_00, "USD", "deposit"),
                (aid, "2026-01-02", -25_00, "USD", "withdrawal"),
            ],
        )

        state = AccountingEngine(con).state_at("2026-01-02")

        self.assertEqual(
            [flow.reporting_amount_minor for flow in state.external_flows],
            [Decimal("40000"), Decimal("-10000")],
        )

    def test_two_feeds_evidence_one_canonical_trade_counted_once(self):
        con = self.open_database()
        aid = account(con, "Wrapper brokerage", "brokerage", "USD")
        sid = security(con, "ONE")
        feed_one = con.execute(
            """INSERT INTO account_feeds(account_id,provider,feed_type,external_account_ref)
               VALUES(?,?,?,?)""",
            (aid, "wrapper", "document", "wrapper-account"),
        ).lastrowid
        feed_two = con.execute(
            """INSERT INTO account_feeds(account_id,provider,feed_type,external_account_ref)
               VALUES(?,?,?,?)""",
            (aid, "ibkr", "api", "ibkr-account"),
        ).lastrowid
        canonical = con.execute(
            """INSERT INTO canonical_events(
                   account_id,event_type,occurred_on,currency,security_id,side,
                   quantity,price_minor,fees_minor
               ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (aid, "trade", "2026-01-01", "USD", sid, "buy", 10, 1000, 0),
        ).lastrowid
        sources = []
        for feed, key in ((feed_one, "statement-row-1"), (feed_two, "ibkr-trade-1")):
            source = con.execute(
                """INSERT INTO source_events(
                       account_feed_id,source_key,event_type,occurred_on
                   ) VALUES(?,?,?,?)""",
                (feed, key, "trade", "2026-01-01"),
            ).lastrowid
            sources.append(source)
        con.execute(
            """INSERT INTO event_evidence(
                   canonical_event_id,source_event_id,evidence_role,match_method,confidence
               ) VALUES(?,?,?,?,?)""",
            (canonical, sources[0], "primary", "reviewed match", 1.0),
        )
        con.execute(
            """INSERT INTO event_evidence(
                   canonical_event_id,source_event_id,evidence_role,match_method,confidence
               ) VALUES(?,?,?,?,?)""",
            (canonical, sources[1], "supporting", "provider id + economics", 1.0),
        )
        for source in sources:
            con.execute(
                """INSERT INTO trades(
                       account_id,security_id,date,side,shares,price_minor,fees_minor,
                       currency,source_event_id
                   ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (aid, sid, "2026-01-01", "buy", 10, 1000, 0, "USD", source),
            )
        con.execute(
            "INSERT INTO prices(security_id,date,close_minor,currency) VALUES(?,?,?,?)",
            (sid, "2026-01-02", 1200, "USD"),
        )
        con.execute(
            "INSERT INTO fx_rates(date,base_currency,quote_currency,rate) VALUES(?,?,?,?)",
            ("2026-01-01", "USD", "ILS", 4),
        )

        state = AccountingEngine(con).state_at("2026-01-02")

        self.assertEqual(len(state.positions), 1)
        self.assertEqual(state.positions[0].quantity, Decimal("10"))
        security_rows = [row for row in state.rows if row.item_type == "security"]
        self.assertEqual(len(security_rows), 1)
        self.assertEqual(security_rows[0].native_value_minor, Decimal("12000"))

    def test_source_evidence_suppresses_raw_cash_without_requiring_denormalized_link(self):
        con = self.open_database()
        aid = account(con, "Wrapper brokerage", "brokerage", "USD")
        feed = con.execute(
            """INSERT INTO account_feeds(account_id,provider,feed_type)
               VALUES(?,?,?)""",
            (aid, "wrapper", "document"),
        ).lastrowid
        source = con.execute(
            """INSERT INTO source_events(
                   account_feed_id,source_key,event_type,occurred_on
               ) VALUES(?,?,?,?)""",
            (feed, "cash-row-1", "cash", "2026-01-01"),
        ).lastrowid
        canonical = con.execute(
            """INSERT INTO canonical_events(
                   account_id,event_type,occurred_on,currency,amount_minor,
                   flow_type,category
               ) VALUES(?,?,?,?,?,?,?)""",
            (aid, "cash", "2026-01-01", "USD", 10_000, "transfer", "deposit"),
        ).lastrowid
        con.execute(
            """INSERT INTO event_evidence(
                   canonical_event_id,source_event_id,evidence_role,match_method
               ) VALUES(?,?,?,?)""",
            (canonical, source, "primary", "reviewed"),
        )
        # canonical_event_id is a convenience link, not the source of truth.  A
        # source linked through event_evidence must still be counted only once.
        con.execute(
            """INSERT INTO transactions(
                   account_id,date,amount_minor,currency,category,flow_type,source_event_id
               ) VALUES(?,?,?,?,?,?,?)""",
            (aid, "2026-01-01", 10_000, "USD", "deposit", "transfer", source),
        )
        con.execute(
            "INSERT INTO fx_rates(date,base_currency,quote_currency,rate) VALUES(?,?,?,?)",
            ("2026-01-01", "USD", "ILS", 4),
        )

        state = AccountingEngine(con).state_at("2026-01-01")

        self.assertEqual(len(state.external_flows), 1)
        self.assertEqual(state.external_flows[0].native_amount_minor, 10_000)
        cash = [row for row in state.rows if row.item_type == "cash"]
        self.assertEqual(len(cash), 1)
        self.assertEqual(cash[0].native_value_minor, Decimal("10000"))

    def test_canonical_investment_cash_without_trade_is_applied(self):
        con = self.open_database()
        aid = account(con, "Broker", "brokerage", "USD")
        con.execute(
            """INSERT INTO balances(
                   account_id,as_of,amount_minor,currency,component
               ) VALUES(?,?,?,?,?)""",
            (aid, "2026-01-01", 100_000, "USD", "cash_usd"),
        )
        con.execute(
            """INSERT INTO canonical_events(
                   account_id,event_type,occurred_on,currency,amount_minor,
                   flow_type,category,component
               ) VALUES(?,?,?,?,?,?,?,?)""",
            (
                aid,
                "cash",
                "2026-01-02",
                "USD",
                -20_000,
                "investment",
                "brokerage adjustment",
                "cash_usd",
            ),
        )

        state = AccountingEngine(con, "USD").state_at("2026-01-02")
        cash = next(row for row in state.rows if row.item_type == "cash")

        self.assertEqual(cash.native_value_minor, Decimal("80000"))

    def test_latest_position_anchor_is_selected_per_security(self):
        con = self.open_database()
        aid = account(con, "Broker", "brokerage", "USD")
        first = security(con, "AAA")
        second = security(con, "BBB")
        con.executemany(
            """INSERT INTO positions(
                   account_id,security_id,as_of,quantity,market_price_minor,
                   market_value_minor,currency
               ) VALUES(?,?,?,?,?,?,?)""",
            [
                (aid, first, "2026-01-01", 2, 1_000, 2_000, "USD"),
                (aid, second, "2026-01-02", 3, 2_000, 6_000, "USD"),
            ],
        )
        con.execute(
            "INSERT INTO fx_rates(date,base_currency,quote_currency,rate) VALUES(?,?,?,?)",
            ("2026-01-01", "USD", "ILS", 4),
        )

        state = AccountingEngine(con).state_at("2026-01-03")

        self.assertEqual(
            {(row.ticker, row.quantity) for row in state.positions},
            {("AAA", Decimal("2")), ("BBB", Decimal("3"))},
        )

    def _cost_snapshots(self, later_quantity: int = 10):
        con = self.open_database()
        aid = account(con, "Broker", "brokerage", "USD")
        sid = security(con, "AAA")
        con.executemany(
            """INSERT INTO positions(
                   account_id,security_id,as_of,quantity,avg_cost_minor,
                   market_price_minor,market_value_minor,currency
               ) VALUES(?,?,?,?,?,?,?,?)""",
            [
                (aid, sid, "2026-01-01", 10, 800, 1_000, 10_000, "USD"),
                (
                    aid, sid, "2026-01-03", later_quantity, None, 1_100,
                    later_quantity * 1_100, "USD",
                ),
            ],
        )
        return con, aid, sid

    def test_cost_less_snapshot_keeps_unchanged_earlier_basis(self):
        con, _, _ = self._cost_snapshots()

        position = AccountingEngine(con, "USD").state_at("2026-01-03").positions[0]

        self.assertEqual(position.anchor_date, "2026-01-03")
        self.assertEqual(position.average_cost_minor, Decimal("800"))
        self.assertEqual(position.reporting_cost_basis_minor, Decimal("8000"))

    def test_cost_less_snapshot_after_a_trade_leaves_basis_unknown(self):
        con, aid, sid = self._cost_snapshots()
        con.execute(
            """INSERT INTO trades(
                   account_id,security_id,date,side,shares,price_minor,
                   fees_minor,currency
               ) VALUES(?,?,?,?,?,?,?,?)""",
            (aid, sid, "2026-01-02", "buy", 1, 1_000, 0, "USD"),
        )

        position = AccountingEngine(con, "USD").state_at("2026-01-03").positions[0]

        self.assertIsNone(position.average_cost_minor)

    def test_cost_less_snapshot_with_new_quantity_leaves_basis_unknown(self):
        con, _, _ = self._cost_snapshots(later_quantity=12)

        position = AccountingEngine(con, "USD").state_at("2026-01-03").positions[0]

        self.assertIsNone(position.average_cost_minor)

    def test_position_snapshot_does_not_reset_partial_cash_reconstruction(self):
        con = self.open_database()
        aid = account(con, "Broker", "brokerage", "USD")
        sid = security(con, "CASH")
        con.executemany(
            """INSERT INTO positions(
                   account_id,security_id,as_of,quantity,market_price_minor,
                   market_value_minor,currency
               ) VALUES(?,?,?,?,?,?,?)""",
            [
                (aid, sid, "2026-01-01", 10, 1_000, 10_000, "USD"),
                (aid, sid, "2026-01-03", 10, 1_000, 10_000, "USD"),
            ],
        )
        con.execute(
            """INSERT INTO transactions(
                   account_id,date,amount_minor,currency,category,flow_type
               ) VALUES(?,?,?,?,?,?)""",
            (aid, "2026-01-02", 5_000, "USD", "deposit", "transfer"),
        )

        before = AccountingEngine(con, "USD").state_at("2026-01-02")
        after = AccountingEngine(con, "USD").state_at("2026-01-03")
        before_cash = next(row for row in before.rows if row.item_type == "cash")
        after_cash = next(row for row in after.rows if row.item_type == "cash")

        self.assertEqual(before_cash.native_value_minor, Decimal("5000"))
        self.assertEqual(after_cash.native_value_minor, Decimal("5000"))
        self.assertIsNone(after_cash.coverage.anchor_date)
        self.assertEqual(after_cash.coverage.method, "events-partial")
        self.assertIn("no opening balance snapshot", after_cash.coverage.warnings)

    def test_pre_opening_evidence_does_not_seed_successor_account(self):
        con = self.open_database()
        aid = account(con, "Successor", "checking", "ILS")
        con.execute(
            "UPDATE accounts SET opened_on='2026-02-24' WHERE id=?", (aid,)
        )
        con.execute(
            "INSERT INTO balances(account_id,as_of,amount_minor,currency) VALUES(?,?,?,?)",
            (aid, "2026-02-14", 50_000, "ILS"),
        )
        con.executemany(
            """INSERT INTO transactions(
                   account_id,date,amount_minor,currency,category,flow_type
               ) VALUES(?,?,?,?,?,?)""",
            [
                (aid, "2026-02-15", -5_000, "ILS", "old", "expense"),
                (aid, "2026-02-24", 20_000, "ILS", "migration", "transfer"),
            ],
        )

        before = AccountingEngine(con).state_at("2026-02-23")
        after = AccountingEngine(con).state_at("2026-02-24")

        self.assertEqual(before.account_total_minor(aid), Decimal("0"))
        self.assertEqual(after.account_total_minor(aid), Decimal("20000"))
        self.assertEqual(after.rows[0].coverage.method, "events-partial")
        self.assertIsNone(after.rows[0].coverage.anchor_date)

    def test_locked_contributions_are_visible_before_first_snapshot(self):
        con = self.open_database()
        aid = account(con, "Pension", "pension", "ILS")
        con.executemany(
            """INSERT INTO transactions(
                   account_id,date,amount_minor,currency,category,flow_type
               ) VALUES(?,?,?,?,?,?)""",
            [
                (aid, "2026-01-01", 1_000, "ILS", "pension_deposit", "transfer"),
                (aid, "2026-01-02", 2_000, "ILS", "pension_deposit", "transfer"),
            ],
        )

        state = AccountingEngine(con).state_at("2026-01-02")

        self.assertEqual(state.account_total_minor(aid), Decimal("3000"))
        self.assertEqual(state.rows[0].coverage.method, "events-partial")
        self.assertIn("no opening balance snapshot", state.rows[0].coverage.warnings)

    def test_locked_snapshot_carries_only_later_contributions(self):
        con = self.open_database()
        aid = account(con, "Pension", "pension", "ILS")
        components = ("tagmul_employee", "tagmul_employer", "pitsuyim")
        con.executemany(
            """INSERT INTO balances(
                   account_id,as_of,component,amount_minor,currency
               ) VALUES(?,?,?,?,?)""",
            [(aid, "2026-01-01", component, 2_000, "ILS") for component in components],
        )
        con.execute(
            """INSERT INTO transactions(
                   account_id,date,amount_minor,currency,category,flow_type
               ) VALUES(?,?,?,?,?,?)""",
            (aid, "2026-01-02", 1_000, "ILS", "pension_deposit", "transfer"),
        )

        carried = AccountingEngine(con).state_at("2026-01-02")

        self.assertEqual(carried.account_total_minor(aid), Decimal("7000"))
        known_flows = next(
            row for row in carried.rows if row.item_key == "known_events_ils"
        )
        self.assertEqual(known_flows.native_value_minor, Decimal("1000"))
        self.assertEqual(known_flows.coverage.method, "snapshot+events")

        con.executemany(
            """INSERT INTO balances(
                   account_id,as_of,component,amount_minor,currency
               ) VALUES(?,?,?,?,?)""",
            [
                (aid, "2026-01-03", "tagmul_employee", 2_000, "ILS"),
                (aid, "2026-01-03", "tagmul_employer", 2_000, "ILS"),
                (aid, "2026-01-03", "pitsuyim", 3_000, "ILS"),
            ],
        )

        reanchored = AccountingEngine(con).state_at("2026-01-03")

        self.assertEqual(reanchored.account_total_minor(aid), Decimal("7000"))
        known_flows = next(
            row for row in reanchored.rows if row.item_key == "known_events_ils"
        )
        self.assertEqual(known_flows.native_value_minor, Decimal("0"))

    def test_pension_snapshot_rolls_forward_with_later_payslips(self):
        con = self.open_database()
        aid = account(con, "Pension", "pension", "ILS")
        con.execute(
            """INSERT INTO balances(account_id,as_of,amount_minor,currency)
               VALUES(?,'2026-01-31',600000,'ILS')""",
            (aid,),
        )
        con.executemany(
            """INSERT INTO payslips(
                   period_start,period_end,paid_on,employer,gross_minor,net_minor,
                   pension_employee_minor,pension_employer_minor,currency
               ) VALUES(?,?,?,?,?,?,?,?,?)""",
            [
                (
                    "2026-02-01", "2026-02-28", "2026-03-05", "Employer",
                    100000, 70000, 10000, 15000, "ILS",
                ),
                (
                    "2026-03-01", "2026-03-31", "2026-04-05", "Employer",
                    100000, 70000, 11000, 16000, "ILS",
                ),
            ],
        )

        state = AccountingEngine(con).state_at("2026-04-05")

        self.assertEqual(state.account_total_minor(aid), Decimal("652000"))
        estimate = next(
            row for row in state.rows if row.item_key == "known_events_ils"
        )
        self.assertEqual(estimate.native_value_minor, Decimal("52000"))
        self.assertIn(
            "payslip contributions are estimated until the next fund statement",
            estimate.coverage.warnings,
        )

    def test_payslip_paid_after_snapshot_is_not_hidden_by_its_pay_period(self):
        con = self.open_database()
        aid = account(con, "Pension", "pension", "ILS")
        con.execute(
            """INSERT INTO balances(account_id,as_of,amount_minor,currency)
               VALUES(?,'2026-03-01',600000,'ILS')""",
            (aid,),
        )
        con.execute(
            """INSERT INTO payslips(
                   period_start,period_end,paid_on,employer,gross_minor,net_minor,
                   pension_employee_minor,pension_employer_minor,currency
               ) VALUES(
                   '2026-02-01','2026-02-28','2026-03-05','Employer',
                   100000,70000,10000,15000,'ILS'
               )"""
        )

        state = AccountingEngine(con).state_at("2026-03-05")

        self.assertEqual(state.account_total_minor(aid), Decimal("625000"))

    def test_new_fund_snapshot_replaces_prior_payslip_estimates(self):
        con = self.open_database()
        aid = account(con, "Study fund", "study_fund", "ILS")
        con.execute(
            """INSERT INTO balances(account_id,as_of,amount_minor,currency)
               VALUES(?,'2026-01-31',600000,'ILS')""",
            (aid,),
        )
        con.execute(
            """INSERT INTO payslips(
                   period_start,period_end,paid_on,employer,gross_minor,net_minor,
                   study_fund_employee_minor,study_fund_employer_minor,currency
               ) VALUES(
                   '2026-02-01','2026-02-28','2026-03-05','Employer',
                   100000,70000,5000,7500,'ILS'
               )"""
        )

        estimated = AccountingEngine(con).state_at("2026-03-05")
        self.assertEqual(estimated.account_total_minor(aid), Decimal("612500"))

        con.execute(
            """INSERT INTO balances(account_id,as_of,amount_minor,currency)
               VALUES(?,'2026-03-31',620000,'ILS')""",
            (aid,),
        )

        state = AccountingEngine(con).state_at("2026-03-31")

        self.assertEqual(state.account_total_minor(aid), Decimal("620000"))
        self.assertFalse(
            any(row.item_key == "known_events_ils" for row in state.rows)
        )

    def test_exact_fund_movements_take_precedence_over_older_payslips(self):
        con = self.open_database()
        aid = account(con, "Pension", "pension", "ILS")
        con.execute(
            """INSERT INTO balances(account_id,as_of,amount_minor,currency)
               VALUES(?,'2026-01-31',600000,'ILS')""",
            (aid,),
        )
        con.executemany(
            """INSERT INTO payslips(
                   period_start,period_end,paid_on,employer,gross_minor,net_minor,
                   pension_employee_minor,pension_employer_minor,currency
               ) VALUES(?,?,?,?,?,?,?,?,?)""",
            [
                (
                    "2026-02-01", "2026-02-28", "2026-03-05", "Employer",
                    100000, 70000, 10000, 15000, "ILS",
                ),
                (
                    "2026-03-01", "2026-03-31", "2026-04-05", "Employer",
                    100000, 70000, 11000, 16000, "ILS",
                ),
            ],
        )
        con.execute(
            """INSERT INTO transactions(
                   account_id,date,amount_minor,currency,category,flow_type
               ) VALUES(?,'2026-03-20',26000,'ILS','pension_deposit','transfer')""",
            (aid,),
        )

        state = AccountingEngine(con).state_at("2026-04-05")

        # The exact movement covers older payroll rows; only the newer payslip is
        # estimated on top of it.
        self.assertEqual(state.account_total_minor(aid), Decimal("653000"))

    def test_payslip_contributions_are_not_duplicated_across_two_funds(self):
        con = self.open_database()
        first = account(con, "Pension A", "pension", "ILS")
        second = account(con, "Pension B", "pension", "ILS")
        for aid in (first, second):
            con.execute(
                """INSERT INTO balances(account_id,as_of,amount_minor,currency)
                   VALUES(?,'2026-01-31',600000,'ILS')""",
                (aid,),
            )
        con.execute(
            """INSERT INTO payslips(
                   period_start,period_end,paid_on,employer,gross_minor,net_minor,
                   pension_employee_minor,pension_employer_minor,currency
               ) VALUES(
                   '2026-02-01','2026-02-28','2026-03-05','Employer',
                   100000,70000,10000,15000,'ILS'
               )"""
        )

        state = AccountingEngine(con).state_at("2026-03-05")

        self.assertEqual(state.account_total_minor(first), Decimal("600000"))
        self.assertEqual(state.account_total_minor(second), Decimal("600000"))
        self.assertTrue(
            all(
                "payslip contributions could not be routed to one fund account"
                in row.coverage.warnings
                for row in state.rows
                if row.item_key == "known_events_ils"
            )
        )

    def test_non_brokerage_snapshot_key_is_stable_across_source_shapes(self):
        con = self.open_database()
        aid = account(con, "Card", "credit_card", "ILS")
        con.execute(
            """INSERT INTO balances(
                   account_id,as_of,component,amount_minor,currency
               ) VALUES(?,?,?,?,?)""",
            (aid, "2026-01-01", "next_bill", -1_000, "ILS"),
        )
        component_state = AccountingEngine(con).state_at("2026-01-01")
        con.execute(
            """INSERT INTO balances(account_id,as_of,amount_minor,currency)
               VALUES(?,?,?,?)""",
            (aid, "2026-01-02", -1_000, "ILS"),
        )
        whole_state = AccountingEngine(con).state_at("2026-01-02")

        self.assertEqual(component_state.rows[0].item_key, "balance")
        self.assertEqual(whole_state.rows[0].item_key, "balance")

    def test_trade_currency_mismatch_fails_before_mixing_holding_costs(self):
        con = self.open_database()
        aid = account(con, "Broker", "brokerage", "USD")
        sid = security(con, "MIXED", "USD")
        con.execute(
            """INSERT INTO positions(
                   account_id,security_id,as_of,quantity,avg_cost_minor,
                   market_price_minor,market_value_minor,currency
               ) VALUES(?,?,?,?,?,?,?,?)""",
            (aid, sid, "2026-01-01", 10, 1_000, 1_000, 10_000, "USD"),
        )
        con.execute(
            """INSERT INTO trades(
                   account_id,security_id,date,side,shares,price_minor,fees_minor,currency
               ) VALUES(?,?,?,?,?,?,?,?)""",
            (aid, sid, "2026-01-02", "buy", 1, 1_000, 0, "GBP"),
        )

        with self.assertRaisesRegex(AccountingError, "trade currency"):
            AccountingEngine(con, "USD").state_at("2026-01-02")

    def test_aggregate_brokerage_snapshot_is_not_discarded_by_partial_trades(self):
        con = self.open_database()
        aid = account(con, "Broker", "brokerage", "ILS")
        sid = security(con, "NEW", "ILS")
        con.execute(
            "INSERT INTO balances(account_id,as_of,amount_minor,currency) VALUES(?,?,?,?)",
            (aid, "2026-01-01", 10_000, "ILS"),
        )
        con.execute(
            """INSERT INTO trades(
                   account_id,security_id,date,side,shares,price_minor,fees_minor,currency
               ) VALUES(?,?,?,?,?,?,?,?)""",
            (aid, sid, "2026-01-02", "buy", 1, 1_000, 0, "ILS"),
        )
        con.execute(
            "INSERT INTO prices(security_id,date,close_minor,currency) VALUES(?,?,?,?)",
            (sid, "2026-01-02", 1_000, "ILS"),
        )

        state = AccountingEngine(con).state_at("2026-01-02")

        self.assertEqual(state.total_minor, Decimal("10000"))
        self.assertEqual(len(state.rows), 1)
        self.assertEqual(state.rows[0].coverage.method, "aggregate-snapshot")
        self.assertTrue(state.rows[0].coverage.warnings)

    def test_partial_position_snapshot_does_not_replace_aggregate_total(self):
        con = self.open_database()
        aid = account(con, "Broker", "brokerage", "ILS")
        sid = security(con, "PARTIAL", "ILS")
        con.execute(
            "INSERT INTO balances(account_id,as_of,amount_minor,currency) VALUES(?,?,?,?)",
            (aid, "2026-01-01", 100_000, "ILS"),
        )
        con.execute(
            """INSERT INTO positions(
                   account_id,security_id,as_of,quantity,avg_cost_minor,
                   market_price_minor,market_value_minor,currency
               ) VALUES(?,?,?,?,?,?,?,?)""",
            (aid, sid, "2026-01-02", 2, 9_000, 10_000, 20_000, "ILS"),
        )

        state = AccountingEngine(con).state_at("2026-01-02")

        self.assertEqual(state.total_minor, Decimal("100000"))
        self.assertEqual(state.rows[0].item_key, "aggregate")
        self.assertIn("partial", " ".join(state.rows[0].coverage.warnings))

    def test_explicit_complete_account_snapshot_replaces_aggregate_total(self):
        con = self.open_database()
        aid = account(con, "Broker", "brokerage", "ILS")
        sid = security(con, "COMPLETE", "ILS")
        feed_id = con.execute(
            """INSERT INTO account_feeds(account_id,provider,feed_type)
               VALUES(?,?,?)""",
            (aid, "complete-test", "api"),
        ).lastrowid
        con.execute(
            "INSERT INTO balances(account_id,as_of,amount_minor,currency) VALUES(?,?,?,?)",
            (aid, "2026-01-01", 100_000, "ILS"),
        )
        con.execute(
            """INSERT INTO balances(
                   account_id,as_of,amount_minor,currency,component
               ) VALUES(?,?,?,?,?)""",
            (aid, "2026-01-02", 0, "ILS", "cash_ils"),
        )
        position_id = con.execute(
            """INSERT INTO positions(
                   account_id,security_id,as_of,quantity,avg_cost_minor,
                   market_price_minor,market_value_minor,currency
               ) VALUES(?,?,?,?,?,?,?,?)""",
            (aid, sid, "2026-01-02", 2, 9_000, 10_000, 20_000, "ILS"),
        ).lastrowid
        con.execute(
            """INSERT INTO position_evidence(
                   canonical_position_id,account_feed_id,source_key,
                   observed_quantity,observed_price_minor,observed_value_minor,
                   observed_currency,account_snapshot_complete
               ) VALUES(?,?,?,?,?,?,?,1)""",
            (position_id, feed_id, "complete:2026-01-02", 2, 10_000, 20_000, "ILS"),
        )

        state = AccountingEngine(con).state_at("2026-01-02")

        self.assertEqual(state.total_minor, Decimal("20000"))
        self.assertNotIn("aggregate", {row.item_key for row in state.rows})

    def test_complete_account_snapshot_zeros_omitted_older_security(self):
        con = self.open_database()
        aid = account(con, "Broker", "brokerage", "ILS")
        old_sid = security(con, "OLD", "ILS")
        current_sid = security(con, "CURRENT", "ILS")
        feed_id = con.execute(
            """INSERT INTO account_feeds(account_id,provider,feed_type)
               VALUES(?,?,?)""",
            (aid, "complete-test", "api"),
        ).lastrowid
        con.execute(
            """INSERT INTO positions(
                   account_id,security_id,as_of,quantity,market_price_minor,
                   market_value_minor,currency
               ) VALUES(?,?,?,?,?,?,?)""",
            (aid, old_sid, "2026-01-01", 5, 1_000, 5_000, "ILS"),
        )
        current_position = con.execute(
            """INSERT INTO positions(
                   account_id,security_id,as_of,quantity,market_price_minor,
                   market_value_minor,currency
               ) VALUES(?,?,?,?,?,?,?)""",
            (aid, current_sid, "2026-02-01", 2, 2_000, 4_000, "ILS"),
        ).lastrowid
        con.execute(
            """INSERT INTO position_evidence(
                   canonical_position_id,account_feed_id,source_key,
                   observed_quantity,observed_price_minor,observed_value_minor,
                   observed_currency,account_snapshot_complete
               ) VALUES(?,?,?,?,?,?,?,1)""",
            (
                current_position,
                feed_id,
                "complete:2026-02-01",
                2,
                2_000,
                4_000,
                "ILS",
            ),
        )

        state = AccountingEngine(con).state_at("2026-02-01")

        self.assertEqual(
            {(row.ticker, row.quantity) for row in state.positions},
            {("CURRENT", Decimal("2"))},
        )
        self.assertEqual(state.total_minor, Decimal("4000"))

    def test_complete_zero_position_snapshot_can_retire_aggregate_total(self):
        con = self.open_database()
        aid = account(con, "Broker", "brokerage", "ILS")
        feed_id = con.execute(
            """INSERT INTO account_feeds(account_id,provider,feed_type)
               VALUES(?,?,?)""",
            (aid, "empty-complete-test", "api"),
        ).lastrowid
        con.execute(
            "INSERT INTO balances(account_id,as_of,amount_minor,currency) VALUES(?,?,?,?)",
            (aid, "2026-01-01", 100_000, "ILS"),
        )
        balance_id = con.execute(
            """INSERT INTO balances(
                   account_id,as_of,amount_minor,currency,component
               ) VALUES(?,?,?,?,?)""",
            (aid, "2026-01-02", 0, "ILS", "cash_ils"),
        ).lastrowid
        con.execute(
            """INSERT INTO balance_evidence(
                   canonical_balance_id,account_feed_id,source_key,
                   observed_amount_minor,observed_currency,
                   account_snapshot_complete
               ) VALUES(?,?,?,?,?,1)""",
            (balance_id, feed_id, "empty:2026-01-02", 0, "ILS"),
        )

        state = AccountingEngine(con).state_at("2026-01-02")

        self.assertEqual(state.total_minor, Decimal("0"))
        self.assertNotIn("aggregate", {row.item_key for row in state.rows})
        self.assertEqual(len(state.positions), 0)

    def test_aggregate_total_advances_with_later_noninvestment_cash_flows(self):
        con = self.open_database()
        aid = account(con, "Broker", "brokerage", "ILS")
        con.execute(
            "INSERT INTO balances(account_id,as_of,amount_minor,currency) VALUES(?,?,?,?)",
            (aid, "2026-01-01", 100_000, "ILS"),
        )
        con.executemany(
            """INSERT INTO transactions(
                   account_id,date,amount_minor,currency,category,flow_type
               ) VALUES(?,?,?,?,?,?)""",
            [
                (aid, "2026-01-02", 10_000, "ILS", "deposit", "transfer"),
                (aid, "2026-01-03", -5_000, "ILS", "withdrawal", "transfer"),
            ],
        )

        state = AccountingEngine(con).state_at("2026-01-03")

        self.assertEqual(state.total_minor, Decimal("105000"))
        self.assertEqual(state.rows[0].coverage.method, "aggregate-snapshot+flows")

    def test_aggregate_total_keeps_unlinked_canonical_investment_cash_separate(self):
        con = self.open_database()
        aid = account(con, "Broker", "brokerage", "ILS")
        con.execute(
            "INSERT INTO balances(account_id,as_of,amount_minor,currency) VALUES(?,?,?,?)",
            (aid, "2026-01-01", 100_000, "ILS"),
        )
        con.execute(
            """INSERT INTO canonical_events(
                   account_id,event_type,occurred_on,currency,amount_minor,
                   flow_type,category
               ) VALUES(?,?,?,?,?,?,?)""",
            (aid, "cash", "2026-01-02", "ILS", -5_000, "investment", "adjustment"),
        )

        state = AccountingEngine(con).state_at("2026-01-02")

        self.assertEqual(state.total_minor, Decimal("95000"))

    def test_aggregate_total_applies_known_later_trade_fees(self):
        con = self.open_database()
        aid = account(con, "Broker", "brokerage", "ILS")
        sid = security(con, "FEE", "ILS")
        con.execute(
            "INSERT INTO balances(account_id,as_of,amount_minor,currency) VALUES(?,?,?,?)",
            (aid, "2026-01-01", 100_000, "ILS"),
        )
        con.execute(
            """INSERT INTO trades(
                   account_id,security_id,date,side,shares,price_minor,fees_minor,currency
               ) VALUES(?,?,?,?,?,?,?,?)""",
            (aid, sid, "2026-01-02", "buy", 1, 10_000, 1_000, "ILS"),
        )

        state = AccountingEngine(con).state_at("2026-01-02")

        self.assertEqual(state.total_minor, Decimal("99000"))

    def test_aggregate_flow_coverage_includes_its_fx_dependency(self):
        con = self.open_database()
        aid = account(con, "Broker", "brokerage", "ILS")
        con.execute(
            "INSERT INTO balances(account_id,as_of,amount_minor,currency) VALUES(?,?,?,?)",
            (aid, "2026-07-20", 100_000, "ILS"),
        )
        con.execute(
            """INSERT INTO transactions(
                   account_id,date,amount_minor,currency,category,flow_type
               ) VALUES(?,?,?,?,?,?)""",
            (aid, "2026-07-21", 10_000, "USD", "deposit", "transfer"),
        )
        con.execute(
            "INSERT INTO fx_rates(date,base_currency,quote_currency,rate) VALUES(?,?,?,?)",
            ("2026-01-01", "USD", "ILS", 3),
        )

        state = AccountingEngine(con).state_at("2026-07-21")
        row = state.rows[0]

        self.assertEqual(row.reporting_value_minor, Decimal("130000"))
        self.assertEqual(row.coverage.observed_through, "2026-01-01")
        self.assertEqual(row.coverage.stale_days, 201)

    def test_zero_position_anchor_starts_later_buy_with_known_basis(self):
        con = self.open_database()
        aid = account(con, "Broker", "brokerage", "ILS")
        sid = security(con, "REOPEN", "ILS")
        con.execute(
            """INSERT INTO positions(
                   account_id,security_id,as_of,quantity,market_value_minor,currency
               ) VALUES(?,?,?,?,?,?)""",
            (aid, sid, "2026-01-01", 0, 0, "ILS"),
        )
        con.execute(
            """INSERT INTO trades(
                   account_id,security_id,date,side,shares,price_minor,fees_minor,currency
               ) VALUES(?,?,?,?,?,?,?,?)""",
            (aid, sid, "2026-01-02", "buy", 10, 1_000, 0, "ILS"),
        )
        con.execute(
            "INSERT INTO prices(security_id,date,close_minor,currency) VALUES(?,?,?,?)",
            (sid, "2026-01-02", 1_200, "ILS"),
        )

        position = AccountingEngine(con).state_at("2026-01-02").positions[0]

        self.assertEqual(position.quantity, Decimal("10"))
        self.assertEqual(position.cost_basis_minor, Decimal("10000"))
        self.assertEqual(position.average_cost_minor, Decimal("1000"))

    def test_foreign_zero_anchor_does_not_require_pre_anchor_fx(self):
        con = self.open_database()
        aid = account(con, "Broker", "brokerage", "USD")
        sid = security(con, "ZERO-FX", "USD")
        con.execute(
            """INSERT INTO positions(
                   account_id,security_id,as_of,quantity,market_value_minor,currency
               ) VALUES(?,?,?,?,?,?)""",
            (aid, sid, "2026-01-01", 0, 0, "USD"),
        )
        con.execute(
            """INSERT INTO trades(
                   account_id,security_id,date,side,shares,price_minor,fees_minor,currency
               ) VALUES(?,?,?,?,?,?,?,?)""",
            (aid, sid, "2026-01-02", "buy", 1, 1_000, 0, "USD"),
        )
        con.execute(
            "INSERT INTO prices(security_id,date,close_minor,currency) VALUES(?,?,?,?)",
            (sid, "2026-01-02", 1_200, "USD"),
        )
        con.execute(
            "INSERT INTO fx_rates(date,base_currency,quote_currency,rate) VALUES(?,?,?,?)",
            ("2026-01-02", "USD", "ILS", 3),
        )

        position = AccountingEngine(con).state_at("2026-01-02").positions[0]

        self.assertEqual(position.cost_basis_minor, Decimal("1000"))
        self.assertEqual(position.reporting_cost_basis_minor, Decimal("3000"))

    def test_partial_cover_preserves_short_average_cost_basis(self):
        con = self.open_database()
        aid = account(con, "Broker", "brokerage", "ILS")
        sid = security(con, "SHORT", "ILS")
        con.execute(
            """INSERT INTO positions(
                   account_id,security_id,as_of,quantity,avg_cost_minor,
                   market_price_minor,market_value_minor,currency
               ) VALUES(?,?,?,?,?,?,?,?)""",
            (aid, sid, "2026-01-01", -10, 1_000, 900, -9_000, "ILS"),
        )
        con.execute(
            """INSERT INTO trades(
                   account_id,security_id,date,side,shares,price_minor,fees_minor,currency
               ) VALUES(?,?,?,?,?,?,?,?)""",
            (aid, sid, "2026-01-02", "buy", 5, 800, 0, "ILS"),
        )
        con.execute(
            "INSERT INTO prices(security_id,date,close_minor,currency) VALUES(?,?,?,?)",
            (sid, "2026-01-02", 900, "ILS"),
        )

        position = AccountingEngine(con).state_at("2026-01-02").positions[0]

        self.assertEqual(position.quantity, Decimal("-5"))
        self.assertEqual(position.cost_basis_minor, Decimal("-5000"))
        self.assertEqual(position.average_cost_minor, Decimal("1000"))

    def test_sell_crossing_zero_starts_a_known_short_basis(self):
        con = self.open_database()
        aid = account(con, "Broker", "brokerage", "ILS")
        sid = security(con, "CROSS", "ILS")
        con.execute(
            """INSERT INTO positions(
                   account_id,security_id,as_of,quantity,avg_cost_minor,
                   market_price_minor,market_value_minor,currency
               ) VALUES(?,?,?,?,?,?,?,?)""",
            (aid, sid, "2026-01-01", 2, 1_000, 1_100, 2_200, "ILS"),
        )
        con.execute(
            """INSERT INTO trades(
                   account_id,security_id,date,side,shares,price_minor,fees_minor,currency
               ) VALUES(?,?,?,?,?,?,?,?)""",
            (aid, sid, "2026-01-02", "sell", 5, 1_200, 30, "ILS"),
        )
        con.execute(
            "INSERT INTO prices(security_id,date,close_minor,currency) VALUES(?,?,?,?)",
            (sid, "2026-01-02", 1_100, "ILS"),
        )

        position = AccountingEngine(con).state_at("2026-01-02").positions[0]

        # Three fifths of the order opens the short: -3*1200 + 3/5*30.
        self.assertEqual(position.quantity, Decimal("-3"))
        self.assertEqual(position.cost_basis_minor, Decimal("-3582.0"))
        self.assertEqual(position.average_cost_minor, Decimal("1194.0"))

    def test_position_uses_as_of_fx_and_quote_date_for_freshness(self):
        con = self.open_database()
        aid = account(con, "Broker", "brokerage", "USD")
        sid = security(con, "STALE")
        con.execute(
            """INSERT INTO positions(
                   account_id,security_id,as_of,quantity,market_price_minor,
                   market_value_minor,currency
               ) VALUES(?,?,?,?,?,?,?)""",
            (aid, sid, "2026-01-01", 1, 1_000, 1_000, "USD"),
        )
        con.execute(
            "INSERT INTO prices(security_id,date,close_minor,currency) VALUES(?,?,?,?)",
            (sid, "2026-01-01", 1_000, "USD"),
        )
        con.executemany(
            "INSERT INTO fx_rates(date,base_currency,quote_currency,rate) VALUES(?,?,?,?)",
            [
                ("2026-01-01", "USD", "ILS", 3),
                ("2026-01-10", "USD", "ILS", 4),
            ],
        )

        state = AccountingEngine(con).state_at("2026-01-10")
        row = next(item for item in state.rows if item.item_type == "security")

        self.assertEqual(row.reporting_value_minor, Decimal("4000"))
        self.assertEqual(row.coverage.observed_through, "2026-01-01")
        self.assertEqual(row.coverage.stale_days, 9)

    def test_stale_fx_is_a_limiting_position_valuation_dependency(self):
        con = self.open_database()
        aid = account(con, "Broker", "brokerage", "USD")
        sid = security(con, "FRESH-QUOTE")
        con.execute(
            """INSERT INTO positions(
                   account_id,security_id,as_of,quantity,market_price_minor,
                   market_value_minor,currency
               ) VALUES(?,?,?,?,?,?,?)""",
            (aid, sid, "2026-01-01", 1, 10_000, 10_000, "USD"),
        )
        con.execute(
            "INSERT INTO prices(security_id,date,close_minor,currency) VALUES(?,?,?,?)",
            (sid, "2026-07-21", 12_000, "USD"),
        )
        con.execute(
            """INSERT INTO fx_rates(
                   date,base_currency,quote_currency,rate,source
               ) VALUES(?,?,?,?,?)""",
            ("2026-01-01", "USD", "ILS", 3, "yahoo"),
        )

        state = AccountingEngine(con).state_at("2026-07-21")
        row = next(item for item in state.rows if item.item_type == "security")

        self.assertEqual(row.reporting_value_minor, Decimal("36000"))
        self.assertEqual(row.coverage.observed_through, "2026-01-01")
        self.assertEqual(row.coverage.stale_days, 201)

    def test_newer_snapshot_market_price_beats_older_cached_close(self):
        con = self.open_database()
        aid = account(con, "Broker", "brokerage", "ILS")
        sid = security(con, "DATED", "ILS")
        con.execute(
            "INSERT INTO prices(security_id,date,close_minor,currency) VALUES(?,?,?,?)",
            (sid, "2025-01-01", 1_000, "ILS"),
        )
        con.execute(
            """INSERT INTO positions(
                   account_id,security_id,as_of,quantity,market_price_minor,
                   market_value_minor,currency
               ) VALUES(?,?,?,?,?,?,?)""",
            (aid, sid, "2026-01-01", 2, 2_000, 4_000, "ILS"),
        )

        state = AccountingEngine(con).state_at("2026-01-02")
        row = next(item for item in state.rows if item.item_type == "security")

        self.assertEqual(row.native_value_minor, Decimal("4000"))
        self.assertEqual(row.unit_price_minor, Decimal("2000"))
        self.assertEqual(row.coverage.method, "snapshot-price")
        self.assertEqual(row.coverage.observed_through, "2026-01-01")

    def test_fresher_inverse_fx_quote_beats_stale_direct_quote(self):
        con = self.open_database()
        con.executemany(
            "INSERT INTO fx_rates(date,base_currency,quote_currency,rate) VALUES(?,?,?,?)",
            [
                ("2026-01-01", "USD", "ILS", 3),
                ("2026-01-02", "ILS", "USD", 0.25),
            ],
        )

        self.assertEqual(
            FxBook(con).rate("USD", "ILS", "2026-01-02"), Decimal("4")
        )

    def test_same_day_inverse_document_fx_beats_direct_market_quote(self):
        con = self.open_database()
        con.executemany(
            """INSERT INTO fx_rates(
                   date,base_currency,quote_currency,rate,source
               ) VALUES(?,?,?,?,?)""",
            [
                ("2026-01-02", "USD", "ILS", 3, "yahoo"),
                ("2026-01-02", "ILS", "USD", 0.25, "document"),
            ],
        )

        self.assertEqual(
            FxBook(con).rate("USD", "ILS", "2026-01-02"), Decimal("4")
        )

    def test_conflicting_same_day_document_fx_orientations_are_rejected(self):
        con = self.open_database()
        con.executemany(
            """INSERT INTO fx_rates(
                   date,base_currency,quote_currency,rate,source
               ) VALUES(?,?,?,?,?)""",
            [
                ("2026-01-02", "USD", "ILS", 3, "document"),
                ("2026-01-02", "ILS", "USD", 0.25, "document"),
            ],
        )

        with self.assertRaisesRegex(AccountingError, "conflicting authoritative"):
            FxBook(con).rate("USD", "ILS", "2026-01-02")


if __name__ == "__main__":
    unittest.main()
