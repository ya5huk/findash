from __future__ import annotations

from pathlib import Path
import sqlite3
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
SCHEMA = (ROOT / "scripts" / "init-db.sql").read_text(encoding="utf-8")

from findash_core.accounting import AccountingEngine  # noqa: E402


class EventReaderReviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        self.addCleanup(self.connection.close)
        self.connection.executescript(SCHEMA)
        self.account = self.connection.execute(
            """INSERT INTO accounts(name,kind,institution,currency)
               VALUES('Cash','checking','Bank','ILS') RETURNING id"""
        ).fetchone()[0]
        feed = self.connection.execute(
            """INSERT INTO account_feeds(account_id,provider,feed_type)
               VALUES(?,'test','document') RETURNING id""",
            (self.account,),
        ).fetchone()[0]
        source = self.connection.execute(
            """INSERT INTO source_events(
                   account_feed_id,source_key,event_type,occurred_on
               ) VALUES(?,'row-1','cash','2026-01-02') RETURNING id""",
            (feed,),
        ).fetchone()[0]
        self.canonical = self.connection.execute(
            """INSERT INTO canonical_events(
                   account_id,event_type,occurred_on,currency,amount_minor,
                   flow_type,category,status
               ) VALUES(?,'cash','2026-01-02','ILS',-1000,
                        'expense','groceries','needs_review')
               RETURNING id""",
            (self.account,),
        ).fetchone()[0]
        self.connection.execute(
            """INSERT INTO transactions(
                   account_id,date,amount_minor,currency,category,flow_type,
                   source_event_id,canonical_event_id
               ) VALUES(?,'2026-01-02',-1000,'ILS','groceries','expense',?,?)""",
            (self.account, source, self.canonical),
        )
        self.connection.execute(
            """INSERT INTO event_evidence(
                   canonical_event_id,source_event_id,evidence_role,match_method
               ) VALUES(?,?,'primary','pending-review')""",
            (self.canonical, source),
        )

    def test_primary_raw_event_remains_visible_until_canonical_is_confirmed(self):
        engine = AccountingEngine(self.connection)

        pending = engine.transactions(self.account, "2026-01-31")
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["is_canonical"], 0)

        self.connection.execute(
            "UPDATE canonical_events SET status='confirmed' WHERE id=?",
            (self.canonical,),
        )
        confirmed = AccountingEngine(self.connection).transactions(
            self.account, "2026-01-31"
        )
        self.assertEqual(len(confirmed), 1)
        self.assertEqual(confirmed[0]["is_canonical"], 1)

    def test_excluded_canonical_suppresses_its_raw_observation(self):
        self.connection.execute(
            "UPDATE canonical_events SET status='excluded' WHERE id=?",
            (self.canonical,),
        )

        rows = AccountingEngine(self.connection).transactions(
            self.account, "2026-01-31"
        )

        self.assertEqual(rows, [])


if __name__ == "__main__":
    unittest.main()
