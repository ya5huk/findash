"""Apply typed ledger intents inside one SQLite transaction.

An intent states what Claude observed. This module derives everything that can
be derived and writes every row the fact implies: raw observation, source
identity, canonical event, evidence, and relationship links. Re-applying the
same intent is a no-op; an intent that contradicts recorded facts is rejected.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace
from decimal import ROUND_HALF_UP, Decimal
import hashlib
import json
import sqlite3
from typing import Any, Callable, Optional

from findash_core.currencies import minor_to_major, minor_unit_factor

from .rates import implied_rate, market_rate, rate_orientation
from .reference import ReferenceIntents
from .values import (
    IntentError,
    currency,
    decimal,
    iso_date,
    minor,
    optional_date,
    positive_id,
    positive_minor,
    require_shape,
    text,
)


FLOW_TYPES = frozenset(
    {"expense", "income", "transfer", "investment", "tax", "refund", "other"}
)
DOCUMENT_FEED = "document-ledger"
MAX_INTENTS = 2_000

EXPECTED_LEG_NOTE = (
    "Expected counterpart of an own-account transfer; confirmed when its cash "
    "is observed."
)


@dataclass(frozen=True)
class Evidence:
    """Where an observation came from.

    ``source`` evidence names a feed observation (document row or provider
    key). ``transaction`` adopts an existing raw row. ``event`` points at an
    already-normalized canonical event.
    """

    kind: str
    feed_id: Optional[int] = None
    source_key: Optional[str] = None
    source_doc_id: Optional[int] = None
    transaction: Optional[sqlite3.Row] = None
    event: Optional[sqlite3.Row] = None

    def suffixed(self, suffix: str) -> "Evidence":
        if self.kind != "source":
            raise IntentError("only document or feed evidence can be shared by two legs")
        return Evidence(
            kind="source",
            feed_id=self.feed_id,
            source_key=f"{self.source_key}:{suffix}",
            source_doc_id=self.source_doc_id,
        )


@dataclass(frozen=True)
class CashFacts:
    account: sqlite3.Row
    occurred_on: str
    amount_minor: int
    currency: str
    flow_type: str
    category: Optional[str]
    component: Optional[str]
    description: Optional[str] = None
    counterparty: Optional[str] = None
    reference: Optional[str] = None
    value_date: Optional[str] = None


def request_hash(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
    return hashlib.sha256(encoded).hexdigest()[:16]


class LedgerWriter(ReferenceIntents):
    """Applies intents against an open connection inside the caller's transaction."""

    def __init__(self, connection: sqlite3.Connection):
        self.con = connection
        self.con.row_factory = sqlite3.Row
        self.outcomes: Counter[str] = Counter()
        self._handlers: dict[str, Callable[[dict, str], str]] = {
            "cash": self._cash,
            "transfer": self._transfer,
            "fx_conversion": self._fx_conversion,
            "trade": self._trade,
            "trade_settlement": self._trade_settlement,
            "dividend": self._dividend,
            "confirm": self._confirm,
            "exclude": self._exclude,
            "raw_sql": self._raw_sql,
            **self.reference_handlers(),
        }

    # ------------------------------------------------------------------ entry

    def apply(self, intents: Any, *, raw_sql_validator=None) -> dict[str, int]:
        if not isinstance(intents, list) or not intents:
            raise IntentError("intents must be a non-empty list")
        if len(intents) > MAX_INTENTS:
            raise IntentError(f"a request may hold at most {MAX_INTENTS} intents")
        self._raw_sql_validator = raw_sql_validator
        types: Counter[str] = Counter()
        for index, intent in enumerate(intents):
            if not isinstance(intent, dict) or not isinstance(intent.get("type"), str):
                raise IntentError(f"intent {index} needs a string type")
            kind = intent["type"]
            handler = self._handlers.get(kind)
            if handler is None:
                raise IntentError(f"intent {index}: unknown type")
            label = f"intent {index} ({kind})"
            outcome = handler({k: v for k, v in intent.items() if k != "type"}, label)
            self.outcomes[f"{kind}:{outcome}"] += 1
            types[kind] += 1
        summary = ", ".join(f"{kind}={count}" for kind, count in sorted(types.items()))
        self.con.execute(
            "INSERT INTO ledger_log(kind,summary,request_hash) VALUES ('intents',?,?)",
            (summary, request_hash(intents)),
        )
        return dict(sorted(self.outcomes.items()))

    # -------------------------------------------------------------- lookups

    def _account(self, reference: Any, field: str) -> sqlite3.Row:
        if isinstance(reference, bool):
            raise IntentError(f"{field} must be an account id or exact name")
        if isinstance(reference, int):
            row = self.con.execute(
                "SELECT * FROM accounts WHERE id=?", (reference,)
            ).fetchone()
        elif isinstance(reference, str) and reference.strip():
            row = self.con.execute(
                "SELECT * FROM accounts WHERE name=?", (reference.strip(),)
            ).fetchone()
        else:
            raise IntentError(f"{field} must be an account id or exact name")
        if row is None:
            raise IntentError(f"{field}: unknown account")
        return row

    @staticmethod
    def _require_active(account: sqlite3.Row, on_date: str, field: str) -> None:
        if (account["opened_on"] and on_date < account["opened_on"]) or (
            account["closed_on"] and on_date >= account["closed_on"]
        ):
            raise IntentError(f"{field}: date falls outside the account's lifetime")

    def _document_id(self, drive_id: Any, field: str) -> int:
        if not isinstance(drive_id, str) or not drive_id.strip():
            raise IntentError(f"{field} must be a document drive_id")
        row = self.con.execute(
            "SELECT id FROM documents WHERE drive_id=?", (drive_id,)
        ).fetchone()
        if row is None:
            raise IntentError(f"{field}: unknown document")
        return int(row["id"])

    def _feed(self, account_id: int, provider: str, field: str, *, create: bool) -> int:
        rows = self.con.execute(
            """SELECT id FROM account_feeds
                WHERE account_id=? AND provider=? AND is_active=1
                ORDER BY id""",
            (account_id, provider),
        ).fetchall()
        if len(rows) > 1:
            raise IntentError(f"{field}: provider has several active feeds on this account")
        if rows:
            return int(rows[0]["id"])
        if not create:
            raise IntentError(f"{field}: account has no active feed for that provider")
        return int(
            self.con.execute(
                """INSERT INTO account_feeds(account_id,provider,feed_type,label)
                   VALUES (?,?,'document','Normalized document ledger')
                   RETURNING id""",
                (account_id, provider),
            ).fetchone()[0]
        )

    def _event(self, event_id: int, field: str) -> sqlite3.Row:
        row = self.con.execute(
            "SELECT * FROM canonical_events WHERE id=?", (event_id,)
        ).fetchone()
        if row is None:
            raise IntentError(f"{field}: unknown event")
        return row

    def _security(self, ticker: Any, iso_currency: str, field: str) -> int:
        symbol = text(ticker, field, optional=False)
        row = self.con.execute(
            "SELECT id, currency FROM securities WHERE ticker=?", (symbol,)
        ).fetchone()
        if row is not None:
            if row["currency"] != iso_currency:
                raise IntentError(f"{field}: security is recorded in another currency")
            return int(row["id"])
        return int(
            self.con.execute(
                "INSERT INTO securities(ticker,currency) VALUES (?,?) RETURNING id",
                (symbol, iso_currency),
            ).fetchone()[0]
        )

    def _evidence(self, account: sqlite3.Row, spec: Any, field: str) -> Evidence:
        if not isinstance(spec, dict):
            raise IntentError(f"{field} must be an object")
        if "transaction" in spec:
            require_shape(spec, field, required={"transaction"})
            row = self.con.execute(
                "SELECT * FROM transactions WHERE id=?",
                (positive_id(spec["transaction"], f"{field}.transaction"),),
            ).fetchone()
            if row is None:
                raise IntentError(f"{field}: unknown transaction")
            if row["account_id"] != account["id"]:
                raise IntentError(f"{field}: transaction belongs to another account")
            return Evidence(kind="transaction", transaction=row)
        if "event" in spec:
            require_shape(spec, field, required={"event"})
            row = self._event(positive_id(spec["event"], f"{field}.event"), field)
            if row["account_id"] != account["id"]:
                raise IntentError(f"{field}: event belongs to another account")
            return Evidence(kind="event", event=row)
        if "key" in spec:
            require_shape(spec, field, required={"feed", "key"}, optional={"document"})
            provider = text(spec["feed"], f"{field}.feed", optional=False)
            key = text(spec["key"], f"{field}.key", optional=False)
            doc_id = (
                self._document_id(spec["document"], f"{field}.document")
                if spec.get("document") is not None
                else None
            )
            return Evidence(
                kind="source",
                feed_id=self._feed(account["id"], provider, f"{field}.feed", create=False),
                source_key=key,
                source_doc_id=doc_id,
            )
        require_shape(spec, field, required={"document", "row"}, optional={"feed"})
        doc_id = self._document_id(spec["document"], f"{field}.document")
        row_key = spec["row"]
        if isinstance(row_key, bool) or not isinstance(row_key, (int, str)) or not str(row_key).strip():
            raise IntentError(f"{field}.row must be a stable row identifier")
        provider = (
            text(spec["feed"], f"{field}.feed", optional=False)
            if spec.get("feed") is not None
            else DOCUMENT_FEED
        )
        return Evidence(
            kind="source",
            feed_id=self._feed(account["id"], provider, f"{field}.feed", create=True),
            source_key=f"document:{doc_id}:{str(row_key).strip()}",
            source_doc_id=doc_id,
        )

    # ----------------------------------------------------------- primitives

    def _source_event(
        self, evidence: Evidence, event_type: str, occurred_on: str
    ) -> tuple[int, bool]:
        row = self.con.execute(
            """SELECT id,event_type FROM source_events
                WHERE account_feed_id=? AND source_key=?""",
            (evidence.feed_id, evidence.source_key),
        ).fetchone()
        if row is not None:
            if row["event_type"] != event_type:
                raise IntentError("evidence is already recorded as another event type")
            return int(row["id"]), False
        return (
            int(
                self.con.execute(
                    """INSERT INTO source_events(
                           account_feed_id,source_key,event_type,occurred_on,source_doc_id
                       ) VALUES (?,?,?,?,?) RETURNING id""",
                    (
                        evidence.feed_id,
                        evidence.source_key,
                        event_type,
                        occurred_on,
                        evidence.source_doc_id,
                    ),
                ).fetchone()[0]
            ),
            True,
        )

    def _linked_canonical(self, source_id: int) -> Optional[int]:
        row = self.con.execute(
            """SELECT canonical_event_id FROM event_evidence
                WHERE source_event_id=? AND evidence_role!='aggregate'""",
            (source_id,),
        ).fetchone()
        return int(row[0]) if row else None

    def new_cash_event(self, facts: CashFacts, *, status: str, notes: Optional[str]) -> int:
        return int(
            self.con.execute(
                """INSERT INTO canonical_events(
                       account_id,event_type,occurred_on,currency,amount_minor,
                       flow_type,category,component,status,notes
                   ) VALUES (?,'cash',?,?,?,?,?,?,?,?) RETURNING id""",
                (
                    facts.account["id"],
                    facts.occurred_on,
                    facts.currency,
                    facts.amount_minor,
                    facts.flow_type,
                    facts.category,
                    facts.component,
                    status,
                    notes,
                ),
            ).fetchone()[0]
        )

    def _link_evidence(self, canonical_id: int, source_id: int, role: str) -> None:
        self.con.execute(
            """INSERT INTO event_evidence(
                   canonical_event_id,source_event_id,evidence_role,match_method,confidence
               ) VALUES (?,?,?,'intent',1.0)""",
            (canonical_id, source_id, role),
        )

    def _raw_transaction(
        self,
        facts: CashFacts,
        evidence: Evidence,
        source_id: int,
        canonical_id: Optional[int],
    ) -> None:
        self.con.execute(
            """INSERT INTO transactions(
                   account_id,date,value_date,amount_minor,currency,category,component,
                   counterparty,reference,description,source_doc_id,flow_type,
                   source_event_id,canonical_event_id
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                facts.account["id"],
                facts.occurred_on,
                facts.value_date,
                facts.amount_minor,
                facts.currency,
                facts.category,
                facts.component,
                facts.counterparty,
                facts.reference,
                facts.description,
                evidence.source_doc_id,
                facts.flow_type,
                source_id,
                canonical_id,
            ),
        )

    @staticmethod
    def _same_cash(row: sqlite3.Row, facts: CashFacts) -> bool:
        return (
            row["event_type"] == "cash"
            and row["account_id"] == facts.account["id"]
            and row["currency"] == facts.currency
            and int(row["amount_minor"]) == facts.amount_minor
        )

    def observe_cash(
        self,
        facts: CashFacts,
        evidence: Evidence,
        *,
        status: str = "confirmed",
        notes: Optional[str] = None,
        same_as: Optional[list[int]] = None,
    ) -> tuple[int, str]:
        """Record one cash observation; return ``(canonical_id, outcome)``.

        The outcome is ``created`` (new canonical event), ``linked`` (new
        observation supporting an existing event) or ``unchanged``.
        """

        if evidence.kind == "event":
            event = evidence.event
            if not self._same_cash(event, facts):
                raise IntentError("referenced event does not match the stated facts")
            return int(event["id"]), "unchanged"

        if evidence.kind == "transaction":
            raw = evidence.transaction
            if (
                raw["currency"] != facts.currency
                or int(raw["amount_minor"]) != facts.amount_minor
                or raw["date"] != facts.occurred_on
            ):
                raise IntentError("adopted transaction does not match the stated facts")
            if raw["canonical_event_id"] is not None:
                event = self._event(int(raw["canonical_event_id"]), "adopted transaction")
                if not self._same_cash(event, facts):
                    raise IntentError("adopted transaction's event contradicts the stated facts")
                return int(event["id"]), "unchanged"
            if raw["source_event_id"] is not None:
                source_id = int(raw["source_event_id"])
                existing = self._linked_canonical(source_id)
                if existing is not None:
                    raise IntentError("adopted transaction is already normalized elsewhere")
            else:
                source_id, _ = self._source_event(
                    Evidence(
                        kind="source",
                        feed_id=self._feed(
                            facts.account["id"], DOCUMENT_FEED, "evidence", create=True
                        ),
                        source_key=(
                            f"document:{raw['source_doc_id']}:transaction:{raw['id']}"
                            if raw["source_doc_id"] is not None
                            else f"transaction:{raw['id']}"
                        ),
                        source_doc_id=raw["source_doc_id"],
                    ),
                    "cash",
                    facts.occurred_on,
                )
            canonical_id = self.new_cash_event(facts, status=status, notes=notes)
            self._link_evidence(canonical_id, source_id, "primary")
            self.con.execute(
                """UPDATE transactions
                      SET category=?,flow_type=?,component=?,
                          source_event_id=?,canonical_event_id=?
                    WHERE id=?""",
                (
                    facts.category,
                    facts.flow_type,
                    facts.component,
                    source_id,
                    canonical_id,
                    raw["id"],
                ),
            )
            return canonical_id, "created"

        source_id, created = self._source_event(evidence, "cash", facts.occurred_on)
        if not created:
            existing = self._linked_canonical(source_id)
            if existing is None:
                raise IntentError("evidence exists without a normalized event; review it")
            if same_as:
                if existing not in same_as:
                    raise IntentError("evidence already supports a different event")
            elif not self._same_cash(self._event(existing, "evidence"), facts):
                raise IntentError("evidence is already recorded with different facts")
            return existing, "unchanged"

        if same_as:
            role = "supporting" if len(same_as) == 1 else "aggregate"
            for target in same_as:
                event = self._event(target, "same_as_event")
                if event["event_type"] != "cash" or event["account_id"] != facts.account["id"]:
                    raise IntentError("same_as_event must be a cash event on the same account")
                if role == "supporting" and not self._same_cash(event, facts):
                    raise IntentError("same_as_event does not match the stated facts")
                self._link_evidence(target, source_id, role)
            pointer = same_as[0] if role == "supporting" else None
            self._raw_transaction(facts, evidence, source_id, pointer)
            return same_as[0], "linked"

        canonical_id = self.new_cash_event(facts, status=status, notes=notes)
        self._link_evidence(canonical_id, source_id, "primary")
        self._raw_transaction(facts, evidence, source_id, canonical_id)
        return canonical_id, "created"

    def _peek_cash(self, evidence: Evidence) -> Optional[int]:
        """The canonical event this evidence already resolves to, without writing."""

        if evidence.kind == "event":
            return int(evidence.event["id"])
        if evidence.kind == "transaction":
            raw = evidence.transaction
            if raw["canonical_event_id"] is not None:
                return int(raw["canonical_event_id"])
            if raw["source_event_id"] is not None:
                return self._linked_canonical(int(raw["source_event_id"]))
            return None
        row = self.con.execute(
            "SELECT id FROM source_events WHERE account_feed_id=? AND source_key=?",
            (evidence.feed_id, evidence.source_key),
        ).fetchone()
        return self._linked_canonical(int(row[0])) if row else None

    def _has_evidence(self, event_id: int) -> bool:
        return bool(
            self.con.execute(
                "SELECT 1 FROM event_evidence WHERE canonical_event_id=?", (event_id,)
            ).fetchone()
        )

    def attach_first_observation(
        self, event_id: int, facts: CashFacts, evidence: Evidence
    ) -> None:
        """Give an expected or inferred event its first real observation and confirm it."""

        if self._has_evidence(event_id):
            raise IntentError("this event is already observed")
        if evidence.kind == "event":
            raise IntentError("an unobserved event can only take a new observation")
        if evidence.kind == "transaction":
            raw = evidence.transaction
            if raw["canonical_event_id"] is not None or (
                raw["currency"] != facts.currency
                or int(raw["amount_minor"]) != facts.amount_minor
                or raw["date"] != facts.occurred_on
            ):
                raise IntentError("adopted transaction does not match the event")
            source_id, _ = self._source_event(
                Evidence(
                    kind="source",
                    feed_id=self._feed(facts.account["id"], DOCUMENT_FEED, "evidence", create=True),
                    source_key=(
                        f"document:{raw['source_doc_id']}:transaction:{raw['id']}"
                        if raw["source_doc_id"] is not None
                        else f"transaction:{raw['id']}"
                    ),
                    source_doc_id=raw["source_doc_id"],
                ),
                "cash",
                facts.occurred_on,
            )
        else:
            source_id, created = self._source_event(evidence, "cash", facts.occurred_on)
            if not created:
                raise IntentError("evidence is already recorded elsewhere")
        self.con.execute(
            """UPDATE canonical_events SET occurred_on=?,status='confirmed'
                WHERE id=? AND (occurred_on<>? OR status<>'confirmed')""",
            (facts.occurred_on, event_id, facts.occurred_on),
        )
        self._link_evidence(event_id, source_id, "primary")
        if evidence.kind == "transaction":
            self.con.execute(
                """UPDATE transactions
                      SET category=?,flow_type=?,component=?,
                          source_event_id=?,canonical_event_id=?
                    WHERE id=?""",
                (
                    facts.category,
                    facts.flow_type,
                    facts.component,
                    source_id,
                    event_id,
                    evidence.transaction["id"],
                ),
            )
        else:
            self._raw_transaction(facts, evidence, source_id, event_id)

    @staticmethod
    def _component(account: sqlite3.Row, iso_currency: str, explicit: Any) -> Optional[str]:
        if explicit is not None:
            return text(explicit, "component", optional=False)
        if account["kind"] == "brokerage":
            return f"cash_{iso_currency.lower()}"
        return None

    def _same_as(self, payload: dict, label: str) -> Optional[list[int]]:
        value = payload.get("same_as_event")
        if value is None:
            return None
        values = value if isinstance(value, list) else [value]
        if not values:
            raise IntentError(f"{label}.same_as_event must not be empty")
        ids = [positive_id(item, f"{label}.same_as_event") for item in values]
        if len(set(ids)) != len(ids):
            raise IntentError(f"{label}.same_as_event has duplicates")
        return ids

    # ---------------------------------------------------------------- cash

    def _cash(self, payload: dict, label: str) -> str:
        require_shape(
            payload,
            label,
            required={"account", "date", "amount", "currency", "flow_type", "evidence"},
            optional={
                "category",
                "component",
                "description",
                "counterparty",
                "reference",
                "value_date",
                "status",
                "notes",
                "same_as_event",
            },
        )
        account = self._account(payload["account"], f"{label}.account")
        occurred_on = iso_date(payload["date"], f"{label}.date")
        self._require_active(account, occurred_on, label)
        iso_currency = currency(payload["currency"], f"{label}.currency")
        flow_type = payload["flow_type"]
        if flow_type not in FLOW_TYPES:
            raise IntentError(f"{label}.flow_type is not a known accounting class")
        status = payload.get("status", "confirmed")
        if status not in {"confirmed", "needs_review"}:
            raise IntentError(f"{label}.status must be confirmed or needs_review")
        amount = minor(payload["amount"], iso_currency, f"{label}.amount")
        if amount == 0:
            raise IntentError(f"{label}.amount must be non-zero")
        facts = CashFacts(
            account=account,
            occurred_on=occurred_on,
            amount_minor=amount,
            currency=iso_currency,
            flow_type=flow_type,
            category=text(payload.get("category"), f"{label}.category"),
            component=self._component(account, iso_currency, payload.get("component")),
            description=text(payload.get("description"), f"{label}.description"),
            counterparty=text(payload.get("counterparty"), f"{label}.counterparty"),
            reference=text(payload.get("reference"), f"{label}.reference"),
            value_date=optional_date(payload, "value_date", f"{label}.value_date"),
        )
        evidence = self._evidence(account, payload["evidence"], f"{label}.evidence")
        _, outcome = self.observe_cash(
            facts,
            evidence,
            status=status,
            notes=text(payload.get("notes"), f"{label}.notes"),
            same_as=self._same_as(payload, label),
        )
        return outcome

    # ------------------------------------------------------------ transfer

    def _transfer(self, payload: dict, label: str) -> str:
        require_shape(
            payload,
            label,
            required={"date", "amount", "currency", "from", "to"},
            optional={"description", "counterparty", "reference"},
        )
        occurred_on = iso_date(payload["date"], f"{label}.date")
        iso_currency = currency(payload["currency"], f"{label}.currency")
        amount = positive_minor(payload["amount"], iso_currency, f"{label}.amount")
        sides = {}
        for name in ("from", "to"):
            side = require_shape(
                payload[name],
                f"{label}.{name}",
                required={"account"},
                optional={"evidence", "date", "category", "confirmed_by"},
            )
            account = self._account(side["account"], f"{label}.{name}.account")
            side_date = optional_date(side, "date", f"{label}.{name}.date") or occurred_on
            self._require_active(account, side_date, f"{label}.{name}")
            if side.get("evidence") is not None and side.get("confirmed_by") is not None:
                raise IntentError(f"{label}.{name}: give evidence or confirmed_by, not both")
            sides[name] = {
                "account": account,
                "date": side_date,
                "category": text(side.get("category"), f"{label}.{name}.category"),
                "evidence": (
                    self._evidence(account, side["evidence"], f"{label}.{name}.evidence")
                    if side.get("evidence") is not None
                    else None
                ),
                "confirmed_by": text(
                    side.get("confirmed_by"), f"{label}.{name}.confirmed_by"
                ),
            }
        if sides["from"]["account"]["id"] == sides["to"]["account"]["id"]:
            raise IntentError(f"{label}: a transfer needs two different accounts")
        if not any(sides[name]["evidence"] for name in sides):
            raise IntentError(f"{label}: at least one side needs evidence")

        def facts(name: str, sign: int) -> CashFacts:
            side = sides[name]
            account = side["account"]
            default_category = (
                ("deposit" if sign > 0 else "withdrawal")
                if account["kind"] == "brokerage"
                else "transfer"
            )
            return CashFacts(
                account=account,
                occurred_on=side["date"],
                amount_minor=sign * amount,
                currency=iso_currency,
                flow_type="transfer",
                category=side["category"] or default_category,
                component=self._component(account, iso_currency, None),
                description=text(payload.get("description"), f"{label}.description"),
                counterparty=text(payload.get("counterparty"), f"{label}.counterparty"),
                reference=text(payload.get("reference"), f"{label}.reference"),
            )

        leg_facts = {"from": facts("from", -1), "to": facts("to", 1)}
        peeked = {
            name: self._peek_cash(sides[name]["evidence"])
            for name in sides
            if sides[name]["evidence"] is not None
        }
        existing = self._existing_transfer(peeked.values())
        if existing is not None:
            return self._extend_transfer(label, existing, sides, peeked, leg_facts)

        events: dict[str, Optional[int]] = {"from": None, "to": None}
        for name in ("from", "to"):
            evidence = sides[name]["evidence"]
            if evidence is None:
                continue
            event_id, _ = self.observe_cash(leg_facts[name], evidence)
            if self._event(event_id, label)["flow_type"] != "transfer":
                raise IntentError(f"{label}.{name}: evidence is not recorded as a transfer")
            events[name] = event_id

        method = "intent"
        for name in ("from", "to"):
            if events[name] is not None:
                continue
            asserted = sides[name]["confirmed_by"]
            events[name] = self.new_cash_event(
                leg_facts[name],
                status="confirmed" if asserted else "needs_review",
                notes=(
                    f"Counterpart asserted from evidence: {asserted}"
                    if asserted
                    else EXPECTED_LEG_NOTE
                ),
            )
            method = "intent:asserted" if asserted else "intent:expected"
        self.con.execute(
            """INSERT INTO transfer_links(outflow_event_id,inflow_event_id,match_method)
               VALUES (?,?,?)""",
            (events["from"], events["to"], method),
        )
        return "created"

    def _extend_transfer(
        self,
        label: str,
        link: tuple[int, int],
        sides: dict,
        peeked: dict[str, Optional[int]],
        leg_facts: dict[str, CashFacts],
    ) -> str:
        """Re-apply a known transfer: no-op, or fill its expected leg."""

        outcome = "unchanged"
        for name, linked_id in zip(("from", "to"), link):
            leg = self._event(linked_id, label)
            if leg["account_id"] != sides[name]["account"]["id"]:
                raise IntentError(f"{label}: transfer is already linked to another account")
            if name not in peeked or peeked[name] == linked_id:
                continue
            if peeked[name] is not None:
                raise IntentError(f"{label}: evidence is already linked to another transfer")
            if self._has_evidence(linked_id):
                raise IntentError(f"{label}.{name}: this side is already observed")
            self.attach_first_observation(
                linked_id,
                replace(leg_facts[name], occurred_on=sides[name]["date"]),
                sides[name]["evidence"],
            )
            outcome = "confirmed"
        return outcome

    def _existing_transfer(self, event_ids) -> Optional[tuple[int, int]]:
        for event_id in filter(None, event_ids):
            row = self.con.execute(
                """SELECT outflow_event_id,inflow_event_id FROM transfer_links
                    WHERE outflow_event_id=? OR inflow_event_id=?""",
                (event_id, event_id),
            ).fetchone()
            if row is not None:
                return int(row[0]), int(row[1])
        return None

    # ------------------------------------------------------- FX conversion

    @staticmethod
    def _rate_window(rate: Decimal) -> tuple[Decimal, Decimal]:
        half_unit = Decimal(1).scaleb(rate.as_tuple().exponent) / 2
        return rate - half_unit, rate + half_unit

    def _fx_amounts(
        self,
        label: str,
        sell_currency: str,
        buy_currency: str,
        sell_minor: Optional[int],
        buy_minor: Optional[int],
        rate: Optional[Decimal],
    ) -> tuple[int, int, Decimal]:
        base, _ = rate_orientation(sell_currency, buy_currency)
        given = sum(value is not None for value in (sell_minor, buy_minor, rate))
        if given < 2:
            raise IntentError(f"{label}: give two of sell amount, buy amount and rate")
        if rate is not None and rate <= 0:
            raise IntentError(f"{label}.rate must be positive")

        def derive(amount_minor: int, from_currency: str, to_currency: str, value: Decimal) -> Decimal:
            # Rate is quote units per base unit.
            major = minor_to_major(amount_minor, from_currency)
            converted = major / value if from_currency != base else major * value
            return converted * minor_unit_factor(to_currency)

        if rate is not None and (sell_minor is None or buy_minor is None):
            if buy_minor is None:
                buy_minor = int(
                    derive(sell_minor, sell_currency, buy_currency, rate).quantize(
                        Decimal(1), rounding=ROUND_HALF_UP
                    )
                )
            else:
                sell_minor = int(
                    derive(buy_minor, buy_currency, sell_currency, rate).quantize(
                        Decimal(1), rounding=ROUND_HALF_UP
                    )
                )
        elif rate is not None:
            low, high = self._rate_window(rate)
            bounds = sorted(
                derive(sell_minor, sell_currency, buy_currency, edge) for edge in (low, high)
            )
            if not bounds[0] - 1 <= buy_minor <= bounds[1] + 1:
                raise IntentError(f"{label}: amounts and rate contradict each other")
        if sell_minor <= 0 or buy_minor <= 0:
            raise IntentError(f"{label}: conversion amounts must be positive")
        actual = rate or implied_rate(sell_minor, sell_currency, buy_minor, buy_currency, base)
        return sell_minor, buy_minor, actual

    def _fx_conversion(self, payload: dict, label: str) -> str:
        require_shape(
            payload,
            label,
            required={"account", "date", "sell", "buy"},
            optional={"settled_on", "rate", "evidence", "confirmed_by", "description"},
        )
        account = self._account(payload["account"], f"{label}.account")
        converted_on = iso_date(payload["date"], f"{label}.date")
        settled_on = optional_date(payload, "settled_on", f"{label}.settled_on") or converted_on
        if settled_on < converted_on:
            raise IntentError(f"{label}.settled_on precedes the conversion date")
        self._require_active(account, converted_on, label)
        legs = {}
        for name in ("sell", "buy"):
            leg = require_shape(
                payload[name],
                f"{label}.{name}",
                required={"currency"},
                optional={"amount", "evidence"},
            )
            iso_currency = currency(leg["currency"], f"{label}.{name}.currency")
            legs[name] = {
                "currency": iso_currency,
                "amount": (
                    positive_minor(leg["amount"], iso_currency, f"{label}.{name}.amount")
                    if leg.get("amount") is not None
                    else None
                ),
                "evidence": (
                    self._evidence(account, leg["evidence"], f"{label}.{name}.evidence")
                    if leg.get("evidence") is not None
                    else None
                ),
            }
        if legs["sell"]["currency"] == legs["buy"]["currency"]:
            raise IntentError(f"{label}: a conversion needs two different currencies")
        rate = decimal(payload["rate"], f"{label}.rate") if payload.get("rate") is not None else None
        sell_minor, buy_minor, actual = self._fx_amounts(
            label,
            legs["sell"]["currency"],
            legs["buy"]["currency"],
            legs["sell"]["amount"],
            legs["buy"]["amount"],
            rate,
        )
        shared = (
            self._evidence(account, payload["evidence"], f"{label}.evidence")
            if payload.get("evidence") is not None
            else None
        )
        asserted = text(payload.get("confirmed_by"), f"{label}.confirmed_by")
        description = text(payload.get("description"), f"{label}.description")
        leg_evidence = {
            name: legs[name]["evidence"] or (shared.suffixed(name) if shared else None)
            for name in legs
        }
        # Evidence keys make documented legs idempotent. A conversion with an
        # inferred or asserted leg has no such key, so it is matched on its economics.
        same_economics = self.con.execute(
            """SELECT conversion.id,conversion.sell_event_id,conversion.buy_event_id,
                      conversion.match_method
                 FROM fx_conversions AS conversion
                 JOIN canonical_events AS sell ON sell.id=conversion.sell_event_id
                 JOIN canonical_events AS buy ON buy.id=conversion.buy_event_id
                WHERE conversion.account_id=? AND conversion.converted_on=?
                  AND sell.currency=? AND sell.amount_minor=?
                  AND buy.currency=? AND buy.amount_minor=?
                ORDER BY conversion.id""",
            (
                account["id"],
                converted_on,
                legs["sell"]["currency"],
                -sell_minor,
                legs["buy"]["currency"],
                buy_minor,
            ),
        ).fetchone()
        if same_economics is not None and str(same_economics["match_method"]).startswith(
            "reconcile:"
        ):
            for name, event_id in (
                ("sell", same_economics["sell_event_id"]),
                ("buy", same_economics["buy_event_id"]),
            ):
                if leg_evidence[name] is None:
                    continue
                event = self._event(int(event_id), label)
                self.attach_first_observation(
                    int(event_id),
                    CashFacts(
                        account=account,
                        occurred_on=event["occurred_on"],
                        amount_minor=int(event["amount_minor"]),
                        currency=event["currency"],
                        flow_type="investment",
                        category="fx",
                        component=event["component"],
                        description=description,
                    ),
                    leg_evidence[name],
                )
            self.con.execute(
                "UPDATE fx_conversions SET actual_rate=?,match_method=? WHERE id=?",
                (float(actual), "intent", same_economics["id"]),
            )
            return "confirmed"
        if same_economics is not None and None in leg_evidence.values():
            return "unchanged"
        events = {}
        for name, signed, on_date in (
            ("sell", -sell_minor, converted_on),
            ("buy", buy_minor, settled_on),
        ):
            iso_currency = legs[name]["currency"]
            facts = CashFacts(
                account=account,
                occurred_on=on_date,
                amount_minor=signed,
                currency=iso_currency,
                flow_type="investment",
                category="fx",
                component=self._component(account, iso_currency, None),
                description=description,
            )
            evidence = leg_evidence[name]
            if evidence is None:
                if not asserted:
                    raise IntentError(f"{label}: each leg needs evidence or confirmed_by")
                events[name] = (
                    self.new_cash_event(
                        facts,
                        status="confirmed",
                        notes=f"Conversion asserted from evidence: {asserted}",
                    ),
                    "created",
                )
            else:
                events[name] = self.observe_cash(facts, evidence)

        sell_id, buy_id = events["sell"][0], events["buy"][0]
        existing = self.con.execute(
            """SELECT sell_event_id,buy_event_id FROM fx_conversions
                WHERE sell_event_id=? OR buy_event_id=?""",
            (sell_id, buy_id),
        ).fetchone()
        if existing is not None:
            if (int(existing[0]), int(existing[1])) != (sell_id, buy_id):
                raise IntentError(f"{label}: a leg already belongs to another conversion")
            return "unchanged"
        self.record_conversion(
            account_id=int(account["id"]),
            converted_on=converted_on,
            sell_event_id=sell_id,
            buy_event_id=buy_id,
            sell_currency=legs["sell"]["currency"],
            buy_currency=legs["buy"]["currency"],
            actual_rate=actual,
            match_method="intent:asserted" if asserted and not shared else "intent",
        )
        return "created"

    def record_conversion(
        self,
        *,
        account_id: int,
        converted_on: str,
        sell_event_id: int,
        buy_event_id: int,
        sell_currency: str,
        buy_currency: str,
        actual_rate: Decimal,
        match_method: str,
    ) -> None:
        """Link two FX legs and store the user's rate beside the market rate."""

        base, quote = rate_orientation(sell_currency, buy_currency)
        market = market_rate(self.con, base, quote, converted_on)
        self.con.execute(
            """INSERT INTO fx_conversions(
                   account_id,converted_on,sell_event_id,buy_event_id,base_currency,
                   quote_currency,actual_rate,market_rate,market_rate_date,match_method
               ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                account_id,
                converted_on,
                sell_event_id,
                buy_event_id,
                base,
                quote,
                float(actual_rate),
                float(market.rate) if market else None,
                market.date if market else None,
                match_method,
            ),
        )

    # ---------------------------------------------------------------- trade

    def _trade(self, payload: dict, label: str) -> str:
        require_shape(
            payload,
            label,
            required={"account", "date", "ticker", "side", "shares", "price", "currency", "evidence"},
            optional={"fees", "external_id", "same_as_event", "status"},
        )
        account = self._account(payload["account"], f"{label}.account")
        occurred_on = iso_date(payload["date"], f"{label}.date")
        self._require_active(account, occurred_on, label)
        iso_currency = currency(payload["currency"], f"{label}.currency")
        side = payload["side"]
        if side not in {"buy", "sell"}:
            raise IntentError(f"{label}.side must be buy or sell")
        shares = decimal(payload["shares"], f"{label}.shares")
        if shares <= 0:
            raise IntentError(f"{label}.shares must be positive")
        price_minor = int(
            (decimal(payload["price"], f"{label}.price") * minor_unit_factor(iso_currency)).quantize(
                Decimal(1), rounding=ROUND_HALF_UP
            )
        )
        if price_minor < 0:
            raise IntentError(f"{label}.price must not be negative")
        fees_minor = (
            abs(minor(payload["fees"], iso_currency, f"{label}.fees"))
            if payload.get("fees") is not None
            else 0
        )
        status = payload.get("status", "confirmed")
        if status not in {"confirmed", "needs_review"}:
            raise IntentError(f"{label}.status must be confirmed or needs_review")
        security_id = self._security(payload["ticker"], iso_currency, f"{label}.ticker")
        evidence = self._evidence(account, payload["evidence"], f"{label}.evidence")
        if evidence.kind != "source":
            raise IntentError(f"{label}.evidence must name a document row or feed key")
        same_as = self._same_as(payload, label)

        def matches(event: sqlite3.Row) -> bool:
            return (
                event["event_type"] == "trade"
                and event["account_id"] == account["id"]
                and event["security_id"] == security_id
                and event["side"] == side
                and Decimal(str(event["quantity"])) == shares
                and int(event["price_minor"]) == price_minor
                and int(event["fees_minor"]) == fees_minor
                and event["currency"] == iso_currency
            )

        source_id, created = self._source_event(evidence, "trade", occurred_on)
        if not created:
            existing = self._linked_canonical(source_id)
            if existing is None or not (
                (same_as and existing in same_as) or matches(self._event(existing, label))
            ):
                raise IntentError(f"{label}: evidence is already recorded with different facts")
            return "unchanged"

        if same_as:
            role = "supporting" if len(same_as) == 1 else "aggregate"
            for target in same_as:
                event = self._event(target, f"{label}.same_as_event")
                if event["event_type"] != "trade" or event["account_id"] != account["id"]:
                    raise IntentError(f"{label}.same_as_event must be a trade on this account")
                if role == "supporting" and not matches(event):
                    raise IntentError(f"{label}.same_as_event does not match the stated facts")
                self._link_evidence(target, source_id, role)
            canonical_id = same_as[0] if role == "supporting" else None
        else:
            canonical_id = int(
                self.con.execute(
                    """INSERT INTO canonical_events(
                           account_id,event_type,occurred_on,currency,security_id,side,
                           quantity,price_minor,fees_minor,status
                       ) VALUES (?,'trade',?,?,?,?,?,?,?,?) RETURNING id""",
                    (
                        account["id"],
                        occurred_on,
                        iso_currency,
                        security_id,
                        side,
                        float(shares),
                        price_minor,
                        fees_minor,
                        status,
                    ),
                ).fetchone()[0]
            )
            self._link_evidence(canonical_id, source_id, "primary")
        self.con.execute(
            """INSERT INTO trades(
                   date,account_id,security_id,side,shares,price_minor,fees_minor,
                   currency,source_doc_id,external_id,source_event_id,canonical_event_id
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                occurred_on,
                account["id"],
                security_id,
                side,
                float(shares),
                price_minor,
                fees_minor,
                iso_currency,
                evidence.source_doc_id,
                text(payload.get("external_id"), f"{label}.external_id"),
                source_id,
                canonical_id,
            ),
        )
        return "created" if not same_as else "linked"

    def _trade_settlement(self, payload: dict, label: str) -> str:
        require_shape(payload, label, required={"cash_event", "trade_events"})
        cash_id = positive_id(payload["cash_event"], f"{label}.cash_event")
        trades = payload["trade_events"]
        if not isinstance(trades, list) or not trades:
            raise IntentError(f"{label}.trade_events must be a non-empty list")
        trade_ids = [positive_id(item, f"{label}.trade_events") for item in trades]
        created = False
        for trade_id in trade_ids:
            existing = self.con.execute(
                "SELECT cash_event_id FROM trade_cash_links WHERE trade_event_id=?",
                (trade_id,),
            ).fetchone()
            if existing is not None:
                if int(existing[0]) != cash_id:
                    raise IntentError(f"{label}: a trade is already settled by another cash event")
                continue
            self.con.execute(
                """INSERT INTO trade_cash_links(cash_event_id,trade_event_id,match_method)
                   VALUES (?,?,'intent')""",
                (cash_id, trade_id),
            )
            created = True
        return "created" if created else "unchanged"

    # ------------------------------------------------------------- dividend

    def _dividend(self, payload: dict, label: str) -> str:
        require_shape(
            payload,
            label,
            required={"account", "ticker", "date", "currency", "evidence"},
            optional={"ex_date", "gross", "withholding", "net", "description"},
        )
        account = self._account(payload["account"], f"{label}.account")
        paid_on = iso_date(payload["date"], f"{label}.date")
        self._require_active(account, paid_on, label)
        ex_date = optional_date(payload, "ex_date", f"{label}.ex_date")
        if ex_date and ex_date > paid_on:
            raise IntentError(f"{label}.ex_date is after the pay date")
        iso_currency = currency(payload["currency"], f"{label}.currency")
        amounts = {
            name: (
                minor(payload[name], iso_currency, f"{label}.{name}")
                if payload.get(name) is not None
                else None
            )
            for name in ("gross", "withholding", "net")
        }
        gross, withholding, net = amounts["gross"], amounts["withholding"], amounts["net"]
        if withholding is not None:
            withholding = abs(withholding)
        if net is None:
            if gross is None:
                raise IntentError(f"{label}: give net, or gross (with withholding if any)")
            net = gross - (withholding or 0)
        elif gross is not None and withholding is not None and abs(gross - withholding - net) > 1:
            raise IntentError(f"{label}: gross, withholding and net contradict each other")
        if net <= 0:
            raise IntentError(f"{label}: net dividend must be positive")
        security_id = self._security(payload["ticker"], iso_currency, f"{label}.ticker")
        facts = CashFacts(
            account=account,
            occurred_on=paid_on,
            amount_minor=net,
            currency=iso_currency,
            flow_type="income",
            category="dividend",
            component=self._component(account, iso_currency, None),
            description=text(payload.get("description"), f"{label}.description"),
        )
        evidence = self._evidence(account, payload["evidence"], f"{label}.evidence")

        expected = self._open_expected_dividend(account["id"], security_id, ex_date, paid_on)
        if evidence.kind == "source":
            source_id, created = self._source_event(evidence, "cash", paid_on)
            if not created:
                existing = self._linked_canonical(source_id)
                if existing is None or not self._same_cash(self._event(existing, label), facts):
                    raise IntentError(f"{label}: evidence is already recorded with different facts")
                return "unchanged"
            if expected is not None:
                self._settle_expected_dividend(expected, facts)
                self._link_evidence(expected, source_id, "primary")
                self._raw_transaction(facts, evidence, source_id, expected)
                self._update_dividend_detail(expected, gross, withholding)
                return "confirmed"
            canonical_id = self.new_cash_event(facts, status="confirmed", notes=None)
            self._link_evidence(canonical_id, source_id, "primary")
            self._raw_transaction(facts, evidence, source_id, canonical_id)
        else:
            if expected is not None:
                raise IntentError(
                    f"{label}: an expected dividend exists; confirm it with document or feed evidence"
                )
            canonical_id, outcome = self.observe_cash(facts, evidence)
            if outcome != "created" and self.con.execute(
                "SELECT 1 FROM dividend_events WHERE cash_event_id=?", (canonical_id,)
            ).fetchone():
                return "unchanged"
        self.con.execute(
            """INSERT INTO dividend_events(
                   cash_event_id,account_id,security_id,ex_date,gross_minor,withholding_minor
               ) VALUES (?,?,?,?,?,?)""",
            (canonical_id, account["id"], security_id, ex_date, gross, withholding),
        )
        return "created"

    def _open_expected_dividend(
        self, account_id: int, security_id: int, ex_date: Optional[str], paid_on: str
    ) -> Optional[int]:
        if ex_date:
            rows = self.con.execute(
                """SELECT dividend.cash_event_id FROM dividend_events AS dividend
                     JOIN canonical_events AS cash ON cash.id=dividend.cash_event_id
                    WHERE dividend.account_id=? AND dividend.security_id=?
                      AND dividend.ex_date=? AND cash.status='needs_review'""",
                (account_id, security_id, ex_date),
            ).fetchall()
        else:
            rows = self.con.execute(
                """SELECT dividend.cash_event_id FROM dividend_events AS dividend
                     JOIN canonical_events AS cash ON cash.id=dividend.cash_event_id
                    WHERE dividend.account_id=? AND dividend.security_id=?
                      AND cash.status='needs_review'
                      AND dividend.ex_date BETWEEN date(?,'-60 days') AND ?""",
                (account_id, security_id, paid_on, paid_on),
            ).fetchall()
        if len(rows) > 1:
            raise IntentError("several expected dividends match; give ex_date")
        return int(rows[0][0]) if rows else None

    def _settle_expected_dividend(self, event_id: int, facts: CashFacts) -> None:
        self.con.execute(
            """UPDATE canonical_events
                  SET amount_minor=?,occurred_on=?,status='confirmed'
                WHERE id=?""",
            (facts.amount_minor, facts.occurred_on, event_id),
        )

    def _update_dividend_detail(
        self, event_id: int, gross: Optional[int], withholding: Optional[int]
    ) -> None:
        self.con.execute(
            """UPDATE dividend_events
                  SET gross_minor=COALESCE(?,gross_minor),
                      withholding_minor=COALESCE(?,withholding_minor)
                WHERE cash_event_id=?""",
            (gross, withholding, event_id),
        )

    # ------------------------------------------------------ status changes

    def _confirm(self, payload: dict, label: str) -> str:
        require_shape(
            payload, label, required={"event"}, optional={"date", "amount", "evidence", "note"}
        )
        event = self._event(positive_id(payload["event"], f"{label}.event"), label)
        if event["status"] == "excluded":
            raise IntentError(f"{label}: an excluded event cannot be confirmed")
        account = self._account(int(event["account_id"]), label)
        occurred_on = optional_date(payload, "date", f"{label}.date") or event["occurred_on"]
        amount = int(event["amount_minor"]) if event["amount_minor"] is not None else None
        if payload.get("amount") is not None:
            if event["event_type"] != "cash":
                raise IntentError(f"{label}: only cash events take an observed amount")
            amount = minor(payload["amount"], event["currency"], f"{label}.amount")
            if amount != int(event["amount_minor"]) and self._is_linked_cash(int(event["id"])):
                raise IntentError(f"{label}: linked transfer or conversion amounts are fixed")
            if (amount > 0) != (int(event["amount_minor"]) > 0):
                raise IntentError(f"{label}: observed amount changes the event's direction")
        note = text(payload.get("note"), f"{label}.note")
        if (
            event["status"] == "confirmed"
            and occurred_on == event["occurred_on"]
            and amount == event["amount_minor"]
            and note is None
            and payload.get("evidence") is None
        ):
            return "unchanged"
        self.con.execute(
            """UPDATE canonical_events
                  SET status='confirmed',occurred_on=?,amount_minor=?,
                      notes=COALESCE(?,notes)
                WHERE id=?""",
            (occurred_on, amount, note, event["id"]),
        )
        if payload.get("evidence") is not None:
            if event["event_type"] != "cash":
                raise IntentError(f"{label}: attach trade evidence with a trade intent")
            evidence = self._evidence(account, payload["evidence"], f"{label}.evidence")
            if evidence.kind != "source":
                raise IntentError(f"{label}.evidence must name a document row or feed key")
            source_id, created = self._source_event(evidence, "cash", occurred_on)
            if not created:
                if self._linked_canonical(source_id) != int(event["id"]):
                    raise IntentError(f"{label}: evidence already supports another event")
                return "unchanged"
            has_primary = self.con.execute(
                """SELECT 1 FROM event_evidence
                    WHERE canonical_event_id=? AND evidence_role='primary'""",
                (event["id"],),
            ).fetchone()
            self._link_evidence(int(event["id"]), source_id, "supporting" if has_primary else "primary")
            refreshed = self._event(int(event["id"]), label)
            self._raw_transaction(
                CashFacts(
                    account=account,
                    occurred_on=refreshed["occurred_on"],
                    amount_minor=int(refreshed["amount_minor"]),
                    currency=refreshed["currency"],
                    flow_type=refreshed["flow_type"],
                    category=refreshed["category"],
                    component=refreshed["component"],
                ),
                evidence,
                source_id,
                int(event["id"]),
            )
        return "confirmed"

    def _is_linked_cash(self, event_id: int) -> bool:
        return bool(
            self.con.execute(
                """SELECT 1 FROM transfer_links
                    WHERE outflow_event_id=?1 OR inflow_event_id=?1
                   UNION ALL
                   SELECT 1 FROM fx_conversions
                    WHERE sell_event_id=?1 OR buy_event_id=?1""",
                (event_id,),
            ).fetchone()
        )

    def _exclude(self, payload: dict, label: str) -> str:
        require_shape(payload, label, required={"event", "reason"})
        event = self._event(positive_id(payload["event"], f"{label}.event"), label)
        reason = text(payload["reason"], f"{label}.reason", optional=False)
        if event["status"] == "excluded":
            return "unchanged"
        related = {int(event["id"])}
        transfer = self.con.execute(
            """SELECT outflow_event_id,inflow_event_id FROM transfer_links
                WHERE outflow_event_id=?1 OR inflow_event_id=?1""",
            (event["id"],),
        ).fetchone()
        if transfer is not None:
            self.con.execute(
                "DELETE FROM transfer_links WHERE outflow_event_id=?", (transfer[0],)
            )
            # An unobserved counterpart only existed because of this link.
            for leg in (int(transfer[0]), int(transfer[1])):
                if not self.con.execute(
                    "SELECT 1 FROM event_evidence WHERE canonical_event_id=?", (leg,)
                ).fetchone():
                    related.add(leg)
        conversion = self.con.execute(
            """SELECT id,sell_event_id,buy_event_id FROM fx_conversions
                WHERE sell_event_id=?1 OR buy_event_id=?1""",
            (event["id"],),
        ).fetchone()
        if conversion is not None:
            self.con.execute("DELETE FROM fx_conversions WHERE id=?", (conversion[0],))
            related.update({int(conversion[1]), int(conversion[2])})
        for event_id in sorted(related):
            self.con.execute(
                """UPDATE canonical_events
                      SET status='excluded',
                          notes=trim(COALESCE(notes,'') || ' Excluded: ' || ?)
                    WHERE id=? AND status<>'excluded'""",
                (reason, event_id),
            )
        return "excluded"

    # ------------------------------------------------------------ escape hatch

    def _raw_sql(self, payload: dict, label: str) -> str:
        require_shape(payload, label, required={"reason", "statements"})
        reason = text(payload["reason"], f"{label}.reason", optional=False)
        statements = payload["statements"]
        if not isinstance(statements, list) or not statements:
            raise IntentError(f"{label}.statements must be a non-empty list")
        if len(statements) > MAX_INTENTS:
            raise IntentError(f"{label}: at most {MAX_INTENTS} statements")
        if self._raw_sql_validator is None:
            raise IntentError(f"{label}: raw SQL is unavailable here")
        for index, item in enumerate(statements):
            if not isinstance(item, dict) or not isinstance(item.get("sql"), str):
                raise IntentError(f"{label}.statements[{index}] requires a SQL string")
            params = item.get("params", [])
            if not isinstance(params, (list, dict)):
                raise IntentError(f"{label}.statements[{index}].params must be a list or object")
            self.con.execute(self._raw_sql_validator(item["sql"]), params)
        self.con.execute(
            "INSERT INTO ledger_log(kind,summary,request_hash) VALUES ('raw_sql',?,?)",
            (reason, request_hash(statements)),
        )
        return "applied"


__all__ = ["DOCUMENT_FEED", "LedgerWriter", "IntentError"]
