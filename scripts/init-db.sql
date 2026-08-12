-- findash schema (v13)
--
-- Conventions:
--   * All monetary amounts as INTEGER in the ISO currency's minor unit (agorot for
--     ILS, cents for USD, whole yen for JPY). Convert with findash_core.currencies;
--     the exponent is currency-specific, so never assume a factor of 100 or use REAL.
--   * All dates as TEXT in ISO 8601 (YYYY-MM-DD). Timestamps as YYYY-MM-DD HH:MM:SS.
--   * UTF-8 by default; Hebrew text stored as-is.
--   * Foreign keys ON, fail loud on orphan rows.
--   * "component" is the only un-obvious column: NULL for plain accounts; for pension
--     balances/transactions/canonical cash events:
--     'tagmul_employee' | 'tagmul_employer' | 'pitsuyim';
--     for study fund: 'tagmul_employee' | 'tagmul_employer'.

PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

-- Applied migrations. Existing databases are upgraded by scripts/migrate_db.py;
-- this file creates a new database directly at the latest version.
CREATE TABLE schema_migrations (
  version       INTEGER PRIMARY KEY,
  name          TEXT NOT NULL UNIQUE,
  applied_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Source files. Every fact links back here. Drive ID is the natural key for dedup.
CREATE TABLE documents (
  id              INTEGER PRIMARY KEY,
  drive_id        TEXT UNIQUE NOT NULL,
  drive_path      TEXT NOT NULL,
  filename        TEXT NOT NULL,
  doc_type        TEXT,                                    -- payslip | trade_history | bank_statement | mastercard_statement | pension_statement | training_fund_statement | pension_movements | brokerage_screenshot | trade_confirmation | savings_screenshot | net_worth_snapshot | other
  doc_date        TEXT,                                    -- ISO date the doc represents
  ingested_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  raw_hash        TEXT,                                    -- sha256 of bytes for tamper check
  notes           TEXT
);
CREATE INDEX idx_documents_doc_type ON documents(doc_type);
CREATE INDEX idx_documents_doc_date ON documents(doc_date);

-- Flow state, one row per key. Currently: 'last_db_backup_at' = ISO UTC timestamp of the
-- last successful Drive backup (the weekly gate). Skills add this table idempotently
-- (CREATE TABLE IF NOT EXISTS) on databases created before it existed.
CREATE TABLE meta (
  key    TEXT PRIMARY KEY,
  value  TEXT NOT NULL
);

-- Anything that holds value. Includes credit cards (liability; treat balance as negative).
CREATE TABLE accounts (
  id                INTEGER PRIMARY KEY,
  name              TEXT NOT NULL UNIQUE,                  -- human-readable
  kind              TEXT NOT NULL,                         -- checking | savings | brokerage | credit_card | pension | study_fund | cash | other
  institution       TEXT NOT NULL,                         -- 'Bank Hapoalim', 'Harel', 'Brokerage', 'Cash', ...
  external_id       TEXT,                                  -- account/policy number at institution
  currency          TEXT NOT NULL,                         -- ILS, USD, ...
  opened_on         TEXT,
  closed_on         TEXT,
  investment_track  TEXT,                                  -- e.g. 'S&P 500 עוקב מדד' for pension/study_fund/brokerage
  liquidity_date    TEXT,                                  -- when funds become accessible (training fund, deposits)
  notes             TEXT,
  live_source       TEXT                                   -- legacy compatibility marker; new code uses account_feeds
);
CREATE INDEX idx_accounts_kind ON accounts(kind);
CREATE INDEX idx_accounts_institution ON accounts(institution);

-- A logical account may be observed through several feeds. For example, a local
-- wrapper statement and its underlying IBKR API both describe one economic account.
-- They are evidence sources, never values to sum independently.
CREATE TABLE account_feeds (
  id                    INTEGER PRIMARY KEY,
  account_id            INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  provider              TEXT NOT NULL,                     -- ibkr | hapoalim | wrapper-statement | manual | ...
  feed_type             TEXT NOT NULL CHECK (feed_type IN ('document','api','manual')),
  external_account_ref  TEXT,                              -- provider account id; private local data
  label                 TEXT,
  is_active             INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0,1)),
  created_at            TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX uq_account_feeds_provider_ref
  ON account_feeds(provider, external_account_ref)
  WHERE external_account_ref IS NOT NULL;
CREATE UNIQUE INDEX uq_account_feeds_default
  ON account_feeds(account_id, provider)
  WHERE external_account_ref IS NULL;
CREATE INDEX idx_account_feeds_account ON account_feeds(account_id);

-- Point-in-time balance snapshots. Pension/study-fund have multiple rows per snapshot
-- (one per component); plain accounts have one row with component=NULL.
CREATE TABLE balances (
  id              INTEGER PRIMARY KEY,
  account_id      INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  as_of           TEXT NOT NULL,                           -- ISO date
  component       TEXT,                                    -- NULL | tagmul_employee | tagmul_employer | pitsuyim
  amount_minor    INTEGER NOT NULL,
  currency        TEXT NOT NULL,
  source_doc_id   INTEGER REFERENCES documents(id) ON DELETE SET NULL
);
CREATE INDEX idx_balances_account_date ON balances(account_id, as_of);
-- SQLite considers NULL distinct inside a normal UNIQUE constraint. Coalescing the
-- plain-account NULL component closes that hole without changing its public meaning.
CREATE UNIQUE INDEX uq_balances_account_date_component
  ON balances(account_id, as_of, COALESCE(component, ''));
CREATE TRIGGER balances_component_not_empty_insert
BEFORE INSERT ON balances WHEN NEW.component=''
BEGIN
  SELECT RAISE(ABORT, 'balances.component must be NULL or non-empty');
END;
CREATE TRIGGER balances_component_not_empty_update
BEFORE UPDATE OF component ON balances WHEN NEW.component=''
BEGIN
  SELECT RAISE(ABORT, 'balances.component must be NULL or non-empty');
END;

