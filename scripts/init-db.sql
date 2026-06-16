-- findash schema (v1)
--
-- Conventions:
--   * All monetary amounts as INTEGER in minor units (agorot for ILS, cents for USD).
--     Multiply by 100 on the way in, divide by 100 on the way out. No REAL for money.
--   * All dates as TEXT in ISO 8601 (YYYY-MM-DD). Timestamps as YYYY-MM-DD HH:MM:SS.
--   * UTF-8 by default; Hebrew text stored as-is.
--   * Foreign keys ON, fail loud on orphan rows.
--   * "component" is the only un-obvious column: NULL for plain accounts; for pension
--     balances/transactions: 'tagmul_employee' | 'tagmul_employer' | 'pitsuyim';
--     for study fund: 'tagmul_employee' | 'tagmul_employer'.

PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

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

-- Anything that holds value. Includes credit cards (liability; treat balance as negative).
CREATE TABLE accounts (
  id                INTEGER PRIMARY KEY,
  name              TEXT NOT NULL UNIQUE,                  -- human-readable
  kind              TEXT NOT NULL,                         -- checking | savings | brokerage | credit_card | pension | study_fund | cash | other
  institution       TEXT NOT NULL,                         -- 'Bank Hapoalim', 'Harel', 'Excellence', 'Cash', ...
  external_id       TEXT,                                  -- account/policy number at institution
  currency          TEXT NOT NULL,                         -- ILS, USD, ...
  opened_on         TEXT,
  closed_on         TEXT,
  investment_track  TEXT,                                  -- e.g. 'S&P 500 עוקב מדד' for pension/study_fund/brokerage
  liquidity_date    TEXT,                                  -- when funds become accessible (training fund, deposits)
  notes             TEXT
);
CREATE INDEX idx_accounts_kind ON accounts(kind);
CREATE INDEX idx_accounts_institution ON accounts(institution);

-- Point-in-time balance snapshots. Pension/study-fund have multiple rows per snapshot
-- (one per component); plain accounts have one row with component=NULL.
CREATE TABLE balances (
  id              INTEGER PRIMARY KEY,
  account_id      INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  as_of           TEXT NOT NULL,                           -- ISO date
  component       TEXT,                                    -- NULL | tagmul_employee | tagmul_employer | pitsuyim
  amount_minor    INTEGER NOT NULL,
  currency        TEXT NOT NULL,
  source_doc_id   INTEGER REFERENCES documents(id) ON DELETE SET NULL,
  UNIQUE(account_id, as_of, component)
);
CREATE INDEX idx_balances_account_date ON balances(account_id, as_of);

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
  counterparty    TEXT,                                    -- 'Excellence Investments', 'Sling', family name, etc.
  reference       TEXT,                                    -- אסמכתא; helps with dedup across docs
  description     TEXT,                                    -- free text from source doc
  source_doc_id   INTEGER REFERENCES documents(id) ON DELETE SET NULL
);
CREATE INDEX idx_transactions_account_date ON transactions(account_id, date);
CREATE INDEX idx_transactions_category ON transactions(category);
CREATE INDEX idx_transactions_reference ON transactions(reference);

-- Master list of tradables; includes benchmarks like SPY for the comparison chart.
CREATE TABLE securities (
  id              INTEGER PRIMARY KEY,
  ticker          TEXT NOT NULL UNIQUE,
  name            TEXT,
  asset_class     TEXT,                                    -- stock | etf | mutual_fund | bond | crypto | benchmark
  currency        TEXT NOT NULL
);

-- Buy/sell events. Positions are derived: SUM(shares * sign) per (account, security).
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
  source_doc_id   INTEGER REFERENCES documents(id) ON DELETE SET NULL
);
CREATE INDEX idx_trades_account_security ON trades(account_id, security_id);
CREATE INDEX idx_trades_date ON trades(date);

-- Daily price history for valuation + benchmark comparison.
CREATE TABLE prices (
  security_id     INTEGER NOT NULL REFERENCES securities(id) ON DELETE CASCADE,
  date            TEXT NOT NULL,
  close_minor     INTEGER NOT NULL,
  currency        TEXT NOT NULL,
  PRIMARY KEY (security_id, date)
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
