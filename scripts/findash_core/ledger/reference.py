"""Reference-data intents: documents, accounts, feeds, snapshots, payslips, prices.

Same contract as money intents: Claude states what a source shows, the engine
writes the rows, a re-run is a no-op, and a contradiction is rejected instead of
silently overwriting what an earlier source said.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
import re
import sqlite3
from typing import Any, Optional

from findash_core.accounting import AccountingEngine
from findash_core.currencies import minor_unit_factor

from .values import (
    IntentError,
    currency,
    decimal,
    iso_date,
    minor,
    optional_date,
    require_shape,
    text,
)


FEED_TYPES = frozenset({"document", "api", "manual"})
PAYSLIP_AMOUNTS = (
    "income_tax",
    "social_security",
    "health_insurance",
    "pension_employee",
    "pension_employer",
    "study_fund_employee",
    "study_fund_employer",
)
LINE_ITEM_KINDS = frozenset({"earning", "deduction", "info"})
_META_KEY = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def _flag(payload: dict, key: str, field: str) -> bool:
    value = payload.get(key, False)
    if not isinstance(value, bool):
        raise IntentError(f"{field} must be true or false")
    return value


def _per_unit_minor(value: Any, iso_currency: str, field: str) -> Optional[int]:
    """Per-share prices may carry more precision than money; round to minor units."""

    if value is None:
        return None
    amount = decimal(value, field) * minor_unit_factor(iso_currency)
    result = int(amount.quantize(Decimal(1), rounding=ROUND_HALF_UP))
    if result < 0:
        raise IntentError(f"{field} must not be negative")
    return result


class ReferenceIntents:
    """Mixin for :class:`LedgerWriter`; relies on its lookups and ``self.con``."""

    con: sqlite3.Connection

    def reference_handlers(self) -> dict:
        return {
            "document": self._document,
            "account": self._account_intent,
            "feed": self._feed_intent,
            "security": self._security_intent,
            "price": self._price,
            "meta": self._meta,
            "balance": self._balance,
            "position": self._position,
            "snapshot": self._snapshot,
            "payslip": self._payslip,
        }

    # ------------------------------------------------------------- documents

    def _document(self, payload: dict, label: str) -> str:
        require_shape(
            payload,
            label,
            required={"drive_id", "drive_path", "filename"},
            optional={"doc_type", "doc_date", "raw_hash", "notes"},
        )
        values = {
            "drive_id": text(payload["drive_id"], f"{label}.drive_id", optional=False),
            "drive_path": text(payload["drive_path"], f"{label}.drive_path", optional=False),
            "filename": text(payload["filename"], f"{label}.filename", optional=False),
            "doc_type": text(payload.get("doc_type"), f"{label}.doc_type"),
            "doc_date": optional_date(payload, "doc_date", f"{label}.doc_date"),
            "raw_hash": text(payload.get("raw_hash"), f"{label}.raw_hash"),
            "notes": text(payload.get("notes"), f"{label}.notes"),
        }
        row = self.con.execute(
            "SELECT * FROM documents WHERE drive_id=?", (values["drive_id"],)
        ).fetchone()
        if row is None:
            self.con.execute(
                """INSERT INTO documents(
                       drive_id,drive_path,filename,doc_type,doc_date,raw_hash,notes
                   ) VALUES (:drive_id,:drive_path,:filename,:doc_type,:doc_date,
                             :raw_hash,:notes)""",
                values,
            )
            return "created"
        if values["raw_hash"] and row["raw_hash"] and values["raw_hash"] != row["raw_hash"]:
            raise IntentError(f"{label}: a document with this drive_id has different bytes")
        # Filing location and descriptive fields may be refined on a later run.
        changes = {
            key: values[key]
            for key in ("drive_path", "filename", "doc_type", "doc_date", "raw_hash", "notes")
            if values[key] is not None and values[key] != row[key]
        }
        if not changes:
            return "unchanged"
        assignments = ",".join(f"{key}=:{key}" for key in changes)
        self.con.execute(
            f"UPDATE documents SET {assignments} WHERE id=:id", {**changes, "id": row["id"]}
        )
        return "updated"

    # ------------------------------------------------- accounts and feeds

    def _account_intent(self, payload: dict, label: str) -> str:
        require_shape(
            payload,
            label,
            required={"name", "kind", "institution", "currency"},
            optional={
                "external_id",
                "opened_on",
                "closed_on",
                "investment_track",
                "liquidity_date",
                "notes",
            },
        )
        values = {
            "name": text(payload["name"], f"{label}.name", optional=False),
            "kind": text(payload["kind"], f"{label}.kind", optional=False),
            "institution": text(payload["institution"], f"{label}.institution", optional=False),
            "currency": currency(payload["currency"], f"{label}.currency"),
            "external_id": text(payload.get("external_id"), f"{label}.external_id"),
            "opened_on": optional_date(payload, "opened_on", f"{label}.opened_on"),
            "closed_on": optional_date(payload, "closed_on", f"{label}.closed_on"),
            "investment_track": text(payload.get("investment_track"), f"{label}.investment_track"),
            "liquidity_date": optional_date(payload, "liquidity_date", f"{label}.liquidity_date"),
            "notes": text(payload.get("notes"), f"{label}.notes"),
        }
        row = self.con.execute(
            "SELECT * FROM accounts WHERE name=?", (values["name"],)
        ).fetchone()
        if row is None:
            self.con.execute(
                """INSERT INTO accounts(
                       name,kind,institution,currency,external_id,opened_on,closed_on,
                       investment_track,liquidity_date,notes
                   ) VALUES (:name,:kind,:institution,:currency,:external_id,:opened_on,
                             :closed_on,:investment_track,:liquidity_date,:notes)""",
                values,
            )
            return "created"
        for key in ("kind", "institution", "currency"):
            if values[key] != row[key]:
                raise IntentError(f"{label}: {key} of an existing account cannot change")
        changes = {
            key: values[key]
            for key in (
                "external_id",
                "opened_on",
                "closed_on",
                "investment_track",
                "liquidity_date",
                "notes",
            )
            if values[key] is not None and values[key] != row[key]
        }
        if not changes:
            return "unchanged"
        assignments = ",".join(f"{key}=:{key}" for key in changes)
        self.con.execute(
            f"UPDATE accounts SET {assignments} WHERE id=:id", {**changes, "id": row["id"]}
        )
        return "updated"

    def _feed_intent(self, payload: dict, label: str) -> str:
        require_shape(
            payload,
            label,
            required={"account", "provider", "feed_type"},
            optional={"external_account_ref", "label", "active"},
        )
        account = self._account(payload["account"], f"{label}.account")
        provider = text(payload["provider"], f"{label}.provider", optional=False)
        feed_type = payload["feed_type"]
        if feed_type not in FEED_TYPES:
            raise IntentError(f"{label}.feed_type must be document, api or manual")
        reference = text(payload.get("external_account_ref"), f"{label}.external_account_ref")
        feed_label = text(payload.get("label"), f"{label}.label")
        active = payload.get("active")
        if active is not None and not isinstance(active, bool):
            raise IntentError(f"{label}.active must be true or false")
        if reference is not None:
            row = self.con.execute(
                "SELECT * FROM account_feeds WHERE provider=? AND external_account_ref=?",
                (provider, reference),
            ).fetchone()
        else:
            row = self.con.execute(
                """SELECT * FROM account_feeds
                    WHERE account_id=? AND provider=? AND external_account_ref IS NULL""",
                (account["id"], provider),
            ).fetchone()
        if row is None:
            self.con.execute(
                """INSERT INTO account_feeds(
                       account_id,provider,feed_type,external_account_ref,label,is_active
                   ) VALUES (?,?,?,?,?,?)""",
                (
                    account["id"],
                    provider,
                    feed_type,
                    reference,
                    feed_label,
                    1 if active is None else int(active),
                ),
            )
            return "created"
        if row["account_id"] != account["id"] or row["feed_type"] != feed_type:
            raise IntentError(
                f"{label}: this provider account is already attached differently; "
                "use the reviewed reconcile-account flow"
            )
        changes = {}
        if feed_label is not None and feed_label != row["label"]:
            changes["label"] = feed_label
        if active is not None and int(active) != row["is_active"]:
            changes["is_active"] = int(active)
        if not changes:
            return "unchanged"
        assignments = ",".join(f"{key}=:{key}" for key in changes)
        self.con.execute(
            f"UPDATE account_feeds SET {assignments} WHERE id=:id",
            {**changes, "id": row["id"]},
        )
        return "updated"

    # ----------------------------------------------------- securities/prices

    def _security_intent(self, payload: dict, label: str) -> str:
        require_shape(
            payload, label, required={"ticker", "currency"}, optional={"name", "asset_class"}
        )
        ticker = text(payload["ticker"], f"{label}.ticker", optional=False)
        iso_currency = currency(payload["currency"], f"{label}.currency")
        name = text(payload.get("name"), f"{label}.name")
        asset_class = text(payload.get("asset_class"), f"{label}.asset_class")
        row = self.con.execute("SELECT * FROM securities WHERE ticker=?", (ticker,)).fetchone()
        if row is None:
            self.con.execute(
                "INSERT INTO securities(ticker,name,asset_class,currency) VALUES (?,?,?,?)",
                (ticker, name, asset_class, iso_currency),
            )
            return "created"
        if row["currency"] != iso_currency:
            raise IntentError(f"{label}: security is recorded in another currency")
        changes = {
            key: value
            for key, value in (("name", name), ("asset_class", asset_class))
            if value is not None and value != row[key]
        }
        if not changes:
            return "unchanged"
        assignments = ",".join(f"{key}=:{key}" for key in changes)
        self.con.execute(
            f"UPDATE securities SET {assignments} WHERE id=:id", {**changes, "id": row["id"]}
        )
        return "updated"

    def _price(self, payload: dict, label: str) -> str:
        require_shape(payload, label, required={"ticker", "date", "close", "currency"})
        iso_currency = currency(payload["currency"], f"{label}.currency")
        security_id = self._security(payload["ticker"], iso_currency, f"{label}.ticker")
        on_date = iso_date(payload["date"], f"{label}.date")
        close = _per_unit_minor(payload["close"], iso_currency, f"{label}.close")
        # A stored close is never overwritten; market refreshes own corrections.
        cursor = self.con.execute(
            """INSERT OR IGNORE INTO prices(security_id,date,close_minor,currency)
               VALUES (?,?,?,?)""",
            (security_id, on_date, close, iso_currency),
        )
        return "created" if cursor.rowcount else "unchanged"

    def _meta(self, payload: dict, label: str) -> str:
        require_shape(payload, label, required={"key"}, optional={"value", "now"})
        key = payload["key"]
        if not isinstance(key, str) or not _META_KEY.match(key):
            raise IntentError(f"{label}.key must be a lowercase snake_case name")
        if _flag(payload, "now", f"{label}.now") == ("value" in payload):
            raise IntentError(f"{label}: give exactly one of value or now")
        value = (
            self.con.execute("SELECT datetime('now')").fetchone()[0]
            if payload.get("now")
            else text(payload["value"], f"{label}.value", optional=False)
        )
        self.con.execute(
            """INSERT INTO meta(key,value) VALUES (?,?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
            (key, value),
        )
        return "updated"

    # ------------------------------------------------------------ snapshots

    def _snapshot_evidence(
        self, account: sqlite3.Row, spec: Any, field: str
    ) -> tuple[Optional[int], str, Optional[int]]:
        """Return ``(feed_id, source_key, document_id)`` for one observation."""

        if not isinstance(spec, dict):
            raise IntentError(f"{field} must be an object")
        if "key" in spec:
            require_shape(spec, field, required={"feed", "key"}, optional={"document"})
            provider = text(spec["feed"], f"{field}.feed", optional=False)
            feed_id = self._feed(account["id"], provider, f"{field}.feed", create=False)
            doc_id = (
                self._document_id(spec["document"], f"{field}.document")
                if spec.get("document") is not None
                else None
            )
            return feed_id, text(spec["key"], f"{field}.key", optional=False), doc_id
        require_shape(spec, field, required={"document"}, optional={"row", "feed"})
        doc_id = self._document_id(spec["document"], f"{field}.document")
        feed_id = (
            self._feed(
                account["id"],
                text(spec["feed"], f"{field}.feed", optional=False),
                f"{field}.feed",
                create=False,
            )
            if spec.get("feed") is not None
            else None
        )
        row = spec.get("row")
        if row is not None and (isinstance(row, bool) or not str(row).strip()):
            raise IntentError(f"{field}.row must be a stable row identifier")
        key = f"document:{doc_id}" + (f":{str(row).strip()}" if row is not None else "")
        return feed_id, key, doc_id

    def _record_balance(
        self,
        label: str,
        account: sqlite3.Row,
        as_of: str,
        component: Optional[str],
        amount: int,
        iso_currency: str,
        evidence: tuple[Optional[int], str, Optional[int]],
        *,
        complete: bool,
        prefer: bool,
    ) -> str:
        feed_id, key, doc_id = evidence
        seen = self.con.execute(
            """SELECT evidence.*,balance.account_id,balance.as_of,balance.component
                 FROM balance_evidence AS evidence
                 JOIN balances AS balance ON balance.id=evidence.canonical_balance_id
                WHERE COALESCE(evidence.account_feed_id,0)=? AND evidence.source_key=?""",
            (feed_id or 0, key),
        ).fetchone()
        if seen is not None:
            if (
                seen["account_id"] != account["id"]
                or seen["as_of"] != as_of
                or seen["component"] != component
                or seen["observed_amount_minor"] != amount
                or seen["observed_currency"] != iso_currency
            ):
                raise IntentError(f"{label}: evidence is already recorded with different facts")
            return "unchanged"
        anchor = self.con.execute(
            """SELECT * FROM balances
                WHERE account_id=? AND as_of=? AND COALESCE(component,'')=COALESCE(?,'')""",
            (account["id"], as_of, component),
        ).fetchone()
        if anchor is None:
            balance_id = int(
                self.con.execute(
                    """INSERT INTO balances(account_id,as_of,component,amount_minor,
                                            currency,source_doc_id)
                       VALUES (?,?,?,?,?,?) RETURNING id""",
                    (account["id"], as_of, component, amount, iso_currency, doc_id),
                ).fetchone()[0]
            )
            outcome = "created"
        elif anchor["amount_minor"] == amount and anchor["currency"] == iso_currency:
            balance_id, outcome = int(anchor["id"]), "linked"
        elif prefer:
            self.con.execute(
                """UPDATE balances
                      SET amount_minor=?,currency=?,source_doc_id=COALESCE(?,source_doc_id)
                    WHERE id=?""",
                (amount, iso_currency, doc_id, anchor["id"]),
            )
            balance_id, outcome = int(anchor["id"]), "replaced"
        else:
            raise IntentError(
                f"{label}: conflicts with the recorded balance; decide which source is "
                "right and set prefer on the one to keep"
            )
        self.con.execute(
            """INSERT INTO balance_evidence(
                   canonical_balance_id,account_feed_id,source_doc_id,source_key,
                   observed_amount_minor,observed_currency,account_snapshot_complete
               ) VALUES (?,?,?,?,?,?,?)""",
            (balance_id, feed_id, doc_id, key, amount, iso_currency, int(complete)),
        )
        return outcome

    def _record_position(
        self,
        label: str,
        account: sqlite3.Row,
        as_of: str,
        security_id: int,
        quantity: Decimal,
        iso_currency: str,
        observed: dict[str, Optional[int]],
        evidence: tuple[Optional[int], str, Optional[int]],
        *,
        complete: bool,
        prefer: bool,
    ) -> str:
        feed_id, key, doc_id = evidence
        seen = self.con.execute(
            """SELECT evidence.*,position.account_id,position.as_of,position.security_id
                 FROM position_evidence AS evidence
                 JOIN positions AS position ON position.id=evidence.canonical_position_id
                WHERE COALESCE(evidence.account_feed_id,0)=? AND evidence.source_key=?""",
            (feed_id or 0, key),
        ).fetchone()
        if seen is not None:
            if (
                seen["account_id"] != account["id"]
                or seen["as_of"] != as_of
                or seen["security_id"] != security_id
                or Decimal(str(seen["observed_quantity"])) != quantity
            ):
                raise IntentError(f"{label}: evidence is already recorded with different facts")
            return "unchanged"
        anchor = self.con.execute(
            "SELECT * FROM positions WHERE account_id=? AND security_id=? AND as_of=?",
            (account["id"], security_id, as_of),
        ).fetchone()
        values = (
            float(quantity),
            observed["avg_cost"],
            observed["price"],
            observed["value"],
            iso_currency,
        )
        if anchor is None:
            position_id = int(
                self.con.execute(
                    """INSERT INTO positions(
                           account_id,security_id,as_of,quantity,avg_cost_minor,
                           market_price_minor,market_value_minor,currency,source_doc_id
                       ) VALUES (?,?,?,?,?,?,?,?,?) RETURNING id""",
                    (account["id"], security_id, as_of, *values, doc_id),
                ).fetchone()[0]
            )
            outcome = "created"
        elif Decimal(str(anchor["quantity"])) == quantity:
            position_id, outcome = int(anchor["id"]), "linked"
        elif prefer:
            self.con.execute(
                """UPDATE positions
                      SET quantity=?,avg_cost_minor=?,market_price_minor=?,
                          market_value_minor=?,currency=?,
                          source_doc_id=COALESCE(?,source_doc_id)
                    WHERE id=?""",
                (*values, doc_id, anchor["id"]),
            )
            position_id, outcome = int(anchor["id"]), "replaced"
        else:
            raise IntentError(
                f"{label}: conflicts with the recorded position; decide which source is "
                "right and set prefer on the one to keep"
            )
        self.con.execute(
            """INSERT INTO position_evidence(
                   canonical_position_id,account_feed_id,source_doc_id,source_key,
                   observed_quantity,observed_avg_cost_minor,observed_price_minor,
                   observed_value_minor,observed_currency,account_snapshot_complete
               ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (position_id, feed_id, doc_id, key, *values[:4], iso_currency, int(complete)),
        )
        return outcome

    def _balance(self, payload: dict, label: str) -> str:
        require_shape(
            payload,
            label,
            required={"account", "as_of", "amount", "currency", "evidence"},
            optional={"component", "complete", "prefer"},
        )
        account = self._account(payload["account"], f"{label}.account")
        as_of = iso_date(payload["as_of"], f"{label}.as_of")
        self._require_active(account, as_of, label)
        iso_currency = currency(payload["currency"], f"{label}.currency")
        return self._record_balance(
            label,
            account,
            as_of,
            self._component(account, iso_currency, payload.get("component")),
            minor(payload["amount"], iso_currency, f"{label}.amount"),
            iso_currency,
            self._snapshot_evidence(account, payload["evidence"], f"{label}.evidence"),
            complete=_flag(payload, "complete", f"{label}.complete"),
            prefer=_flag(payload, "prefer", f"{label}.prefer"),
        )

    def _position_values(
        self, payload: dict, iso_currency: str, label: str
    ) -> tuple[Decimal, dict[str, Optional[int]]]:
        quantity = decimal(payload["quantity"], f"{label}.quantity")
        value = (
            minor(payload["value"], iso_currency, f"{label}.value")
            if payload.get("value") is not None
            else None
        )
        if value is not None and quantity != 0 and (value < 0) != (quantity < 0):
            raise IntentError(f"{label}: a position's value carries its quantity's sign")
        return quantity, {
            "avg_cost": _per_unit_minor(payload.get("avg_cost"), iso_currency, f"{label}.avg_cost"),
            "price": _per_unit_minor(payload.get("price"), iso_currency, f"{label}.price"),
            "value": value,
        }

    def _position(self, payload: dict, label: str) -> str:
        require_shape(
            payload,
            label,
            required={"account", "as_of", "ticker", "quantity", "currency", "evidence"},
            optional={"avg_cost", "price", "value", "complete", "prefer"},
        )
        account = self._account(payload["account"], f"{label}.account")
        as_of = iso_date(payload["as_of"], f"{label}.as_of")
        self._require_active(account, as_of, label)
        iso_currency = currency(payload["currency"], f"{label}.currency")
        quantity, observed = self._position_values(payload, iso_currency, label)
        return self._record_position(
            label,
            account,
            as_of,
            self._security(payload["ticker"], iso_currency, f"{label}.ticker"),
            quantity,
            iso_currency,
            observed,
            self._snapshot_evidence(account, payload["evidence"], f"{label}.evidence"),
            complete=_flag(payload, "complete", f"{label}.complete"),
            prefer=_flag(payload, "prefer", f"{label}.prefer"),
        )

    def _snapshot(self, payload: dict, label: str) -> str:
        """One brokerage read: cash per currency, holdings, and completeness.

        A complete read also closes what it omits: every previously held
        security and previously non-zero cash currency gets an explicit zero.
        """

        require_shape(
            payload,
            label,
            required={"account", "as_of", "evidence"},
            optional={"cash", "positions", "complete"},
        )
        account = self._account(payload["account"], f"{label}.account")
        if account["kind"] != "brokerage":
            raise IntentError(f"{label}: snapshots describe brokerage accounts")
        as_of = iso_date(payload["as_of"], f"{label}.as_of")
        self._require_active(account, as_of, label)
        complete = _flag(payload, "complete", f"{label}.complete")
        spec = payload["evidence"]
        if isinstance(spec, dict) and "key" in spec:
            require_shape(spec, f"{label}.evidence", required={"feed", "key"}, optional={"document"})
            prefix = text(spec["key"], f"{label}.evidence.key", optional=False)
        else:
            require_shape(spec, f"{label}.evidence", required={"document"}, optional={"feed"})
            prefix = None
        feed_id = (
            self._feed(account["id"], text(spec["feed"], f"{label}.evidence.feed", optional=False),
                       f"{label}.evidence.feed", create=False)
            if spec.get("feed") is not None
            else None
        )

        def evidence_for(suffix: str) -> tuple[Optional[int], str, Optional[int]]:
            item = {**spec, "key": f"{prefix}:{suffix}"} if prefix else {**spec, "row": suffix}
            return self._snapshot_evidence(account, item, f"{label}.evidence")

        outcomes: list[str] = []
        cash_seen: set[str] = set()
        for index, item in enumerate(payload.get("cash") or []):
            field = f"{label}.cash[{index}]"
            require_shape(item, field, required={"currency", "amount"})
            iso_currency = currency(item["currency"], f"{field}.currency")
            if iso_currency in cash_seen:
                raise IntentError(f"{field}: currency listed twice")
            cash_seen.add(iso_currency)
            outcomes.append(
                self._record_balance(
                    field,
                    account,
                    as_of,
                    f"cash_{iso_currency.lower()}",
                    minor(item["amount"], iso_currency, f"{field}.amount"),
                    iso_currency,
                    evidence_for(f"cash:{iso_currency}"),
                    complete=complete,
                    prefer=False,
                )
            )
        held_seen: set[int] = set()
        for index, item in enumerate(payload.get("positions") or []):
            field = f"{label}.positions[{index}]"
            require_shape(
                item,
                field,
                required={"ticker", "quantity", "currency"},
                optional={"avg_cost", "price", "value"},
            )
            iso_currency = currency(item["currency"], f"{field}.currency")
            security_id = self._security(item["ticker"], iso_currency, f"{field}.ticker")
            if security_id in held_seen:
                raise IntentError(f"{field}: security listed twice")
            held_seen.add(security_id)
            quantity, observed = self._position_values(item, iso_currency, field)
            ticker = self.con.execute(
                "SELECT ticker FROM securities WHERE id=?", (security_id,)
            ).fetchone()[0]
            outcomes.append(
                self._record_position(
                    field,
                    account,
                    as_of,
                    security_id,
                    quantity,
                    iso_currency,
                    observed,
                    evidence_for(f"pos:{ticker}"),
                    complete=complete,
                    prefer=False,
                )
            )
        if complete:
            outcomes.extend(
                self._close_omitted(
                    label, account, as_of, feed_id, cash_seen, held_seen, evidence_for
                )
            )
        if not outcomes:
            raise IntentError(f"{label}: a snapshot needs cash or positions")
        if "created" in outcomes or "replaced" in outcomes:
            return "created"
        return "linked" if "linked" in outcomes else "unchanged"

    def _close_omitted(
        self,
        label: str,
        account: sqlite3.Row,
        as_of: str,
        feed_id: Optional[int],
        cash_seen: set[str],
        held_seen: set[int],
        evidence_for,
    ) -> list[str]:
        """Zero what a complete read omits, within what its feed can see.

        A feed only vouches for cash and holdings it has reported before: a
        wrapper broker's own-currency wallet is invisible to the custodian's API,
        so the API's complete read must not zero it.
        """

        outcomes: list[str] = []
        previous = (date.fromisoformat(as_of) - timedelta(days=1)).isoformat()
        for row in self.con.execute(
            """SELECT balance.currency FROM balances AS balance
                WHERE balance.account_id=? AND balance.as_of<?
                  AND balance.component='cash_' || lower(balance.currency)
                  AND balance.as_of=(
                    SELECT MAX(latest.as_of) FROM balances AS latest
                     WHERE latest.account_id=balance.account_id
                       AND latest.component=balance.component AND latest.as_of<?
                  )
                  AND balance.amount_minor<>0
                  AND (? IS NULL OR EXISTS (
                    SELECT 1 FROM balance_evidence AS seen
                      JOIN balances AS observed ON observed.id=seen.canonical_balance_id
                     WHERE seen.account_feed_id=? AND observed.account_id=balance.account_id
                       AND observed.component=balance.component
                  ))""",
            (account["id"], as_of, as_of, feed_id, feed_id),
        ).fetchall():
            if row["currency"] in cash_seen:
                continue
            outcomes.append(
                self._record_balance(
                    f"{label}.closed_cash",
                    account,
                    as_of,
                    f"cash_{row['currency'].lower()}",
                    0,
                    row["currency"],
                    evidence_for(f"cash:{row['currency']}"),
                    complete=True,
                    prefer=False,
                )
            )
        held = AccountingEngine(self.con).security_quantities_at(
            previous, account_id=int(account["id"])
        )
        for security_id in sorted(held):
            if security_id in held_seen:
                continue
            if feed_id is not None and not self.con.execute(
                """SELECT 1 FROM position_evidence AS seen
                     JOIN positions AS observed ON observed.id=seen.canonical_position_id
                    WHERE seen.account_feed_id=? AND observed.account_id=?
                      AND observed.security_id=?""",
                (feed_id, account["id"], security_id),
            ).fetchone():
                continue
            security = self.con.execute(
                "SELECT ticker,currency FROM securities WHERE id=?", (security_id,)
            ).fetchone()
            outcomes.append(
                self._record_position(
                    f"{label}.closed_position",
                    account,
                    as_of,
                    security_id,
                    Decimal(0),
                    security["currency"],
                    {"avg_cost": None, "price": None, "value": 0},
                    evidence_for(f"pos:{security['ticker']}"),
                    complete=True,
                    prefer=False,
                )
            )
        return outcomes

    # ------------------------------------------------------------- payslips

    def _payslip(self, payload: dict, label: str) -> str:
        require_shape(
            payload,
            label,
            required={"employer", "period_start", "period_end", "gross", "net"},
            optional={"paid_on", "currency", "document", "line_items", *PAYSLIP_AMOUNTS},
        )
        iso_currency = currency(payload.get("currency", "ILS"), f"{label}.currency")
        values: dict[str, Any] = {
            "employer": text(payload["employer"], f"{label}.employer", optional=False),
            "period_start": iso_date(payload["period_start"], f"{label}.period_start"),
            "period_end": iso_date(payload["period_end"], f"{label}.period_end"),
            "currency": iso_currency,
            "gross_minor": minor(payload["gross"], iso_currency, f"{label}.gross"),
            "net_minor": minor(payload["net"], iso_currency, f"{label}.net"),
        }
        if values["period_end"] < values["period_start"]:
            raise IntentError(f"{label}: period ends before it starts")
        for name in PAYSLIP_AMOUNTS:
            values[f"{name}_minor"] = (
                minor(payload[name], iso_currency, f"{label}.{name}")
                if payload.get(name) is not None
                else 0
            )
        paid_on = optional_date(payload, "paid_on", f"{label}.paid_on")
        doc_id = (
            self._document_id(payload["document"], f"{label}.document")
            if payload.get("document") is not None
            else None
        )
        items = []
        for index, item in enumerate(payload.get("line_items") or []):
            field = f"{label}.line_items[{index}]"
            require_shape(item, field, required={"label", "amount", "kind"})
            if item["kind"] not in LINE_ITEM_KINDS:
                raise IntentError(f"{field}.kind must be earning, deduction or info")
            items.append(
                (
                    text(item["label"], f"{field}.label", optional=False),
                    minor(item["amount"], iso_currency, f"{field}.amount"),
                    item["kind"],
                )
            )
        row = self.con.execute(
            "SELECT * FROM payslips WHERE period_start=? AND period_end=? AND employer=?",
            (values["period_start"], values["period_end"], values["employer"]),
        ).fetchone()
        if row is None:
            payslip_id = int(
                self.con.execute(
                    f"""INSERT INTO payslips(
                            {",".join(values)},paid_on,source_doc_id
                        ) VALUES ({",".join("?" for _ in values)},?,?) RETURNING id""",
                    (*values.values(), paid_on, doc_id),
                ).fetchone()[0]
            )
            self.con.executemany(
                """INSERT INTO payslip_line_items(payslip_id,label,amount_minor,kind)
                   VALUES (?,?,?,?)""",
                [(payslip_id, *item) for item in items],
            )
            return "created"
        if any(row[key] != value for key, value in values.items()):
            raise IntentError(f"{label}: a payslip for this period has different amounts")
        recorded = sorted(
            (item["label"], item["amount_minor"], item["kind"])
            for item in self.con.execute(
                "SELECT label,amount_minor,kind FROM payslip_line_items WHERE payslip_id=?",
                (row["id"],),
            )
        )
        if payload.get("line_items") is not None and sorted(items) != recorded:
            raise IntentError(f"{label}: a payslip for this period has different line items")
        # Pairing with the salary credit arrives later; only these two may be filled in.
        changes = {}
        if paid_on is not None and paid_on != row["paid_on"]:
            changes["paid_on"] = paid_on
        if doc_id is not None and row["source_doc_id"] is None:
            changes["source_doc_id"] = doc_id
        if not changes:
            return "unchanged"
        assignments = ",".join(f"{key}=:{key}" for key in changes)
        self.con.execute(
            f"UPDATE payslips SET {assignments} WHERE id=:id", {**changes, "id": row["id"]}
        )
        return "updated"


__all__ = ["ReferenceIntents"]