-- Every source that reported a canonical balance can remain attached as evidence.
-- Conflicting observations are retained here while judgment chooses the anchor in
-- balances; they are never summed as separate assets.
CREATE TABLE balance_evidence (
  id                      INTEGER PRIMARY KEY,
  canonical_balance_id    INTEGER NOT NULL REFERENCES balances(id) ON DELETE CASCADE,
  account_feed_id         INTEGER REFERENCES account_feeds(id) ON DELETE SET NULL,
  source_doc_id           INTEGER REFERENCES documents(id) ON DELETE RESTRICT,
  source_key              TEXT NOT NULL,
  observed_amount_minor   INTEGER NOT NULL,
  observed_currency       TEXT NOT NULL,
  account_snapshot_complete INTEGER NOT NULL DEFAULT 0     -- 1 only when this read covered all holdings + cash
                              CHECK (account_snapshot_complete IN (0,1)),
  observed_at             TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX uq_balance_evidence_source
  ON balance_evidence(COALESCE(account_feed_id, 0), source_key);
CREATE INDEX idx_balance_evidence_balance ON balance_evidence(canonical_balance_id);

-- Money flows in/out of accounts. Sign convention: positive = credit (money in),
-- negative = debit (money out). For credit cards, charges are negative; payments are positive.
CREATE TABLE transactions (
  id              INTEGER PRIMARY KEY,
  account_id      INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  date            TEXT NOT NULL,                           -- transaction date (when it happened)
  value_date      TEXT,                                    -- when it cleared (for bank statements)
  amount_minor    INTEGER NOT NULL,                        -- signed
  currency        TEXT NOT NULL,
  category        TEXT,                                    -- salary | deposit | withdrawal | fee | interest | dividend | tax | transfer | check | card_charge | risk_insurance | rent | groceries | ...
                                                            -- Open vocab. Use judgment (see docs/doc-types.md).
  component       TEXT,                                    -- as in balances
  counterparty    TEXT,                                    -- merchant, employer, broker, person, etc.
  reference       TEXT,                                    -- אסמכתא; helps with dedup across docs
  description     TEXT,                                    -- free text from source doc
  source_doc_id   INTEGER REFERENCES documents(id) ON DELETE SET NULL,
  flow_type       TEXT CHECK (flow_type IN ('expense','income','transfer','investment','tax','refund','other')),
  source_event_id INTEGER REFERENCES source_events(id) ON DELETE RESTRICT,
  canonical_event_id INTEGER REFERENCES canonical_events(id) ON DELETE SET NULL
);
CREATE INDEX idx_transactions_account_date ON transactions(account_id, date);
CREATE INDEX idx_transactions_category ON transactions(category);
CREATE INDEX idx_transactions_reference ON transactions(reference);
CREATE UNIQUE INDEX uq_transactions_source_event
  ON transactions(source_event_id) WHERE source_event_id IS NOT NULL;
CREATE INDEX idx_transactions_canonical_event ON transactions(canonical_event_id);

-- Master list of tradables; includes benchmarks like SPY for the comparison chart.
CREATE TABLE securities (
  id              INTEGER PRIMARY KEY,
  ticker          TEXT NOT NULL UNIQUE,
  name            TEXT,
  asset_class     TEXT,                                    -- stock | etf | mutual_fund | bond | crypto | benchmark
  currency        TEXT NOT NULL
);

-- Buy/sell events. Positions are derived: SUM(shares * sign) per (account, security).
-- external_id dedups live-source trades (e.g. the IBKR connector) that cite no document.
CREATE TABLE trades (
  id              INTEGER PRIMARY KEY,
  date            TEXT NOT NULL,
  account_id      INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  security_id     INTEGER NOT NULL REFERENCES securities(id) ON DELETE CASCADE,
  side            TEXT NOT NULL CHECK (side IN ('buy','sell')),
  shares          REAL NOT NULL,                           -- fractional supported
  price_minor     INTEGER NOT NULL,                        -- per-share in minor units of `currency`
  fees_minor      INTEGER NOT NULL DEFAULT 0,
  currency        TEXT NOT NULL,
  source_doc_id   INTEGER REFERENCES documents(id) ON DELETE SET NULL,
  external_id     TEXT,                                    -- legacy live-source id; source_events owns new identity
  source_event_id INTEGER REFERENCES source_events(id) ON DELETE RESTRICT,
  canonical_event_id INTEGER REFERENCES canonical_events(id) ON DELETE SET NULL
);
CREATE INDEX idx_trades_account_security ON trades(account_id, security_id);
CREATE INDEX idx_trades_date ON trades(date);
-- Legacy connector rows without source_events dedup on (account_id, external_id).
-- Feed-backed rows dedup by source_event_id, so provider-local ids may repeat safely.
CREATE UNIQUE INDEX idx_trades_external
  ON trades(account_id, external_id)
  WHERE external_id IS NOT NULL AND source_event_id IS NULL;
CREATE UNIQUE INDEX uq_trades_source_event
  ON trades(source_event_id) WHERE source_event_id IS NOT NULL;
CREATE INDEX idx_trades_canonical_event ON trades(canonical_event_id);

-- Canonical events are the economic facts calculations count once. Raw observations
-- remain in transactions/trades and source_events, so merging two feeds never erases
-- provenance. Existing unlinked facts remain valid during the transition.
CREATE TABLE canonical_events (
  id              INTEGER PRIMARY KEY,
  account_id      INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  event_type      TEXT NOT NULL CHECK (event_type IN ('cash','trade')),
  occurred_on     TEXT NOT NULL,
  currency        TEXT NOT NULL,
  amount_minor    INTEGER,
  flow_type       TEXT CHECK (flow_type IN ('expense','income','transfer','investment','tax','refund','other')),
  category        TEXT,
  component       TEXT,                                    -- normalized cash sub-ledger; NULL for trades/plain cash
  security_id     INTEGER REFERENCES securities(id) ON DELETE RESTRICT,
  side            TEXT CHECK (side IN ('buy','sell')),
  quantity        REAL,
  price_minor     INTEGER,
  fees_minor      INTEGER NOT NULL DEFAULT 0,
  status          TEXT NOT NULL DEFAULT 'confirmed'
                    CHECK (status IN ('confirmed','needs_review','excluded')),
  notes           TEXT,
  created_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CHECK (component IS NULL OR
         (event_type='cash' AND typeof(component)='text' AND length(trim(component))>0)),
  CHECK (
    (event_type='cash'
     AND amount_minor IS NOT NULL AND flow_type IS NOT NULL
     AND security_id IS NULL AND side IS NULL
     AND quantity IS NULL AND price_minor IS NULL)
    OR
    (event_type='trade'
     AND amount_minor IS NULL AND flow_type IS NULL
     AND component IS NULL
     AND security_id IS NOT NULL AND side IS NOT NULL
     AND quantity IS NOT NULL AND quantity > 0
     AND price_minor IS NOT NULL AND price_minor >= 0)
  )
);
CREATE INDEX idx_canonical_events_account_date ON canonical_events(account_id, occurred_on);
CREATE INDEX idx_canonical_events_security_date
  ON canonical_events(security_id, occurred_on) WHERE event_type='trade';

-- Stable identities for provider/document observations. source_key is the provider
-- id when available, otherwise a document-id + row identity chosen at ingestion.
CREATE TABLE source_events (
  id               INTEGER PRIMARY KEY,
  account_feed_id  INTEGER NOT NULL REFERENCES account_feeds(id) ON DELETE CASCADE,
  source_key       TEXT NOT NULL,
  event_type       TEXT NOT NULL CHECK (event_type IN ('cash','trade')),
  occurred_on      TEXT NOT NULL,
  source_doc_id    INTEGER REFERENCES documents(id) ON DELETE RESTRICT,
  payload_hash     TEXT,
  observed_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(account_feed_id, source_key)
);
CREATE INDEX idx_source_events_date ON source_events(occurred_on);

-- Many source observations may support one event. An aggregate statement row may
-- support several canonical events, hence the deliberate many-to-many relationship.
CREATE TABLE event_evidence (
  canonical_event_id  INTEGER NOT NULL REFERENCES canonical_events(id) ON DELETE CASCADE,
  source_event_id     INTEGER NOT NULL REFERENCES source_events(id) ON DELETE RESTRICT,
  evidence_role       TEXT NOT NULL DEFAULT 'supporting'
                        CHECK (evidence_role IN ('primary','supporting','aggregate')),
  match_method        TEXT NOT NULL,
  confidence          REAL CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
  notes               TEXT,
  linked_at           TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (canonical_event_id, source_event_id)
);
CREATE UNIQUE INDEX uq_event_evidence_primary
  ON event_evidence(canonical_event_id) WHERE evidence_role='primary';
CREATE UNIQUE INDEX uq_event_evidence_nonaggregate_source
  ON event_evidence(source_event_id) WHERE evidence_role!='aggregate';
CREATE INDEX idx_event_evidence_source ON event_evidence(source_event_id);
CREATE TRIGGER event_evidence_same_account_insert
BEFORE INSERT ON event_evidence
BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1
    FROM canonical_events c
    JOIN source_events s ON s.id=NEW.source_event_id
    JOIN account_feeds f ON f.id=s.account_feed_id
    WHERE c.id=NEW.canonical_event_id
      AND c.account_id=f.account_id
      AND c.event_type=s.event_type
  ) THEN RAISE(ABORT, 'event evidence account/type mismatch') END;
END;
CREATE TRIGGER event_evidence_same_account_update
BEFORE UPDATE OF canonical_event_id, source_event_id ON event_evidence
BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1
    FROM canonical_events c
    JOIN source_events s ON s.id=NEW.source_event_id
    JOIN account_feeds f ON f.id=s.account_feed_id
    WHERE c.id=NEW.canonical_event_id
      AND c.account_id=f.account_id
      AND c.event_type=s.event_type
  ) THEN RAISE(ABORT, 'event evidence account/type mismatch') END;
END;

-- A canonical trade derives its brokerage-cash effect unless a reviewed, posted
-- canonical cash event explicitly replaces it. One cash posting may cover several
-- fills; no relationship is inferred from matching amount/date alone.
CREATE TABLE trade_cash_links (
  cash_event_id   INTEGER NOT NULL REFERENCES canonical_events(id),
  trade_event_id  INTEGER NOT NULL REFERENCES canonical_events(id),
  match_method    TEXT NOT NULL CHECK (length(trim(match_method)) > 0),
  created_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (cash_event_id, trade_event_id)
);
CREATE UNIQUE INDEX uq_trade_cash_links_trade
  ON trade_cash_links(trade_event_id);
CREATE TRIGGER trade_cash_links_valid_insert
BEFORE INSERT ON trade_cash_links
WHEN NOT (EXISTS (
  SELECT 1 FROM canonical_events AS cash
  JOIN canonical_events AS trade ON trade.id=NEW.trade_event_id
  WHERE cash.id=NEW.cash_event_id
    AND cash.event_type='cash'
    AND cash.flow_type='investment'
    AND cash.status='confirmed'
    AND trade.event_type='trade'
    AND trade.status='confirmed'
    AND cash.account_id=trade.account_id
    AND cash.currency=trade.currency
    AND cash.occurred_on>=trade.occurred_on
))
BEGIN
  SELECT RAISE(ABORT, 'invalid trade cash link');
END;
CREATE TRIGGER trade_cash_links_valid_update
BEFORE UPDATE OF cash_event_id, trade_event_id, match_method
ON trade_cash_links
WHEN NOT (EXISTS (
  SELECT 1 FROM canonical_events AS cash
  JOIN canonical_events AS trade ON trade.id=NEW.trade_event_id
  WHERE cash.id=NEW.cash_event_id
    AND cash.event_type='cash'
    AND cash.flow_type='investment'
    AND cash.status='confirmed'
    AND trade.event_type='trade'
    AND trade.status='confirmed'
    AND cash.account_id=trade.account_id
    AND cash.currency=trade.currency
    AND cash.occurred_on>=trade.occurred_on
))
BEGIN
  SELECT RAISE(ABORT, 'invalid trade cash link');
END;
CREATE TRIGGER canonical_events_trade_cash_identity_update
BEFORE UPDATE OF account_id, event_type, occurred_on, currency,
  amount_minor, flow_type, component, security_id, side, quantity,
  price_minor, fees_minor, status
