"""Read legacy and normalized economic events without double counting evidence."""

from __future__ import annotations

from typing import Iterator


class EventReader:
    """Mixin for the event-query portion of the accounting engine."""

    def _legacy_transactions(self, account_id: int, as_of: str) -> Iterator[dict]:
        evidence_filters: list[str] = []
        if "canonical_event_id" in self._transaction_columns:
            if "status" in self._canonical_columns:
                evidence_filters.append(
                    """(t.canonical_event_id IS NULL OR NOT EXISTS (
                           SELECT 1 FROM canonical_events direct
                            WHERE direct.id=t.canonical_event_id
                              AND direct.status IN ('confirmed','excluded')
                       ))"""
                )
            else:
                evidence_filters.append("t.canonical_event_id IS NULL")
        if "source_event_id" in self._transaction_columns and self._has_event_evidence:
            if "status" in self._canonical_columns:
                evidence_filters.append(
                    """NOT EXISTS (
                           SELECT 1
                             FROM event_evidence ee
                             JOIN canonical_events mapped
                               ON mapped.id=ee.canonical_event_id
                            WHERE ee.source_event_id=t.source_event_id
                              AND (
                                  mapped.status IN ('confirmed','excluded')
                                  OR ee.evidence_role!='primary'
                              )
                       )"""
                )
            else:
                evidence_filters.append(
                    "NOT EXISTS (SELECT 1 FROM event_evidence ee "
                    "WHERE ee.source_event_id=t.source_event_id)"
                )
        canonical_filter = (
            "AND " + " AND ".join(evidence_filters) if evidence_filters else ""
        )
        flow_expr = "t.flow_type" if "flow_type" in self._transaction_columns else "NULL"
        source_doc_expr = (
            "t.source_doc_id"
            if "source_doc_id" in self._transaction_columns
            else "NULL"
        )
        rows = self.con.execute(
            f"""SELECT t.id, t.date, t.amount_minor, t.currency, t.category, t.component,
                       {flow_expr} AS flow_type, description, counterparty,
                       {source_doc_expr} AS source_doc_id,
                       0 AS is_canonical,
                       NULL AS linked_trade_min_date,
                       NULL AS linked_trade_max_date
                  FROM transactions t
                 WHERE t.account_id=? AND t.date<=? {canonical_filter}
                 ORDER BY t.date, t.id""",
            (account_id, as_of),
        )
        for row in rows:
            yield dict(row)

    def _canonical_transactions(self, account_id: int, as_of: str) -> Iterator[dict]:
        if not self._has_canonical or not {
            "event_type",
            "account_id",
            "occurred_on",
            "amount_minor",
            "currency",
        }.issubset(self._canonical_columns):
            return
        category_expr = "category" if "category" in self._canonical_columns else "NULL"
        component_expr = "component" if "component" in self._canonical_columns else "NULL"
        flow_expr = "flow_type" if "flow_type" in self._canonical_columns else "NULL"
        status_filter = "AND status='confirmed'" if "status" in self._canonical_columns else ""
        linked_trade_min_expr = "NULL"
        linked_trade_max_expr = "NULL"
        if self._has_trade_cash_links:
            linked_trade_min_expr = """(
                SELECT MIN(trade.occurred_on)
                  FROM trade_cash_links link
                  JOIN canonical_events trade ON trade.id=link.trade_event_id
                 WHERE link.cash_event_id=canonical_events.id
            )"""
            linked_trade_max_expr = """(
                SELECT MAX(trade.occurred_on)
                  FROM trade_cash_links link
                  JOIN canonical_events trade ON trade.id=link.trade_event_id
                 WHERE link.cash_event_id=canonical_events.id
            )"""
        if "source_event_id" in self._transaction_columns and self._has_event_evidence:
            description_expr = """(
                SELECT t.description FROM event_evidence ee
                JOIN transactions t ON t.source_event_id=ee.source_event_id
                WHERE ee.canonical_event_id=canonical_events.id
                  AND t.description IS NOT NULL AND trim(t.description)!=''
                ORDER BY CASE ee.evidence_role WHEN 'primary' THEN 0 ELSE 1 END, t.id
                LIMIT 1
            )"""
            counterparty_expr = """(
                SELECT t.counterparty FROM event_evidence ee
                JOIN transactions t ON t.source_event_id=ee.source_event_id
                WHERE ee.canonical_event_id=canonical_events.id
                  AND t.counterparty IS NOT NULL AND trim(t.counterparty)!=''
                ORDER BY CASE ee.evidence_role WHEN 'primary' THEN 0 ELSE 1 END, t.id
                LIMIT 1
            )"""
        else:
            description_expr = counterparty_expr = "NULL"
        source_doc_expr = "NULL"
        if self._has_event_evidence:
            source_doc_expr = """(
                SELECT source.source_doc_id
                  FROM event_evidence ee
                  JOIN source_events source ON source.id=ee.source_event_id
                 WHERE ee.canonical_event_id=canonical_events.id
                   AND source.source_doc_id IS NOT NULL
                 ORDER BY CASE ee.evidence_role WHEN 'primary' THEN 0 ELSE 1 END,
                          source.id
                 LIMIT 1
            )"""
        rows = self.con.execute(
            f"""SELECT id, occurred_on AS date, amount_minor, currency,
                       {category_expr} AS category, {component_expr} AS component,
                       {flow_expr} AS flow_type, {description_expr} AS description,
                       {counterparty_expr} AS counterparty,
                       {source_doc_expr} AS source_doc_id,
                       1 AS is_canonical,
                       {linked_trade_min_expr} AS linked_trade_min_date,
                       {linked_trade_max_expr} AS linked_trade_max_date
                  FROM canonical_events
                 WHERE account_id=? AND event_type='cash' AND occurred_on<=? {status_filter}
                 ORDER BY occurred_on, id""",
            (account_id, as_of),
        )
        for row in rows:
            yield dict(row)

    def transactions(self, account_id: int, as_of: str) -> list[dict]:
        rows = list(self._legacy_transactions(account_id, as_of))
        rows.extend(self._canonical_transactions(account_id, as_of) or ())
        opened_on = self._account_history_start(account_id)
        if opened_on:
            rows = [row for row in rows if str(row["date"]) >= opened_on]
        rows.sort(key=lambda row: (row["date"], row["id"]))
        return rows

    def _legacy_trades(self, account_id: int, as_of: str) -> Iterator[dict]:
        evidence_filters: list[str] = []
        if "canonical_event_id" in self._trade_columns:
            if "status" in self._canonical_columns:
                evidence_filters.append(
                    """(t.canonical_event_id IS NULL OR NOT EXISTS (
                           SELECT 1 FROM canonical_events direct
                            WHERE direct.id=t.canonical_event_id
                              AND direct.status IN ('confirmed','excluded')
                       ))"""
                )
            else:
                evidence_filters.append("t.canonical_event_id IS NULL")
        if "source_event_id" in self._trade_columns and self._has_event_evidence:
            if "status" in self._canonical_columns:
                evidence_filters.append(
                    """NOT EXISTS (
                           SELECT 1
                             FROM event_evidence ee
                             JOIN canonical_events mapped
                               ON mapped.id=ee.canonical_event_id
                            WHERE ee.source_event_id=t.source_event_id
                              AND (
                                  mapped.status IN ('confirmed','excluded')
                                  OR ee.evidence_role!='primary'
                              )
                       )"""
                )
            else:
                evidence_filters.append(
                    "NOT EXISTS (SELECT 1 FROM event_evidence ee "
                    "WHERE ee.source_event_id=t.source_event_id)"
                )
        canonical_filter = (
            "AND " + " AND ".join(evidence_filters) if evidence_filters else ""
        )
        rows = self.con.execute(
            f"""SELECT t.id, t.date, t.security_id, t.side, t.shares,
                       t.price_minor, t.fees_minor, t.currency,
                       s.ticker, s.currency AS security_currency,
                       0 AS is_canonical, 0 AS cash_linked
                  FROM trades t JOIN securities s ON s.id=t.security_id
                 WHERE t.account_id=? AND t.date<=? {canonical_filter}
                 ORDER BY t.date, t.id""",
            (account_id, as_of),
        )
        for row in rows:
            yield dict(row)

    def _canonical_trades(self, account_id: int, as_of: str) -> Iterator[dict]:
        required = {
            "event_type",
            "account_id",
            "occurred_on",
            "security_id",
            "side",
            "quantity",
            "price_minor",
            "fees_minor",
            "currency",
        }
        if not self._has_canonical or not required.issubset(self._canonical_columns):
            return
        status_filter = "AND e.status='confirmed'" if "status" in self._canonical_columns else ""
        cash_linked_expr = "0"
        cash_linked_parameters: tuple[str, ...] = ()
        if self._has_trade_cash_links:
            # A link becomes effective only when its posted cash event belongs to
            # the requested history. Future settlement evidence must never rewrite
            # an earlier account state.
            cash_linked_expr = """EXISTS (
                SELECT 1 FROM trade_cash_links l
                JOIN canonical_events cash ON cash.id=l.cash_event_id
                WHERE l.trade_event_id=e.id AND cash.occurred_on<=?
            )"""
            cash_linked_parameters = (as_of,)
        rows = self.con.execute(
            f"""SELECT e.id, e.occurred_on AS date, e.security_id, e.side,
                      e.quantity AS shares, e.price_minor, e.fees_minor,
                      e.currency, s.ticker, s.currency AS security_currency,
                      1 AS is_canonical, {cash_linked_expr} AS cash_linked
                 FROM canonical_events e
                 JOIN securities s ON s.id=e.security_id
                WHERE e.account_id=? AND e.event_type='trade' AND e.occurred_on<=? {status_filter}
                ORDER BY e.occurred_on, e.id""",
            (*cash_linked_parameters, account_id, as_of),
        )
        for row in rows:
            yield dict(row)

    def trades(self, account_id: int, as_of: str) -> list[dict]:
        rows = list(self._legacy_trades(account_id, as_of))
        rows.extend(self._canonical_trades(account_id, as_of) or ())
        opened_on = self._account_history_start(account_id)
        if opened_on:
            rows = [row for row in rows if str(row["date"]) >= opened_on]
        rows.sort(key=lambda row: (row["date"], row["id"]))
        return rows
