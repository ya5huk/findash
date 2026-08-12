from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from findash_core.reconciliation import (  # noqa: E402
    ReconciliationError,
    reconcile_logical_accounts,
)


class LogicalAccountReconciliationTests(unittest.TestCase):
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
    def payload(
        source: int,
        target: int,
        *,
        events: list[dict] | None = None,
        balances: list[dict] | None = None,
        positions: list[dict] | None = None,
    ) -> dict:
        return {
            "operation": "merge_duplicate_logical_account",
            "source_account_id": source,
            "target_account_id": target,
            "confirmed_same_economic_account": True,
            "confirmed_reviewed_resolutions": True,
            "confirmed_unmapped_events_are_distinct": True,
            "event_resolutions": events or [],
            "balance_resolutions": balances or [],
            "position_resolutions": positions or [],
        }

    @staticmethod
    def add_account(connection: sqlite3.Connection, name: str) -> int:
        return int(
            connection.execute(
                """INSERT INTO accounts(name,kind,institution,currency)
                   VALUES (?,'brokerage','Broker','USD') RETURNING id""",
                (name,),
            ).fetchone()[0]
        )

    @staticmethod
    def add_feed(
        connection: sqlite3.Connection,
        account_id: int,
        provider: str,
        reference: str,
    ) -> int:
        return int(
            connection.execute(
                """INSERT INTO account_feeds
                   (account_id,provider,feed_type,external_account_ref)
                   VALUES (?,?,'api',?) RETURNING id""",
                (account_id, provider, reference),
            ).fetchone()[0]
        )

    @staticmethod
    def add_source_event(
        connection: sqlite3.Connection,
        feed_id: int,
        source_key: str,
        event_type: str,
    ) -> int:
        return int(
            connection.execute(
                """INSERT INTO source_events
                   (account_feed_id,source_key,event_type,occurred_on)
                   VALUES (?,?,?,'2026-01-01') RETURNING id""",
                (feed_id, source_key, event_type),
            ).fetchone()[0]
        )

    @staticmethod
    def add_trade_event(
        connection: sqlite3.Connection, account_id: int, security_id: int
    ) -> int:
        return int(
            connection.execute(
                """INSERT INTO canonical_events
                   (account_id,event_type,occurred_on,currency,security_id,
                    side,quantity,price_minor,fees_minor)
                   VALUES (?,'trade','2026-01-01','USD',?,'buy',1,100,0)
                   RETURNING id""",
                (account_id, security_id),
            ).fetchone()[0]
        )

    @staticmethod
    def add_event_evidence(
        connection: sqlite3.Connection, canonical_id: int, source_id: int
    ) -> None:
        connection.execute(
            """INSERT INTO event_evidence
               (canonical_event_id,source_event_id,evidence_role,match_method)
               VALUES (?,?,'primary','reviewed-source')""",
            (canonical_id, source_id),
        )

    def test_whole_graph_moves_atomically_and_preserves_evidence(self) -> None:
        connection = sqlite3.connect(self.db)
        target = self.add_account(connection, "Target")
        source = self.add_account(connection, "Source")
        feed_one = self.add_feed(connection, source, "provider-one", "one")
        feed_two = self.add_feed(connection, source, "provider-two", "two")
        security = int(
            connection.execute(
                "INSERT INTO securities(ticker,currency) VALUES ('SYN','USD') RETURNING id"
            ).fetchone()[0]
        )

        trade_events: list[tuple[int, int]] = []
        for feed in (feed_one, feed_two):
            source_event = self.add_source_event(
                connection, feed, "provider-local-trade", "trade"
            )
            canonical = self.add_trade_event(connection, source, security)
            connection.execute(
                """INSERT INTO trades
                   (date,account_id,security_id,side,shares,price_minor,fees_minor,
                    currency,external_id,source_event_id,canonical_event_id)
                   VALUES ('2026-01-01',?,?,'buy',1,100,0,'USD',
                           'provider-local-trade',?,?)""",
                (source, security, source_event, canonical),
            )
            self.add_event_evidence(connection, canonical, source_event)
            trade_events.append((source_event, canonical))

        cash_source = self.add_source_event(connection, feed_one, "cash", "cash")
        cash_canonical = int(
            connection.execute(
                """INSERT INTO canonical_events
                   (account_id,event_type,occurred_on,currency,
                    amount_minor,flow_type)
                   VALUES (?,'cash','2026-01-01','USD',-100,'investment')
                   RETURNING id""",
                (source,),
            ).fetchone()[0]
        )
        connection.execute(
            """INSERT INTO transactions
               (account_id,date,amount_minor,currency,flow_type,
                source_event_id,canonical_event_id)
               VALUES (?,'2026-01-01',-100,'USD','investment',?,?)""",
            (source, cash_source, cash_canonical),
        )
        self.add_event_evidence(connection, cash_canonical, cash_source)
        connection.execute(
            """INSERT INTO trade_cash_links
               (cash_event_id,trade_event_id,match_method)
               VALUES (?,?,'reviewed-posting')""",
            (cash_canonical, trade_events[0][1]),
        )

        balance = int(
            connection.execute(
                """INSERT INTO balances(account_id,as_of,amount_minor,currency)
                   VALUES (?,'2026-01-02',100,'USD') RETURNING id""",
                (source,),
            ).fetchone()[0]
        )
        balance_evidence = int(
            connection.execute(
                """INSERT INTO balance_evidence
                   (canonical_balance_id,account_feed_id,source_key,
                    observed_amount_minor,observed_currency)
                   VALUES (?,?,'balance',100,'USD') RETURNING id""",
                (balance, feed_one),
            ).fetchone()[0]
        )
        position = int(
            connection.execute(
                """INSERT INTO positions
                   (account_id,security_id,as_of,quantity,currency)
                   VALUES (?,?,'2026-01-02',1,'USD') RETURNING id""",
                (source, security),
            ).fetchone()[0]
        )
        position_evidence = int(
            connection.execute(
                """INSERT INTO position_evidence
                   (canonical_position_id,account_feed_id,source_key,
                    observed_quantity,observed_currency)
                   VALUES (?,?,'position',1,'USD') RETURNING id""",
                (position, feed_one),
            ).fetchone()[0]
        )
        source_rows_before = connection.execute(
            "SELECT * FROM source_events ORDER BY id"
        ).fetchall()
        event_evidence_before = connection.execute(
            "SELECT * FROM event_evidence ORDER BY canonical_event_id,source_event_id"
        ).fetchall()
        trade_cash_before = connection.execute(
            "SELECT * FROM trade_cash_links"
        ).fetchall()
        balance_evidence_before = connection.execute(
            "SELECT * FROM balance_evidence WHERE id=?", (balance_evidence,)
        ).fetchone()
        position_evidence_before = connection.execute(
            "SELECT * FROM position_evidence WHERE id=?", (position_evidence,)
        ).fetchone()
        connection.commit()
        connection.close()

        counts = reconcile_logical_accounts(
            self.db, self.payload(source, target), close_date="2026-07-21"
        )
        self.assertEqual(counts["source_events"], 3)
        self.assertEqual(counts["event_evidence"], 3)
        self.assertNotIn("source_account_id", counts)
        self.assertNotIn("target_account_id", counts)

        check = sqlite3.connect(self.db)
        self.addCleanup(check.close)
        for table in (
            "account_feeds",
            "balances",
            "transactions",
            "trades",
            "canonical_events",
            "positions",
        ):
            self.assertEqual(
                check.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE account_id=?", (source,)
                ).fetchone()[0],
                0,
            )
        self.assertEqual(
            check.execute(
                "SELECT closed_on FROM accounts WHERE id=?", (source,)
            ).fetchone()[0],
            "2026-07-21",
        )
        self.assertEqual(
            check.execute("SELECT * FROM source_events ORDER BY id").fetchall(),
            source_rows_before,
        )
        self.assertEqual(
            check.execute(
                "SELECT * FROM event_evidence ORDER BY canonical_event_id,source_event_id"
            ).fetchall(),
            event_evidence_before,
        )
        self.assertEqual(
            check.execute("SELECT * FROM trade_cash_links").fetchall(),
            trade_cash_before,
        )
        self.assertEqual(
            check.execute(
                "SELECT * FROM balance_evidence WHERE id=?", (balance_evidence,)
            ).fetchone(),
            balance_evidence_before,
        )
        self.assertEqual(
            check.execute(
                "SELECT * FROM position_evidence WHERE id=?", (position_evidence,)
            ).fetchone(),
            position_evidence_before,
        )
        self.assertEqual(
            check.execute(
                "SELECT COUNT(DISTINCT external_id) FROM trades WHERE account_id=?",
                (target,),
            ).fetchone()[0],
            1,
        )
        self.assertEqual(check.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_reviewed_event_and_snapshot_collisions_are_resolved(self) -> None:
        connection = sqlite3.connect(self.db)
        target = self.add_account(connection, "Target")
        source = self.add_account(connection, "Source")
        target_feed = self.add_feed(connection, target, "wrapper", "wrapper")
        source_feed = self.add_feed(connection, source, "ibkr", "ibkr")
        security = int(
            connection.execute(
                "INSERT INTO securities(ticker,currency) VALUES ('SYN','USD') RETURNING id"
            ).fetchone()[0]
        )

        target_events: list[int] = []
        for index in range(2):
            target_source = self.add_source_event(
                connection, target_feed, f"target-{index}", "trade"
            )
            target_event = self.add_trade_event(connection, target, security)
            self.add_event_evidence(connection, target_event, target_source)
            target_events.append(target_event)
        source_event = self.add_source_event(
            connection, source_feed, "aggregate", "trade"
        )
        obsolete_event = self.add_trade_event(connection, source, security)
        self.add_event_evidence(connection, obsolete_event, source_event)
        raw_trade = int(
            connection.execute(
                """INSERT INTO trades
                   (date,account_id,security_id,side,shares,price_minor,fees_minor,
                    currency,source_event_id,canonical_event_id)
                   VALUES ('2026-01-01',?,?,'buy',1,100,0,'USD',?,?) RETURNING id""",
                (source, security, source_event, obsolete_event),
            ).fetchone()[0]
        )

        balances: list[int] = []
        positions: list[int] = []
        balance_evidence: list[int] = []
        position_evidence: list[int] = []
        for account, feed, suffix in (
            (target, target_feed, "target"),
            (source, source_feed, "source"),
        ):
            balance = int(
                connection.execute(
                    """INSERT INTO balances(account_id,as_of,amount_minor,currency)
                       VALUES (?,'2026-01-02',100,'USD') RETURNING id""",
                    (account,),
                ).fetchone()[0]
            )
            balance_evidence.append(
                int(
                    connection.execute(
                        """INSERT INTO balance_evidence
                           (canonical_balance_id,account_feed_id,source_key,
                            observed_amount_minor,observed_currency,
                            account_snapshot_complete)
                           VALUES (?,?,?,100,'USD',?) RETURNING id""",
                        (
                            balance,
                            feed,
                            f"balance-{suffix}",
                            1 if suffix == "source" else 0,
                        ),
                    ).fetchone()[0]
                )
            )
            position = int(
                connection.execute(
                    """INSERT INTO positions
                       (account_id,security_id,as_of,quantity,currency)
                       VALUES (?,?,'2026-01-02',1,'USD') RETURNING id""",
                    (account, security),
                ).fetchone()[0]
            )
            position_evidence.append(
                int(
                    connection.execute(
                        """INSERT INTO position_evidence
                           (canonical_position_id,account_feed_id,source_key,
                            observed_quantity,observed_currency,
                            account_snapshot_complete)
                           VALUES (?,?,?,1,'USD',?) RETURNING id""",
                        (
                            position,
                            feed,
                            f"position-{suffix}",
                            1 if suffix == "source" else 0,
                        ),
                    ).fetchone()[0]
                )
            )
            balances.append(balance)
            positions.append(position)
        connection.commit()
        connection.close()

        payload = self.payload(
            source,
            target,
            events=[
                {
                    "obsolete_canonical_event_id": obsolete_event,
                    "target_canonical_event_ids": target_events,
                }
            ],
            balances=[
                {
                    "source_balance_id": balances[1],
                    "target_balance_id": balances[0],
                }
            ],
            positions=[
                {
                    "source_position_id": positions[1],
                    "target_position_id": positions[0],
                }
            ],
        )
        counts = reconcile_logical_accounts(
            self.db, payload, close_date="2026-07-21"
        )
        self.assertEqual(counts["event_resolutions"], 1)
        self.assertEqual(counts["balance_resolutions"], 1)
        self.assertEqual(counts["position_resolutions"], 1)

        check = sqlite3.connect(self.db)
        self.addCleanup(check.close)
        self.assertEqual(
            check.execute(
                "SELECT account_id,status FROM canonical_events WHERE id=?",
                (obsolete_event,),
            ).fetchone(),
            (target, "excluded"),
        )
        self.assertEqual(
            check.execute(
                "SELECT account_id,source_event_id,canonical_event_id FROM trades WHERE id=?",
                (raw_trade,),
            ).fetchone(),
            (target, source_event, None),
        )
        self.assertEqual(
            check.execute(
                """SELECT canonical_event_id,evidence_role FROM event_evidence
                   WHERE source_event_id=? ORDER BY canonical_event_id""",
                (source_event,),
            ).fetchall(),
            [(target_events[0], "aggregate"), (target_events[1], "aggregate")],
        )
        self.assertEqual(
            check.execute(
                "SELECT COUNT(*) FROM balances WHERE id=?", (balances[1],)
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            check.execute(
                """SELECT canonical_balance_id,account_snapshot_complete
                   FROM balance_evidence WHERE id=?""",
                (balance_evidence[1],),
            ).fetchone(),
            (balances[0], 1),
        )
        self.assertEqual(
            check.execute(
                "SELECT COUNT(*) FROM positions WHERE id=?", (positions[1],)
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            check.execute(
                """SELECT canonical_position_id,account_snapshot_complete
                   FROM position_evidence WHERE id=?""",
                (position_evidence[1],),
            ).fetchone(),
            (positions[0], 1),
        )

    def test_ambiguous_conflict_rolls_back_without_exposing_values(self) -> None:
        connection = sqlite3.connect(self.db)
        target = self.add_account(connection, "private-target-name")
        source = self.add_account(connection, "private-source-name")
        connection.execute(
            """INSERT INTO account_feeds(account_id,provider,feed_type)
               VALUES (?,'private-provider','manual')""",
            (target,),
        )
        connection.execute(
            """INSERT INTO account_feeds(account_id,provider,feed_type)
               VALUES (?,'private-provider','manual')""",
            (source,),
        )
        connection.commit()
        connection.close()

        with self.assertRaises(ReconciliationError) as caught:
            reconcile_logical_accounts(
                self.db,
                self.payload(source, target),
                close_date="2026-07-21",
            )
        message = str(caught.exception)
        self.assertIn("account_feeds=1", message)
        self.assertNotIn("private-provider", message)
        self.assertNotIn("private-source-name", message)

        check = sqlite3.connect(self.db)
        self.addCleanup(check.close)
        self.assertIsNone(
            check.execute(
                "SELECT closed_on FROM accounts WHERE id=?", (source,)
            ).fetchone()[0]
        )
        self.assertEqual(
            check.execute(
                "SELECT COUNT(*) FROM account_feeds WHERE account_id=?", (source,)
            ).fetchone()[0],
            1,
        )

    def test_request_requires_closed_shape_and_all_confirmations(self) -> None:
        payload = self.payload(2, 1)
        payload["confirmed_reviewed_resolutions"] = False
        with self.assertRaisesRegex(ReconciliationError, "confirmations"):
            reconcile_logical_accounts(self.db, payload, close_date="2026-07-21")

        payload = self.payload(2, 1)
        payload["sql"] = "DELETE FROM accounts"
        with self.assertRaisesRegex(ReconciliationError, "invalid shape"):
            reconcile_logical_accounts(self.db, payload, close_date="2026-07-21")

    def test_merge_preserves_earliest_account_coverage(self) -> None:
        connection = sqlite3.connect(self.db)
        target = self.add_account(connection, "Target")
        source = self.add_account(connection, "Source")
        connection.execute(
            "UPDATE accounts SET opened_on='2025-01-01' WHERE id=?", (target,)
        )
        connection.execute(
            "UPDATE accounts SET opened_on='2020-01-01' WHERE id=?", (source,)
        )
        connection.commit()
        connection.close()

        reconcile_logical_accounts(
            self.db, self.payload(source, target), close_date="2026-07-21"
        )

        check = sqlite3.connect(self.db)
        self.addCleanup(check.close)
        self.assertEqual(
            check.execute(
                "SELECT opened_on FROM accounts WHERE id=?", (target,)
            ).fetchone()[0],
            "2020-01-01",
        )


if __name__ == "__main__":
    unittest.main()