ON canonical_events
WHEN (
  NEW.account_id IS NOT OLD.account_id
  OR NEW.event_type IS NOT OLD.event_type
  OR NEW.occurred_on IS NOT OLD.occurred_on
  OR NEW.currency IS NOT OLD.currency
  OR NEW.amount_minor IS NOT OLD.amount_minor
  OR NEW.flow_type IS NOT OLD.flow_type
  OR NEW.component IS NOT OLD.component
  OR NEW.security_id IS NOT OLD.security_id
  OR NEW.side IS NOT OLD.side
  OR NEW.quantity IS NOT OLD.quantity
  OR NEW.price_minor IS NOT OLD.price_minor
  OR NEW.fees_minor IS NOT OLD.fees_minor
  OR NEW.status IS NOT OLD.status
) AND (
  EXISTS (
    SELECT 1 FROM trade_cash_links WHERE cash_event_id=OLD.id
  ) OR EXISTS (
    SELECT 1 FROM trade_cash_links WHERE trade_event_id=OLD.id
  )
)
BEGIN
  SELECT RAISE(ABORT, 'linked canonical event economics are immutable');
END;

CREATE TRIGGER transactions_event_links_insert
BEFORE INSERT ON transactions
BEGIN
  SELECT CASE WHEN NEW.source_event_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM source_events s
    JOIN account_feeds f ON f.id=s.account_feed_id
    WHERE s.id=NEW.source_event_id AND s.event_type='cash'
      AND f.account_id=NEW.account_id
  ) THEN RAISE(ABORT, 'transaction source event account/type mismatch') END;
  SELECT CASE WHEN NEW.canonical_event_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM canonical_events c
    WHERE c.id=NEW.canonical_event_id AND c.event_type='cash'
      AND c.account_id=NEW.account_id
  ) THEN RAISE(ABORT, 'transaction canonical event account/type mismatch') END;
END;
CREATE TRIGGER transactions_event_links_update
BEFORE UPDATE OF account_id, source_event_id, canonical_event_id ON transactions
BEGIN
  SELECT CASE WHEN NEW.source_event_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM source_events s
    JOIN account_feeds f ON f.id=s.account_feed_id
    WHERE s.id=NEW.source_event_id AND s.event_type='cash'
      AND f.account_id=NEW.account_id
  ) THEN RAISE(ABORT, 'transaction source event account/type mismatch') END;
  SELECT CASE WHEN NEW.canonical_event_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM canonical_events c
    WHERE c.id=NEW.canonical_event_id AND c.event_type='cash'
      AND c.account_id=NEW.account_id
  ) THEN RAISE(ABORT, 'transaction canonical event account/type mismatch') END;
END;
CREATE TRIGGER trades_event_links_insert
BEFORE INSERT ON trades
BEGIN
  SELECT CASE WHEN NEW.source_event_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM source_events s
    JOIN account_feeds f ON f.id=s.account_feed_id
    WHERE s.id=NEW.source_event_id AND s.event_type='trade'
      AND f.account_id=NEW.account_id
  ) THEN RAISE(ABORT, 'trade source event account/type mismatch') END;
  SELECT CASE WHEN NEW.canonical_event_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM canonical_events c
    WHERE c.id=NEW.canonical_event_id AND c.event_type='trade'
      AND c.account_id=NEW.account_id
  ) THEN RAISE(ABORT, 'trade canonical event account/type mismatch') END;
END;
CREATE TRIGGER trades_event_links_update
BEFORE UPDATE OF account_id, source_event_id, canonical_event_id ON trades
BEGIN
  SELECT CASE WHEN NEW.source_event_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM source_events s
    JOIN account_feeds f ON f.id=s.account_feed_id
    WHERE s.id=NEW.source_event_id AND s.event_type='trade'
      AND f.account_id=NEW.account_id
  ) THEN RAISE(ABORT, 'trade source event account/type mismatch') END;
  SELECT CASE WHEN NEW.canonical_event_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM canonical_events c
    WHERE c.id=NEW.canonical_event_id AND c.event_type='trade'
      AND c.account_id=NEW.account_id
  ) THEN RAISE(ABORT, 'trade canonical event account/type mismatch') END;
END;

-- A raw convenience row may point at a canonical event only when its source
-- has exactly one matching, non-aggregate evidence edge. The raw row may be
-- inserted before that edge inside one atomic gateway batch; the gateway checks
-- the completed graph before commit.
CREATE TRIGGER transactions_event_pointer_insert
BEFORE INSERT ON transactions
WHEN NEW.canonical_event_id IS NOT NULL
  AND (
    NEW.source_event_id IS NULL
    OR (
      EXISTS (
        SELECT 1 FROM event_evidence
        WHERE source_event_id=NEW.source_event_id
      )
      AND (
        (SELECT COUNT(*) FROM event_evidence
         WHERE source_event_id=NEW.source_event_id) != 1
        OR NOT EXISTS (
          SELECT 1 FROM event_evidence
          WHERE source_event_id=NEW.source_event_id
            AND canonical_event_id=NEW.canonical_event_id
            AND evidence_role!='aggregate'
        )
      )
    )
  )
BEGIN
  SELECT RAISE(ABORT, 'raw event pointer contradicts evidence');
END;
CREATE TRIGGER transactions_event_pointer_update
BEFORE UPDATE OF source_event_id, canonical_event_id ON transactions
WHEN NEW.canonical_event_id IS NOT NULL
  AND (
    NEW.source_event_id IS NULL
    OR (
      EXISTS (
        SELECT 1 FROM event_evidence
        WHERE source_event_id=NEW.source_event_id
      )
      AND (
        (SELECT COUNT(*) FROM event_evidence
         WHERE source_event_id=NEW.source_event_id) != 1
        OR NOT EXISTS (
          SELECT 1 FROM event_evidence
          WHERE source_event_id=NEW.source_event_id
            AND canonical_event_id=NEW.canonical_event_id
            AND evidence_role!='aggregate'
        )
      )
    )
  )
BEGIN
  SELECT RAISE(ABORT, 'raw event pointer contradicts evidence');
END;
CREATE TRIGGER trades_event_pointer_insert
BEFORE INSERT ON trades
WHEN NEW.canonical_event_id IS NOT NULL
  AND (
    NEW.source_event_id IS NULL
    OR (
      EXISTS (
        SELECT 1 FROM event_evidence
        WHERE source_event_id=NEW.source_event_id
      )
      AND (
        (SELECT COUNT(*) FROM event_evidence
         WHERE source_event_id=NEW.source_event_id) != 1
        OR NOT EXISTS (
          SELECT 1 FROM event_evidence
          WHERE source_event_id=NEW.source_event_id
            AND canonical_event_id=NEW.canonical_event_id
            AND evidence_role!='aggregate'
        )
      )
    )
  )
BEGIN
  SELECT RAISE(ABORT, 'raw event pointer contradicts evidence');
END;
CREATE TRIGGER trades_event_pointer_update
BEFORE UPDATE OF source_event_id, canonical_event_id ON trades
WHEN NEW.canonical_event_id IS NOT NULL
  AND (
    NEW.source_event_id IS NULL
    OR (
      EXISTS (
        SELECT 1 FROM event_evidence
        WHERE source_event_id=NEW.source_event_id
      )
      AND (
        (SELECT COUNT(*) FROM event_evidence
         WHERE source_event_id=NEW.source_event_id) != 1
        OR NOT EXISTS (
          SELECT 1 FROM event_evidence
          WHERE source_event_id=NEW.source_event_id
            AND canonical_event_id=NEW.canonical_event_id
            AND evidence_role!='aggregate'
        )
      )
    )
  )
BEGIN
  SELECT RAISE(ABORT, 'raw event pointer contradicts evidence');
END;
CREATE TRIGGER event_evidence_raw_pointer_insert
BEFORE INSERT ON event_evidence
WHEN EXISTS (
  SELECT 1 FROM transactions AS fact
  WHERE fact.source_event_id=NEW.source_event_id
    AND fact.canonical_event_id IS NOT NULL
    AND (
      NEW.evidence_role='aggregate'
      OR fact.canonical_event_id IS NOT NEW.canonical_event_id
    )
  UNION ALL
  SELECT 1 FROM trades AS fact
  WHERE fact.source_event_id=NEW.source_event_id
    AND fact.canonical_event_id IS NOT NULL
    AND (
      NEW.evidence_role='aggregate'
      OR fact.canonical_event_id IS NOT NEW.canonical_event_id
    )
)
BEGIN
  SELECT RAISE(ABORT, 'event evidence contradicts raw pointer');
END;
CREATE TRIGGER event_evidence_raw_pointer_update
BEFORE UPDATE OF canonical_event_id, source_event_id, evidence_role ON event_evidence
WHEN EXISTS (
  SELECT 1 FROM transactions AS fact
  WHERE fact.source_event_id=NEW.source_event_id
    AND fact.canonical_event_id IS NOT NULL
    AND (
      NEW.evidence_role='aggregate'
      OR fact.canonical_event_id IS NOT NEW.canonical_event_id
    )
  UNION ALL
  SELECT 1 FROM trades AS fact
  WHERE fact.source_event_id=NEW.source_event_id
    AND fact.canonical_event_id IS NOT NULL
    AND (
      NEW.evidence_role='aggregate'
      OR fact.canonical_event_id IS NOT NEW.canonical_event_id
    )
)
BEGIN
  SELECT RAISE(ABORT, 'event evidence contradicts raw pointer');
END;

-- Point-in-time position snapshots from a statement or live brokerage API (e.g. IBKR).
-- These are AS-REPORTED anchors. Calculations use a chosen snapshot and then apply later
-- canonical trades; they do not sum the snapshot independently or require trade history
-- from before the anchor date. A known-complete import writes explicit zero-quantity
-- anchors for omitted prior holdings; a partial source never treats omission as zero.
-- source_doc_id is NULL by design for live-API rows — the deliberate audit-trail
-- exception (no Drive document); see docs/live-sources.md. Idempotency is the UNIQUE key.
CREATE TABLE positions (
  id                  INTEGER PRIMARY KEY,
  account_id          INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  security_id         INTEGER NOT NULL REFERENCES securities(id) ON DELETE CASCADE,
  as_of               TEXT NOT NULL,                         -- ISO snapshot date
  quantity            REAL NOT NULL,                         -- fractional/signed; explicit 0 closes a complete snapshot
  avg_cost_minor      INTEGER,                               -- per-share cost basis, minor units of `currency`
  market_price_minor  INTEGER,                               -- per-share last price, minor units
  market_value_minor  INTEGER,                               -- position market value, minor units
  currency            TEXT NOT NULL,
  source_doc_id       INTEGER REFERENCES documents(id) ON DELETE SET NULL,  -- NULL for live-API rows
  UNIQUE(account_id, security_id, as_of)
);
CREATE INDEX idx_positions_account_date ON positions(account_id, as_of);

-- As with balances, multiple feeds may report the same canonical position anchor.
-- Keep every observation here and choose one position row for calculations.
CREATE TABLE position_evidence (
  id                       INTEGER PRIMARY KEY,
  canonical_position_id    INTEGER NOT NULL REFERENCES positions(id) ON DELETE CASCADE,
  account_feed_id          INTEGER REFERENCES account_feeds(id) ON DELETE SET NULL,
  source_doc_id            INTEGER REFERENCES documents(id) ON DELETE RESTRICT,
  source_key               TEXT NOT NULL,
  observed_quantity        REAL NOT NULL,
  observed_avg_cost_minor  INTEGER,
  observed_price_minor     INTEGER,
  observed_value_minor     INTEGER,
  observed_currency        TEXT NOT NULL,
  account_snapshot_complete INTEGER NOT NULL DEFAULT 0     -- 1 only when this read covered all holdings + cash
                              CHECK (account_snapshot_complete IN (0,1)),
  observed_at              TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX uq_position_evidence_source
  ON position_evidence(COALESCE(account_feed_id, 0), source_key);
CREATE INDEX idx_position_evidence_position ON position_evidence(canonical_position_id);

-- Snapshot evidence must come from a feed attached to the same logical account.
CREATE TRIGGER balance_evidence_same_account_insert
BEFORE INSERT ON balance_evidence
WHEN NEW.account_feed_id IS NOT NULL AND NOT EXISTS (
  SELECT 1 FROM balances AS anchor
  JOIN account_feeds AS feed ON feed.id=NEW.account_feed_id
  WHERE anchor.id=NEW.canonical_balance_id AND anchor.account_id=feed.account_id
)
BEGIN
  SELECT RAISE(ABORT, 'balance_evidence account mismatch');
END;
CREATE TRIGGER balance_evidence_same_account_update
BEFORE UPDATE OF canonical_balance_id, account_feed_id ON balance_evidence
WHEN NEW.account_feed_id IS NOT NULL AND NOT EXISTS (
  SELECT 1 FROM balances AS anchor
  JOIN account_feeds AS feed ON feed.id=NEW.account_feed_id
  WHERE anchor.id=NEW.canonical_balance_id AND anchor.account_id=feed.account_id
)
BEGIN
  SELECT RAISE(ABORT, 'balance_evidence account mismatch');
END;
CREATE TRIGGER position_evidence_same_account_insert
BEFORE INSERT ON position_evidence
WHEN NEW.account_feed_id IS NOT NULL AND NOT EXISTS (
  SELECT 1 FROM positions AS anchor
  JOIN account_feeds AS feed ON feed.id=NEW.account_feed_id
  WHERE anchor.id=NEW.canonical_position_id AND anchor.account_id=feed.account_id
)
BEGIN
  SELECT RAISE(ABORT, 'position_evidence account mismatch');
END;
CREATE TRIGGER position_evidence_same_account_update
BEFORE UPDATE OF canonical_position_id, account_feed_id ON position_evidence
WHEN NEW.account_feed_id IS NOT NULL AND NOT EXISTS (
  SELECT 1 FROM positions AS anchor
  JOIN account_feeds AS feed ON feed.id=NEW.account_feed_id
  WHERE anchor.id=NEW.canonical_position_id AND anchor.account_id=feed.account_id
)
BEGIN
  SELECT RAISE(ABORT, 'position_evidence account mismatch');
END;
CREATE TRIGGER account_feeds_retain_evidence
BEFORE DELETE ON account_feeds
WHEN EXISTS (SELECT 1 FROM source_events WHERE account_feed_id=OLD.id)
  OR EXISTS (SELECT 1 FROM balance_evidence WHERE account_feed_id=OLD.id)
  OR EXISTS (SELECT 1 FROM position_evidence WHERE account_feed_id=OLD.id)
BEGIN
  SELECT RAISE(ABORT, 'cannot delete account feed with evidence');
END;

-- Once evidence or a raw fact depends on an identity, changing its account/type
-- would silently make valid links contradictory. Reconciliation must instead
-- relink the dependent rows atomically before an obsolete identity is retired.
CREATE TRIGGER account_feeds_identity_update
BEFORE UPDATE OF account_id, provider, external_account_ref ON account_feeds
WHEN (NEW.account_id IS NOT OLD.account_id OR
      NEW.provider IS NOT OLD.provider OR
      NEW.external_account_ref IS NOT OLD.external_account_ref) AND (
  EXISTS (SELECT 1 FROM source_events WHERE account_feed_id=OLD.id) OR
  EXISTS (SELECT 1 FROM balance_evidence WHERE account_feed_id=OLD.id) OR
  EXISTS (SELECT 1 FROM position_evidence WHERE account_feed_id=OLD.id))
BEGIN
  SELECT RAISE(ABORT, 'account feed identity has dependent evidence');
END;
CREATE TRIGGER source_events_identity_update
BEFORE UPDATE OF account_feed_id, source_key, event_type ON source_events
WHEN (NEW.account_feed_id IS NOT OLD.account_feed_id OR
      NEW.source_key IS NOT OLD.source_key OR
      NEW.event_type IS NOT OLD.event_type) AND (
  EXISTS (SELECT 1 FROM event_evidence WHERE source_event_id=OLD.id) OR
  EXISTS (SELECT 1 FROM transactions WHERE source_event_id=OLD.id) OR
  EXISTS (SELECT 1 FROM trades WHERE source_event_id=OLD.id))
BEGIN
  SELECT RAISE(ABORT, 'source event identity has dependent evidence');
END;
CREATE TRIGGER canonical_events_identity_update
BEFORE UPDATE OF account_id, event_type ON canonical_events
WHEN (NEW.account_id IS NOT OLD.account_id OR NEW.event_type IS NOT OLD.event_type) AND (
  EXISTS (SELECT 1 FROM event_evidence WHERE canonical_event_id=OLD.id) OR
  EXISTS (SELECT 1 FROM transactions WHERE canonical_event_id=OLD.id) OR
  EXISTS (SELECT 1 FROM trades WHERE canonical_event_id=OLD.id))
BEGIN
  SELECT RAISE(ABORT, 'canonical event identity has dependent evidence');
END;
CREATE TRIGGER balances_identity_update
BEFORE UPDATE OF account_id, as_of, component ON balances
WHEN (NEW.account_id IS NOT OLD.account_id OR
      NEW.as_of IS NOT OLD.as_of OR
      NEW.component IS NOT OLD.component) AND EXISTS (
  SELECT 1 FROM balance_evidence WHERE canonical_balance_id=OLD.id)
BEGIN
  SELECT RAISE(ABORT, 'balance identity has dependent evidence');
END;
CREATE TRIGGER positions_identity_update
BEFORE UPDATE OF account_id, security_id, as_of ON positions
WHEN (NEW.account_id IS NOT OLD.account_id OR
      NEW.security_id IS NOT OLD.security_id OR
      NEW.as_of IS NOT OLD.as_of) AND EXISTS (
  SELECT 1 FROM position_evidence WHERE canonical_position_id=OLD.id)
BEGIN
  SELECT RAISE(ABORT, 'position identity has dependent evidence');
END;

-- Snapshot observations must cite either a document or a stable account feed.
-- Migration v9 assigns old un-attributed rows to a generic manual legacy feed.
CREATE TRIGGER balance_evidence_require_provenance_insert
BEFORE INSERT ON balance_evidence
WHEN NEW.account_feed_id IS NULL AND NEW.source_doc_id IS NULL
BEGIN
  SELECT RAISE(ABORT, 'balance_evidence requires provenance');
END;
CREATE TRIGGER balance_evidence_require_provenance_update
BEFORE UPDATE OF account_feed_id, source_doc_id ON balance_evidence
WHEN NEW.account_feed_id IS NULL AND NEW.source_doc_id IS NULL
BEGIN
  SELECT RAISE(ABORT, 'balance_evidence requires provenance');
END;
CREATE TRIGGER position_evidence_require_provenance_insert
BEFORE INSERT ON position_evidence
WHEN NEW.account_feed_id IS NULL AND NEW.source_doc_id IS NULL
BEGIN
  SELECT RAISE(ABORT, 'position_evidence requires provenance');
END;
CREATE TRIGGER position_evidence_require_provenance_update
BEFORE UPDATE OF account_feed_id, source_doc_id ON position_evidence
WHEN NEW.account_feed_id IS NULL AND NEW.source_doc_id IS NULL
BEGIN
  SELECT RAISE(ABORT, 'position_evidence requires provenance');
END;

-- Daily price history for valuation + benchmark comparison.
CREATE TABLE prices (
  security_id     INTEGER NOT NULL REFERENCES securities(id) ON DELETE CASCADE,
  date            TEXT NOT NULL,
  close_minor     INTEGER NOT NULL,
  currency        TEXT NOT NULL,
  PRIMARY KEY (security_id, date)
);

-- Append-only intraday observations used by the local live dashboard. Daily
-- closes remain in `prices`; these rows preserve what the provider reported at
-- each refresh without turning market data into financial evidence.
CREATE TABLE security_price_observations (
  id              INTEGER PRIMARY KEY,
  security_id     INTEGER NOT NULL REFERENCES securities(id) ON DELETE CASCADE,
  observed_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  price_minor     INTEGER NOT NULL CHECK (price_minor >= 0),
  currency        TEXT NOT NULL,
  source          TEXT NOT NULL CHECK (length(trim(source)) > 0),
  UNIQUE (security_id, observed_at, source)
);
CREATE INDEX idx_security_price_observations_latest
  ON security_price_observations(security_id, observed_at DESC);

-- Cached market distribution events. These are reference data for modeled
-- entitlement, not evidence that cash was received. A rational number preserves
-- sub-minor-unit per-share amounts exactly without storing money as REAL.
CREATE TABLE security_distributions (
  id                                         INTEGER PRIMARY KEY,
  security_id                                INTEGER NOT NULL REFERENCES securities(id) ON DELETE CASCADE,
  event_date                                 TEXT NOT NULL,
  amount_per_share_minor_numerator           INTEGER NOT NULL
                                               CHECK (amount_per_share_minor_numerator > 0),
  amount_per_share_minor_denominator         INTEGER NOT NULL
                                               CHECK (amount_per_share_minor_denominator > 0),
  currency                                   TEXT NOT NULL,
  source                                     TEXT NOT NULL CHECK (length(trim(source)) > 0),
  source_key                                 TEXT NOT NULL CHECK (length(trim(source_key)) > 0),
  observed_at                                TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (security_id, source, source_key)
);
CREATE INDEX idx_security_distributions_security_date
  ON security_distributions(security_id, event_date);

-- Per-security refresh state distinguishes a successful empty provider result
-- from a failed or never-attempted fetch. Failed refreshes retain older cached
-- events but remain visible as incomplete coverage to the renderer.
CREATE TABLE security_distribution_refreshes (
  security_id  INTEGER NOT NULL REFERENCES securities(id) ON DELETE CASCADE,
  source       TEXT NOT NULL CHECK (length(trim(source)) > 0),
  status       TEXT NOT NULL CHECK (status IN ('success','empty','failed')),
  observed_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (security_id, source)
);

-- FX rates for cross-currency reporting. Reporting currency is ILS by convention.
-- `source` tracks provenance: 'document' = extracted from a Drive doc (the user's
-- actual conversion rate, e.g. they manually converted USD at 3.3 ILS on a day
-- the market closed 3.28). 'yahoo' = market reference rate from the daily fetch.
-- Document rows are authoritative; the Yahoo refresh path must never overwrite them.
CREATE TABLE fx_rates (
  date            TEXT NOT NULL,
  base_currency   TEXT NOT NULL,                           -- e.g. 'USD'
  quote_currency  TEXT NOT NULL,                           -- e.g. 'ILS'
  rate            REAL NOT NULL,                           -- 1 base = `rate` quote
  source          TEXT NOT NULL DEFAULT 'yahoo',           -- 'document' | 'yahoo'
  PRIMARY KEY (date, base_currency, quote_currency)
);

-- Append-only FX observations for the local live dashboard. Document rates in
-- `fx_rates` remain authoritative for historical transactions.
CREATE TABLE fx_rate_observations (
  id              INTEGER PRIMARY KEY,
  base_currency   TEXT NOT NULL,
  quote_currency  TEXT NOT NULL,
  observed_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  rate            REAL NOT NULL CHECK (rate > 0),
  source          TEXT NOT NULL CHECK (length(trim(source)) > 0),
  CHECK (base_currency <> quote_currency),
  UNIQUE (base_currency, quote_currency, observed_at, source)
);
CREATE INDEX idx_fx_rate_observations_latest
  ON fx_rate_observations(base_currency, quote_currency, observed_at DESC);

-- Israeli תלוש משכורת with structured columns for the common bits.
-- Anything not in these columns goes into payslip_line_items.
CREATE TABLE payslips (
  id                            INTEGER PRIMARY KEY,
  period_start                  TEXT NOT NULL,             -- first day of pay period
  period_end                    TEXT NOT NULL,             -- last day of pay period
  paid_on                       TEXT,
  employer                      TEXT NOT NULL,
  gross_minor                   INTEGER NOT NULL,
  net_minor                     INTEGER NOT NULL,
  income_tax_minor              INTEGER NOT NULL DEFAULT 0,
  social_security_minor         INTEGER NOT NULL DEFAULT 0, -- ביטוח לאומי (employee portion)
  health_insurance_minor        INTEGER NOT NULL DEFAULT 0, -- ביטוח בריאות (employee portion)
  pension_employee_minor        INTEGER NOT NULL DEFAULT 0, -- ניכוי פנסיה - תגמולי עובד
  pension_employer_minor        INTEGER NOT NULL DEFAULT 0, -- הפרשת מעסיק פנסיה (תגמולי + פיצויים)
  study_fund_employee_minor     INTEGER NOT NULL DEFAULT 0,
  study_fund_employer_minor     INTEGER NOT NULL DEFAULT 0,
  currency                      TEXT NOT NULL DEFAULT 'ILS',
  source_doc_id                 INTEGER REFERENCES documents(id) ON DELETE SET NULL,
  UNIQUE(period_start, period_end, employer)
);
CREATE INDEX idx_payslips_period ON payslips(period_start);

-- Catch-all per-line for the long tail of payslip rows we don't model explicitly.
CREATE TABLE payslip_line_items (
  id              INTEGER PRIMARY KEY,
  payslip_id      INTEGER NOT NULL REFERENCES payslips(id) ON DELETE CASCADE,
  label           TEXT NOT NULL,                           -- e.g. 'מענק', 'החזר נסיעות', 'שעות נוספות'
  amount_minor    INTEGER NOT NULL,                        -- signed
  kind            TEXT NOT NULL CHECK (kind IN ('earning','deduction','info'))
);
CREATE INDEX idx_payslip_line_items_payslip ON payslip_line_items(payslip_id);

-- Migration v3 validates existing facts before upgrade and guards future writes.
-- Migration v4 extends those guards to canonical cash components.
-- Keep these triggers synchronized with findash_core/migrations.py.
CREATE TRIGGER validate_account_feeds_insert BEFORE INSERT ON account_feeds WHEN COALESCE(((length(trim(NEW.provider))>0)), 0)=0 BEGIN SELECT RAISE(ABORT, 'invalid account_feeds value'); END;
CREATE TRIGGER validate_account_feeds_update BEFORE UPDATE ON account_feeds WHEN COALESCE(((length(trim(NEW.provider))>0)), 0)=0 BEGIN SELECT RAISE(ABORT, 'invalid account_feeds value'); END;
CREATE TRIGGER validate_accounts_insert BEFORE INSERT ON accounts WHEN COALESCE(((typeof(NEW.currency)='text' AND NEW.currency GLOB '[A-Z][A-Z][A-Z]') AND (NEW.opened_on IS NULL OR (typeof(NEW.opened_on)='text' AND length(NEW.opened_on)=10 AND NEW.opened_on GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]' AND date(NEW.opened_on)=NEW.opened_on)) AND (NEW.closed_on IS NULL OR (typeof(NEW.closed_on)='text' AND length(NEW.closed_on)=10 AND NEW.closed_on GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]' AND date(NEW.closed_on)=NEW.closed_on)) AND (NEW.liquidity_date IS NULL OR (typeof(NEW.liquidity_date)='text' AND length(NEW.liquidity_date)=10 AND NEW.liquidity_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]' AND date(NEW.liquidity_date)=NEW.liquidity_date)) AND (NEW.opened_on IS NULL OR NEW.closed_on IS NULL OR NEW.closed_on>=NEW.opened_on)), 0)=0 BEGIN SELECT RAISE(ABORT, 'invalid accounts value'); END;
CREATE TRIGGER validate_accounts_update BEFORE UPDATE ON accounts WHEN COALESCE(((typeof(NEW.currency)='text' AND NEW.currency GLOB '[A-Z][A-Z][A-Z]') AND (NEW.opened_on IS NULL OR (typeof(NEW.opened_on)='text' AND length(NEW.opened_on)=10 AND NEW.opened_on GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]' AND date(NEW.opened_on)=NEW.opened_on)) AND (NEW.closed_on IS NULL OR (typeof(NEW.closed_on)='text' AND length(NEW.closed_on)=10 AND NEW.closed_on GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]' AND date(NEW.closed_on)=NEW.closed_on)) AND (NEW.liquidity_date IS NULL OR (typeof(NEW.liquidity_date)='text' AND length(NEW.liquidity_date)=10 AND NEW.liquidity_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]' AND date(NEW.liquidity_date)=NEW.liquidity_date)) AND (NEW.opened_on IS NULL OR NEW.closed_on IS NULL OR NEW.closed_on>=NEW.opened_on)), 0)=0 BEGIN SELECT RAISE(ABORT, 'invalid accounts value'); END;
CREATE TRIGGER validate_balance_evidence_insert BEFORE INSERT ON balance_evidence WHEN COALESCE(((typeof(NEW.observed_amount_minor)='integer') AND (typeof(NEW.observed_currency)='text' AND NEW.observed_currency GLOB '[A-Z][A-Z][A-Z]') AND (typeof(NEW.account_snapshot_complete)='integer' AND NEW.account_snapshot_complete IN (0,1))), 0)=0 BEGIN SELECT RAISE(ABORT, 'invalid balance_evidence value'); END;
CREATE TRIGGER validate_balance_evidence_update BEFORE UPDATE ON balance_evidence WHEN COALESCE(((typeof(NEW.observed_amount_minor)='integer') AND (typeof(NEW.observed_currency)='text' AND NEW.observed_currency GLOB '[A-Z][A-Z][A-Z]') AND (typeof(NEW.account_snapshot_complete)='integer' AND NEW.account_snapshot_complete IN (0,1))), 0)=0 BEGIN SELECT RAISE(ABORT, 'invalid balance_evidence value'); END;
CREATE TRIGGER validate_balances_insert BEFORE INSERT ON balances WHEN COALESCE(((typeof(NEW.as_of)='text' AND length(NEW.as_of)=10 AND NEW.as_of GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]' AND date(NEW.as_of)=NEW.as_of) AND (typeof(NEW.currency)='text' AND NEW.currency GLOB '[A-Z][A-Z][A-Z]') AND (typeof(NEW.amount_minor)='integer') AND (NEW.component IS NULL OR (typeof(NEW.component)='text' AND length(trim(NEW.component))>0))), 0)=0 BEGIN SELECT RAISE(ABORT, 'invalid balances value'); END;
CREATE TRIGGER validate_balances_update BEFORE UPDATE ON balances WHEN COALESCE(((typeof(NEW.as_of)='text' AND length(NEW.as_of)=10 AND NEW.as_of GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]' AND date(NEW.as_of)=NEW.as_of) AND (typeof(NEW.currency)='text' AND NEW.currency GLOB '[A-Z][A-Z][A-Z]') AND (typeof(NEW.amount_minor)='integer') AND (NEW.component IS NULL OR (typeof(NEW.component)='text' AND length(trim(NEW.component))>0))), 0)=0 BEGIN SELECT RAISE(ABORT, 'invalid balances value'); END;
CREATE TRIGGER validate_canonical_events_insert BEFORE INSERT ON canonical_events WHEN COALESCE(((typeof(NEW.occurred_on)='text' AND length(NEW.occurred_on)=10 AND NEW.occurred_on GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]' AND date(NEW.occurred_on)=NEW.occurred_on) AND (typeof(NEW.currency)='text' AND NEW.currency GLOB '[A-Z][A-Z][A-Z]') AND (NEW.amount_minor IS NULL OR (typeof(NEW.amount_minor)='integer')) AND (NEW.quantity IS NULL OR (typeof(NEW.quantity) IN ('integer','real') AND NEW.quantity>0 AND NEW.quantity<1e999)) AND (NEW.price_minor IS NULL OR (typeof(NEW.price_minor)='integer' AND NEW.price_minor>=0)) AND (typeof(NEW.fees_minor)='integer' AND NEW.fees_minor>=0) AND (NEW.component IS NULL OR (NEW.event_type='cash' AND typeof(NEW.component)='text' AND length(trim(NEW.component))>0))), 0)=0 BEGIN SELECT RAISE(ABORT, 'invalid canonical_events value'); END;
CREATE TRIGGER validate_canonical_events_update BEFORE UPDATE ON canonical_events WHEN COALESCE(((typeof(NEW.occurred_on)='text' AND length(NEW.occurred_on)=10 AND NEW.occurred_on GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]' AND date(NEW.occurred_on)=NEW.occurred_on) AND (typeof(NEW.currency)='text' AND NEW.currency GLOB '[A-Z][A-Z][A-Z]') AND (NEW.amount_minor IS NULL OR (typeof(NEW.amount_minor)='integer')) AND (NEW.quantity IS NULL OR (typeof(NEW.quantity) IN ('integer','real') AND NEW.quantity>0 AND NEW.quantity<1e999)) AND (NEW.price_minor IS NULL OR (typeof(NEW.price_minor)='integer' AND NEW.price_minor>=0)) AND (typeof(NEW.fees_minor)='integer' AND NEW.fees_minor>=0) AND (NEW.component IS NULL OR (NEW.event_type='cash' AND typeof(NEW.component)='text' AND length(trim(NEW.component))>0))), 0)=0 BEGIN SELECT RAISE(ABORT, 'invalid canonical_events value'); END;
CREATE TRIGGER validate_documents_insert BEFORE INSERT ON documents WHEN COALESCE(((NEW.doc_date IS NULL OR (typeof(NEW.doc_date)='text' AND length(NEW.doc_date)=10 AND NEW.doc_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]' AND date(NEW.doc_date)=NEW.doc_date))), 0)=0 BEGIN SELECT RAISE(ABORT, 'invalid documents value'); END;
CREATE TRIGGER validate_documents_update BEFORE UPDATE ON documents WHEN COALESCE(((NEW.doc_date IS NULL OR (typeof(NEW.doc_date)='text' AND length(NEW.doc_date)=10 AND NEW.doc_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]' AND date(NEW.doc_date)=NEW.doc_date))), 0)=0 BEGIN SELECT RAISE(ABORT, 'invalid documents value'); END;
CREATE TRIGGER validate_fx_rates_insert BEFORE INSERT ON fx_rates WHEN COALESCE(((typeof(NEW.date)='text' AND length(NEW.date)=10 AND NEW.date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]' AND date(NEW.date)=NEW.date) AND (typeof(NEW.base_currency)='text' AND NEW.base_currency GLOB '[A-Z][A-Z][A-Z]') AND (typeof(NEW.quote_currency)='text' AND NEW.quote_currency GLOB '[A-Z][A-Z][A-Z]') AND (typeof(NEW.rate) IN ('integer','real') AND NEW.rate>0 AND NEW.rate<1e999)), 0)=0 BEGIN SELECT RAISE(ABORT, 'invalid fx_rates value'); END;
CREATE TRIGGER validate_fx_rates_update BEFORE UPDATE ON fx_rates WHEN COALESCE(((typeof(NEW.date)='text' AND length(NEW.date)=10 AND NEW.date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]' AND date(NEW.date)=NEW.date) AND (typeof(NEW.base_currency)='text' AND NEW.base_currency GLOB '[A-Z][A-Z][A-Z]') AND (typeof(NEW.quote_currency)='text' AND NEW.quote_currency GLOB '[A-Z][A-Z][A-Z]') AND (typeof(NEW.rate) IN ('integer','real') AND NEW.rate>0 AND NEW.rate<1e999)), 0)=0 BEGIN SELECT RAISE(ABORT, 'invalid fx_rates value'); END;
CREATE TRIGGER validate_payslip_line_items_insert BEFORE INSERT ON payslip_line_items WHEN COALESCE(((typeof(NEW.amount_minor)='integer')), 0)=0 BEGIN SELECT RAISE(ABORT, 'invalid payslip_line_items value'); END;
CREATE TRIGGER validate_payslip_line_items_update BEFORE UPDATE ON payslip_line_items WHEN COALESCE(((typeof(NEW.amount_minor)='integer')), 0)=0 BEGIN SELECT RAISE(ABORT, 'invalid payslip_line_items value'); END;
CREATE TRIGGER validate_payslips_insert BEFORE INSERT ON payslips WHEN COALESCE(((typeof(NEW.period_start)='text' AND length(NEW.period_start)=10 AND NEW.period_start GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]' AND date(NEW.period_start)=NEW.period_start) AND (typeof(NEW.period_end)='text' AND length(NEW.period_end)=10 AND NEW.period_end GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]' AND date(NEW.period_end)=NEW.period_end) AND (NEW.period_end>=NEW.period_start) AND (NEW.paid_on IS NULL OR (typeof(NEW.paid_on)='text' AND length(NEW.paid_on)=10 AND NEW.paid_on GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]' AND date(NEW.paid_on)=NEW.paid_on)) AND (typeof(NEW.currency)='text' AND NEW.currency GLOB '[A-Z][A-Z][A-Z]') AND (typeof(NEW.gross_minor)='integer') AND (typeof(NEW.net_minor)='integer') AND (typeof(NEW.income_tax_minor)='integer') AND (typeof(NEW.social_security_minor)='integer') AND (typeof(NEW.health_insurance_minor)='integer') AND (typeof(NEW.pension_employee_minor)='integer') AND (typeof(NEW.pension_employer_minor)='integer') AND (typeof(NEW.study_fund_employee_minor)='integer') AND (typeof(NEW.study_fund_employer_minor)='integer')), 0)=0 BEGIN SELECT RAISE(ABORT, 'invalid payslips value'); END;
CREATE TRIGGER validate_payslips_update BEFORE UPDATE ON payslips WHEN COALESCE(((typeof(NEW.period_start)='text' AND length(NEW.period_start)=10 AND NEW.period_start GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]' AND date(NEW.period_start)=NEW.period_start) AND (typeof(NEW.period_end)='text' AND length(NEW.period_end)=10 AND NEW.period_end GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]' AND date(NEW.period_end)=NEW.period_end) AND (NEW.period_end>=NEW.period_start) AND (NEW.paid_on IS NULL OR (typeof(NEW.paid_on)='text' AND length(NEW.paid_on)=10 AND NEW.paid_on GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]' AND date(NEW.paid_on)=NEW.paid_on)) AND (typeof(NEW.currency)='text' AND NEW.currency GLOB '[A-Z][A-Z][A-Z]') AND (typeof(NEW.gross_minor)='integer') AND (typeof(NEW.net_minor)='integer') AND (typeof(NEW.income_tax_minor)='integer') AND (typeof(NEW.social_security_minor)='integer') AND (typeof(NEW.health_insurance_minor)='integer') AND (typeof(NEW.pension_employee_minor)='integer') AND (typeof(NEW.pension_employer_minor)='integer') AND (typeof(NEW.study_fund_employee_minor)='integer') AND (typeof(NEW.study_fund_employer_minor)='integer')), 0)=0 BEGIN SELECT RAISE(ABORT, 'invalid payslips value'); END;
CREATE TRIGGER validate_position_evidence_insert BEFORE INSERT ON position_evidence WHEN COALESCE(((typeof(NEW.observed_currency)='text' AND NEW.observed_currency GLOB '[A-Z][A-Z][A-Z]') AND (typeof(NEW.observed_quantity) IN ('integer','real') AND NEW.observed_quantity>-1e999 AND NEW.observed_quantity<1e999) AND (NEW.observed_avg_cost_minor IS NULL OR (typeof(NEW.observed_avg_cost_minor)='integer' AND NEW.observed_avg_cost_minor>=0)) AND (NEW.observed_price_minor IS NULL OR (typeof(NEW.observed_price_minor)='integer' AND NEW.observed_price_minor>=0)) AND (NEW.observed_value_minor IS NULL OR (typeof(NEW.observed_value_minor)='integer')) AND (typeof(NEW.account_snapshot_complete)='integer' AND NEW.account_snapshot_complete IN (0,1))), 0)=0 BEGIN SELECT RAISE(ABORT, 'invalid position_evidence value'); END;
CREATE TRIGGER validate_position_evidence_update BEFORE UPDATE ON position_evidence WHEN COALESCE(((typeof(NEW.observed_currency)='text' AND NEW.observed_currency GLOB '[A-Z][A-Z][A-Z]') AND (typeof(NEW.observed_quantity) IN ('integer','real') AND NEW.observed_quantity>-1e999 AND NEW.observed_quantity<1e999) AND (NEW.observed_avg_cost_minor IS NULL OR (typeof(NEW.observed_avg_cost_minor)='integer' AND NEW.observed_avg_cost_minor>=0)) AND (NEW.observed_price_minor IS NULL OR (typeof(NEW.observed_price_minor)='integer' AND NEW.observed_price_minor>=0)) AND (NEW.observed_value_minor IS NULL OR (typeof(NEW.observed_value_minor)='integer')) AND (typeof(NEW.account_snapshot_complete)='integer' AND NEW.account_snapshot_complete IN (0,1))), 0)=0 BEGIN SELECT RAISE(ABORT, 'invalid position_evidence value'); END;
CREATE TRIGGER validate_positions_insert BEFORE INSERT ON positions WHEN COALESCE(((typeof(NEW.as_of)='text' AND length(NEW.as_of)=10 AND NEW.as_of GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]' AND date(NEW.as_of)=NEW.as_of) AND (typeof(NEW.currency)='text' AND NEW.currency GLOB '[A-Z][A-Z][A-Z]') AND (typeof(NEW.quantity) IN ('integer','real') AND NEW.quantity>-1e999 AND NEW.quantity<1e999) AND (NEW.avg_cost_minor IS NULL OR (typeof(NEW.avg_cost_minor)='integer' AND NEW.avg_cost_minor>=0)) AND (NEW.market_price_minor IS NULL OR (typeof(NEW.market_price_minor)='integer' AND NEW.market_price_minor>=0)) AND (NEW.market_value_minor IS NULL OR (typeof(NEW.market_value_minor)='integer'))), 0)=0 BEGIN SELECT RAISE(ABORT, 'invalid positions value'); END;
CREATE TRIGGER validate_positions_update BEFORE UPDATE ON positions WHEN COALESCE(((typeof(NEW.as_of)='text' AND length(NEW.as_of)=10 AND NEW.as_of GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]' AND date(NEW.as_of)=NEW.as_of) AND (typeof(NEW.currency)='text' AND NEW.currency GLOB '[A-Z][A-Z][A-Z]') AND (typeof(NEW.quantity) IN ('integer','real') AND NEW.quantity>-1e999 AND NEW.quantity<1e999) AND (NEW.avg_cost_minor IS NULL OR (typeof(NEW.avg_cost_minor)='integer' AND NEW.avg_cost_minor>=0)) AND (NEW.market_price_minor IS NULL OR (typeof(NEW.market_price_minor)='integer' AND NEW.market_price_minor>=0)) AND (NEW.market_value_minor IS NULL OR (typeof(NEW.market_value_minor)='integer'))), 0)=0 BEGIN SELECT RAISE(ABORT, 'invalid positions value'); END;
CREATE TRIGGER validate_prices_insert BEFORE INSERT ON prices WHEN COALESCE(((typeof(NEW.date)='text' AND length(NEW.date)=10 AND NEW.date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]' AND date(NEW.date)=NEW.date) AND (typeof(NEW.currency)='text' AND NEW.currency GLOB '[A-Z][A-Z][A-Z]') AND (typeof(NEW.close_minor)='integer' AND NEW.close_minor>=0)), 0)=0 BEGIN SELECT RAISE(ABORT, 'invalid prices value'); END;
CREATE TRIGGER validate_prices_update BEFORE UPDATE ON prices WHEN COALESCE(((typeof(NEW.date)='text' AND length(NEW.date)=10 AND NEW.date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]' AND date(NEW.date)=NEW.date) AND (typeof(NEW.currency)='text' AND NEW.currency GLOB '[A-Z][A-Z][A-Z]') AND (typeof(NEW.close_minor)='integer' AND NEW.close_minor>=0)), 0)=0 BEGIN SELECT RAISE(ABORT, 'invalid prices value'); END;
CREATE TRIGGER validate_security_price_observations_insert BEFORE INSERT ON security_price_observations WHEN COALESCE(((typeof(NEW.price_minor)='integer' AND NEW.price_minor>=0) AND (typeof(NEW.currency)='text' AND NEW.currency GLOB '[A-Z][A-Z][A-Z]') AND (typeof(NEW.source)='text' AND length(trim(NEW.source))>0) AND (typeof(NEW.observed_at)='text' AND length(NEW.observed_at)=19 AND datetime(NEW.observed_at)=NEW.observed_at)), 0)=0 BEGIN SELECT RAISE(ABORT, 'invalid security_price_observations value'); END;
CREATE TRIGGER validate_security_price_observations_update BEFORE UPDATE ON security_price_observations WHEN COALESCE(((typeof(NEW.price_minor)='integer' AND NEW.price_minor>=0) AND (typeof(NEW.currency)='text' AND NEW.currency GLOB '[A-Z][A-Z][A-Z]') AND (typeof(NEW.source)='text' AND length(trim(NEW.source))>0) AND (typeof(NEW.observed_at)='text' AND length(NEW.observed_at)=19 AND datetime(NEW.observed_at)=NEW.observed_at)), 0)=0 BEGIN SELECT RAISE(ABORT, 'invalid security_price_observations value'); END;
CREATE TRIGGER validate_fx_rate_observations_insert BEFORE INSERT ON fx_rate_observations WHEN COALESCE(((typeof(NEW.base_currency)='text' AND NEW.base_currency GLOB '[A-Z][A-Z][A-Z]') AND (typeof(NEW.quote_currency)='text' AND NEW.quote_currency GLOB '[A-Z][A-Z][A-Z]') AND (NEW.base_currency<>NEW.quote_currency) AND (typeof(NEW.rate) IN ('integer','real') AND NEW.rate>0 AND NEW.rate<1e999) AND (typeof(NEW.source)='text' AND length(trim(NEW.source))>0) AND (typeof(NEW.observed_at)='text' AND length(NEW.observed_at)=19 AND datetime(NEW.observed_at)=NEW.observed_at)), 0)=0 BEGIN SELECT RAISE(ABORT, 'invalid fx_rate_observations value'); END;
CREATE TRIGGER validate_fx_rate_observations_update BEFORE UPDATE ON fx_rate_observations WHEN COALESCE(((typeof(NEW.base_currency)='text' AND NEW.base_currency GLOB '[A-Z][A-Z][A-Z]') AND (typeof(NEW.quote_currency)='text' AND NEW.quote_currency GLOB '[A-Z][A-Z][A-Z]') AND (NEW.base_currency<>NEW.quote_currency) AND (typeof(NEW.rate) IN ('integer','real') AND NEW.rate>0 AND NEW.rate<1e999) AND (typeof(NEW.source)='text' AND length(trim(NEW.source))>0) AND (typeof(NEW.observed_at)='text' AND length(NEW.observed_at)=19 AND datetime(NEW.observed_at)=NEW.observed_at)), 0)=0 BEGIN SELECT RAISE(ABORT, 'invalid fx_rate_observations value'); END;
CREATE TRIGGER validate_securities_insert BEFORE INSERT ON securities WHEN COALESCE(((typeof(NEW.currency)='text' AND NEW.currency GLOB '[A-Z][A-Z][A-Z]')), 0)=0 BEGIN SELECT RAISE(ABORT, 'invalid securities value'); END;
CREATE TRIGGER validate_securities_update BEFORE UPDATE ON securities WHEN COALESCE(((typeof(NEW.currency)='text' AND NEW.currency GLOB '[A-Z][A-Z][A-Z]')), 0)=0 BEGIN SELECT RAISE(ABORT, 'invalid securities value'); END;
CREATE TRIGGER validate_security_distributions_insert BEFORE INSERT ON security_distributions WHEN COALESCE(((typeof(NEW.event_date)='text' AND length(NEW.event_date)=10 AND NEW.event_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]' AND date(NEW.event_date)=NEW.event_date) AND (typeof(NEW.amount_per_share_minor_numerator)='integer' AND NEW.amount_per_share_minor_numerator>0) AND (typeof(NEW.amount_per_share_minor_denominator)='integer' AND NEW.amount_per_share_minor_denominator>0) AND (typeof(NEW.currency)='text' AND NEW.currency GLOB '[A-Z][A-Z][A-Z]') AND (typeof(NEW.source)='text' AND length(trim(NEW.source))>0) AND (typeof(NEW.source_key)='text' AND length(trim(NEW.source_key))>0) AND (typeof(NEW.observed_at)='text' AND length(NEW.observed_at)=19 AND datetime(NEW.observed_at)=NEW.observed_at)), 0)=0 BEGIN SELECT RAISE(ABORT, 'invalid security_distributions value'); END;
CREATE TRIGGER validate_security_distributions_update BEFORE UPDATE ON security_distributions WHEN COALESCE(((typeof(NEW.event_date)='text' AND length(NEW.event_date)=10 AND NEW.event_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]' AND date(NEW.event_date)=NEW.event_date) AND (typeof(NEW.amount_per_share_minor_numerator)='integer' AND NEW.amount_per_share_minor_numerator>0) AND (typeof(NEW.amount_per_share_minor_denominator)='integer' AND NEW.amount_per_share_minor_denominator>0) AND (typeof(NEW.currency)='text' AND NEW.currency GLOB '[A-Z][A-Z][A-Z]') AND (typeof(NEW.source)='text' AND length(trim(NEW.source))>0) AND (typeof(NEW.source_key)='text' AND length(trim(NEW.source_key))>0) AND (typeof(NEW.observed_at)='text' AND length(NEW.observed_at)=19 AND datetime(NEW.observed_at)=NEW.observed_at)), 0)=0 BEGIN SELECT RAISE(ABORT, 'invalid security_distributions value'); END;
CREATE TRIGGER validate_security_distribution_refreshes_insert BEFORE INSERT ON security_distribution_refreshes WHEN COALESCE(((typeof(NEW.source)='text' AND length(trim(NEW.source))>0) AND (typeof(NEW.status)='text' AND NEW.status IN ('success','empty','failed')) AND (typeof(NEW.observed_at)='text' AND length(NEW.observed_at)=19 AND datetime(NEW.observed_at)=NEW.observed_at)), 0)=0 BEGIN SELECT RAISE(ABORT, 'invalid security_distribution_refreshes value'); END;
CREATE TRIGGER validate_security_distribution_refreshes_update BEFORE UPDATE ON security_distribution_refreshes WHEN COALESCE(((typeof(NEW.source)='text' AND length(trim(NEW.source))>0) AND (typeof(NEW.status)='text' AND NEW.status IN ('success','empty','failed')) AND (typeof(NEW.observed_at)='text' AND length(NEW.observed_at)=19 AND datetime(NEW.observed_at)=NEW.observed_at)), 0)=0 BEGIN SELECT RAISE(ABORT, 'invalid security_distribution_refreshes value'); END;
CREATE TRIGGER validate_source_events_insert BEFORE INSERT ON source_events WHEN COALESCE(((typeof(NEW.occurred_on)='text' AND length(NEW.occurred_on)=10 AND NEW.occurred_on GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]' AND date(NEW.occurred_on)=NEW.occurred_on) AND (length(trim(NEW.source_key))>0)), 0)=0 BEGIN SELECT RAISE(ABORT, 'invalid source_events value'); END;
CREATE TRIGGER validate_source_events_update BEFORE UPDATE ON source_events WHEN COALESCE(((typeof(NEW.occurred_on)='text' AND length(NEW.occurred_on)=10 AND NEW.occurred_on GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]' AND date(NEW.occurred_on)=NEW.occurred_on) AND (length(trim(NEW.source_key))>0)), 0)=0 BEGIN SELECT RAISE(ABORT, 'invalid source_events value'); END;
CREATE TRIGGER validate_trades_insert BEFORE INSERT ON trades WHEN COALESCE(((typeof(NEW.date)='text' AND length(NEW.date)=10 AND NEW.date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]' AND date(NEW.date)=NEW.date) AND (typeof(NEW.currency)='text' AND NEW.currency GLOB '[A-Z][A-Z][A-Z]') AND (typeof(NEW.shares) IN ('integer','real') AND NEW.shares>0 AND NEW.shares<1e999) AND (typeof(NEW.price_minor)='integer' AND NEW.price_minor>=0) AND (typeof(NEW.fees_minor)='integer' AND NEW.fees_minor>=0)), 0)=0 BEGIN SELECT RAISE(ABORT, 'invalid trades value'); END;
CREATE TRIGGER validate_trades_update BEFORE UPDATE ON trades WHEN COALESCE(((typeof(NEW.date)='text' AND length(NEW.date)=10 AND NEW.date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]' AND date(NEW.date)=NEW.date) AND (typeof(NEW.currency)='text' AND NEW.currency GLOB '[A-Z][A-Z][A-Z]') AND (typeof(NEW.shares) IN ('integer','real') AND NEW.shares>0 AND NEW.shares<1e999) AND (typeof(NEW.price_minor)='integer' AND NEW.price_minor>=0) AND (typeof(NEW.fees_minor)='integer' AND NEW.fees_minor>=0)), 0)=0 BEGIN SELECT RAISE(ABORT, 'invalid trades value'); END;
CREATE TRIGGER validate_transactions_insert BEFORE INSERT ON transactions WHEN COALESCE(((typeof(NEW.date)='text' AND length(NEW.date)=10 AND NEW.date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]' AND date(NEW.date)=NEW.date) AND (NEW.value_date IS NULL OR (typeof(NEW.value_date)='text' AND length(NEW.value_date)=10 AND NEW.value_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]' AND date(NEW.value_date)=NEW.value_date)) AND (typeof(NEW.currency)='text' AND NEW.currency GLOB '[A-Z][A-Z][A-Z]') AND (typeof(NEW.amount_minor)='integer') AND (NEW.component IS NULL OR (typeof(NEW.component)='text' AND length(trim(NEW.component))>0))), 0)=0 BEGIN SELECT RAISE(ABORT, 'invalid transactions value'); END;
CREATE TRIGGER validate_transactions_update BEFORE UPDATE ON transactions WHEN COALESCE(((typeof(NEW.date)='text' AND length(NEW.date)=10 AND NEW.date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]' AND date(NEW.date)=NEW.date) AND (NEW.value_date IS NULL OR (typeof(NEW.value_date)='text' AND length(NEW.value_date)=10 AND NEW.value_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]' AND date(NEW.value_date)=NEW.value_date)) AND (typeof(NEW.currency)='text' AND NEW.currency GLOB '[A-Z][A-Z][A-Z]') AND (typeof(NEW.amount_minor)='integer') AND (NEW.component IS NULL OR (typeof(NEW.component)='text' AND length(trim(NEW.component))>0))), 0)=0 BEGIN SELECT RAISE(ABORT, 'invalid transactions value'); END;

INSERT INTO schema_migrations(version, name) VALUES
  (1, 'legacy_baseline'),
  (2, 'normalized_event_evidence'),
  (3, 'fact_validation'),
  (4, 'canonical_cash_component'),
  (5, 'evidence_account_guards'),
  (6, 'parent_identity_guards'),
  (7, 'complete_identity_guards'),
  (8, 'explicit_trade_cash_links'),
  (9, 'snapshot_provenance'),
  (10, 'accounting_integrity'),
  (11, 'ledger_relationship_integrity'),
  (12, 'security_distributions'),
  (13, 'market_observations');
