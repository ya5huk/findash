# Changelog

All notable changes to findash are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/); the project is versioned via the
plugin manifest (`.claude-plugin/plugin.json`).

## [Unreleased]

### Added
- **Ledger engine: typed intents are the only write path.** Claude describes what
  a source says — money (`cash`, `transfer`, `fx_conversion`, `trade`,
  `trade_settlement`, `dividend`, `confirm`, `exclude`) and reference data
  (`document`, `account`, `feed`, `security`, `price`, `meta`, `balance`,
  `position`, `snapshot`, `payslip`) — and `findash_db.py apply` derives and writes
  every row in one transaction. Plain SQL statements are gone; a logged `raw_sql`
  intent is the escape hatch, and the doctor flags its use.
- **Snapshots close what they omit.** A complete `snapshot` intent writes the zero
  anchors for previously held securities and cash currencies itself, and a
  disagreeing second source is rejected until one is chosen with `prefer`.
- **Transfers keep both legs.** A side not yet observed becomes an expected leg that
  does not count until its cash appears.
- **Conversions keep two rates.** `fx_conversions` stores the user's rate beside the
  day's market close, so the broker's markup is visible in the forex table.
- **Daily reconcile.** `findash_db.py reconcile` predicts dividends from ex-dates and
  holdings, confirms expected transfers, conversions and dividends that uniquely
  explain a brokerage cash change, and lists what is still open. Sync closes what it
  can and reports only items stuck for more than seven days.
- **Intraday price sessions on the dashboard.** `refresh_prices.py --intraday`
  (and the light `--intraday-only`) backfills the whole current session — pre- and
  post-market included — into `security_price_observations` at provider timestamps,
  and stores each session's boundaries so the dashboard sparklines distinguish a
  pre-market series from a regular one and stay complete even when the dashboard
  was just opened. The dashboard server runs the light refresh every 5 minutes
  between full 30-minute refreshes.

### Fixed
- **Cost basis survives a snapshot without average cost.** When the latest position
  snapshot omits `avg_cost`, the valuation keeps the most recent reported basis as
  long as the quantity is unchanged and no trade happened in between, instead of
  showing every holding's cost and gain as unavailable. `fetch-investments` now
  always sends `avg_cost` when the connector reports one.

### Removed
- **Single-purpose funding operations.** `infer-brokerage-funding` and
  `reconcile-funding-fx` (and their modules) are replaced by `transfer` and
  `fx_conversion` intents plus reconcile. Schema v16 moves their links into
  `transfer_links` and `fx_conversions`.

- **The Telegram snapshot path is gone.** `send-finance-dashboard`, `scripts/send_telegram.sh`,
  `scripts/render_dashboard.py`, `scripts/bundle-assets.py`, the `templates/` HTML/CSS/JS
  shell, `findash_core.dashboard_html`/`assets`/`script_json`, the `[telegram]` secrets
  section, and the accumulating `data/last_sync_summary.md` file. The static HTML
  renderer had fallen behind the normalized ledger; the live React dashboard is the only
  output. Sync reports its count-only summary on stdout and nowhere else.

### Changed
- **Licensed under MIT.** The repository was public but carried an all-rights-reserved
  notice; it is now genuinely open source.
- **Google Drive moved to the official Drive connector.** Skills prepare, list, pull
  from, and file into the vault through Anthropic's Google Drive connector (added in
  Claude, scoped to `[drive] root_folder_id`). `scripts/drive_root.py` is the only way
  skills learn the vault id, and `scripts/staging_files.py save-download` decodes a
  connector download into `inbox/staging/drive/` without the bytes crossing a shell
  command. No OAuth token lives in the checkout anymore.
- **Database backups are local.** `scripts/backup_database.py` archives the weekly
  consistent snapshot under `data/backups/` (newest eight kept) instead of uploading
  to Drive; the seven-day gate and integrity checks are unchanged.
- **Daily syncs run as Claude scheduled tasks** in the desktop app, inside the
  signed-in session where the Drive and IBKR connectors are available.
- **Sync and output are independent.** `sync-finance-data` now owns bank/card
  fetch, best-effort IBKR, Drive gathering, ingestion/reconciliation, market-cache
  refresh, Drive filing, and backup. The live React dashboard is opened separately.
- The former `daily-run` and `render-finance-dashboard` skills were removed.
- **Market refreshes keep both daily caches and append-only observations.** Daily
  `prices`/`fx_rates` remain the accounting baseline, while current observations
  preserve what each 30-minute local-dashboard refresh saw.
- **The React dashboard uses the design system's Chart.js visual language.** Net-worth, benchmark, funding, and expense views are responsive,
  animated, hoverable, and include 3M/YTD/1Y/All range controls where relevant.
- **Standalone marketplace installation is deferred.** FinDash currently relies on
  private state and mechanical scripts beside the checkout; clone it and launch with
  `claude --plugin-dir .` until a relocatable install path is implemented.
- **Local staging is the fetch→sync handoff.** `fetch-bank-data` stages its pairs in
  `inbox/staging/fetched/` and never writes to Drive; sync ingests everything staged
  and deletes files after commit. Fetched pairs get synthetic `documents` rows
  (`drive_id = 'local:fetch:<filename>:<sha256-first-12>'`, sidecar notes folded into
  `documents.notes`) — SQLite is their only persistence.
- **Every Drive touch is best-effort.** Sync gathers manual Drive drops into
  `inbox/staging/drive/<driveId>__<name>`, prepares missing fixed vault folders,
  and sorts direct `dump/` files after ingest. Moves never overwrite a destination,
  so collisions preserve both Drive identities. Drive failures degrade to a `⚠️`
  summary warning instead of failing the run.
- **XLSX ingestion is streaming and bounded.** Archive expansion, XML parts/depth,
  sheets, styles, rows, cells, text fragments, and emitted text all have explicit
  limits so a malformed import cannot build an unbounded XML tree or sparse result.
- **DB backup is weekly and gated.** The Drive backup (vault `finance.db` overwrite +
  timestamped `backups/` copy) fires only when the last success is >7 days old, tracked
  in the new `meta` table (`last_db_backup_at`). It now uploads a verified SQLite online
  snapshot instead of copying the live WAL database file. `fetch-investments` no longer
  pushes the DB to Drive.
- **Unattended runtime hardening.** Daily syncs are single-instance, each Claude attempt
  has a portable timeout, completion uses an explicit status marker, local financial
  files use user-only modes, and Chromium keeps its sandbox by default.
- **Bank scraper output stays private.** Raw provider responses are atomically captured
  in a mode-600 local staging file for the fetch skill to split; stdout and unattended
  logs contain source/count status only, and the capture is deleted after staging.
- **IBKR joined the full sync best-effort**, gated on an active
  `account_feeds.provider='ibkr'` mapping; unattended runs skip it with a warning since the
  connector authenticates only in interactive sessions.
- **One normalized valuation ledger now drives every dashboard total.** Snapshots anchor
  incomplete history, canonical events advance state, liabilities and every brokerage
  are valued once, partial sells keep remaining average-cost basis, signed external flows
  drive the benchmark, and headline/Overview reconciliation is enforced.
- **One logical account can have several evidence feeds.** Wrapper statements and their
  underlying provider API retain separate raw observations through `source_events` and
  `event_evidence`, while calculations count one confirmed canonical event.
- **Duplicate logical accounts now have one reviewed reconciliation path.** The fixed
  `findash_db.py reconcile-account` operation moves the whole owned graph atomically,
  requires explicit event/snapshot resolutions, preserves evidence, validates the final
  graph, and reports counts only. Normal write batches still cannot delete rows.
- **Ledger identities and provenance fail closed.** Linked feed/source/snapshot natural
  keys cannot be rewritten behind evidence; feed-backed trade ids are scoped by source
  event; and every snapshot observation cites a document or feed. Legacy unattributed
  snapshot rows receive a generic manual migration feed without changing their values.
- **Schema v11 makes normalized relationships self-consistent.** New source, canonical,
  evidence, and raw rows are written atomically; a raw canonical pointer must agree with
  its source's sole evidence link, which must be non-aggregate. Migration audits relationships
  by name/count only and restores lookup indexes missing from very old databases.
- **Posted brokerage cash is linked to trades explicitly.** Confirmed cash observations
  always count; `trade_cash_links` suppresses derived cash only for the reviewed fills it
  covers. Settlement must follow every covered fill and match signed trade economics
  within per-fill minor-unit rounding tolerance; linked economics are then immutable.
- **Income and spending timelines preserve missing pieces.** Salary credits and payslips
  reconcile one-to-one only on exact posting evidence, so unmatched jobs/bonuses survive;
  all-time expense charts retain covered months with zero consumer spending. Each summary
  column starts at its own real coverage boundary, so payslip-only or investment-history
  months no longer become invented zero-spending months.
- **Snapshot completeness is explicit.** Partial brokerage observations warn instead of
  pretending to be opening truth, while a reviewed complete empty-account snapshot is a
  valid zero anchor for holdings first acquired later.
- **Rendering is read-only.** Yahoo refresh is an explicit cache step, currency/quote-unit
  normalization is centralized, delivered diagnostics contain counts rather than raw
  financial rows, and modeled dividend entitlement remains separate from recorded cash.
- **Telegram summary restyle** — emoji-prefixed bullets, ≤10 words each, with a
  `## ⚠️ Warnings` section leading the summary on degraded runs.

### Removed
- **rclone and its wrapper.** `scripts/drive.sh`, `scripts/adopt_rclone_config.py`,
  `rclone.conf` handling in the doctor/setup skills, and the Drive `finance.db` /
  `backups/` uploads are gone; the vault's old copies are no longer maintained.
- **The headless daily-sync wrapper.** `scripts/run_daily.sh`, `scripts/run_sync.sh`,
  `scripts/run_with_timeout.py`, `scripts/install_launch_agent.py`, and the
  `com.findash.daily` LaunchAgent template were removed — a `claude -p` job cannot
  reach claude.ai connectors, so scheduling lives in the desktop app instead.

### Added
- A private localhost React dashboard and API, launched with
  `./run_dashboard.sh`. The server refreshes stock and FX references every
  30 minutes; the browser only reads a JSON snapshot and never writes SQLite.
- Schema v13 append-only `security_price_observations` and
  `fx_rate_observations` tables for current market history.
- `meta` table in `scripts/init-db.sql` (flow state; added idempotently to older DBs) —
  holds the backup gate (`last_db_backup_at`) plus a run journal (`last_sync_at`,
  `last_ingest_<source>_at`, `last_ibkr_at`) that feeds new doctor staleness warnings.
- `scripts/drive.sh` — the one front door for skill Drive access
  (`probe`/`ensure-layout`/`ls`/`pull`/`push`/`move`/`delete`); owns `rclone.conf` + the root-ID and fast-fails
  so a Drive outage degrades in seconds.
- Backup retention: after a successful weekly backup, sync prunes `gdrive:backups/` to
  the newest ~8 copies.

## [1.0.0] - 2026-06-21

findash is now packaged as a Claude Code **plugin**.

### Added
- **Plugin packaging** — `.claude-plugin/plugin.json` (name `findash`, so skills are
  invoked as `/findash:<skill>`) and `.claude-plugin/marketplace.json` so the repo is
  installable via `/plugin marketplace add ya5huk/findash`. Load locally by running
  Claude Code from the repo root with `claude --plugin-dir .`.
- **`daily-run` skill** — runs the whole morning flow end-to-end
  (fetch → sync → render → deliver to Telegram). This is what `scripts/run_daily.sh` now
  invokes; the orchestration that used to live as a long prompt inside the shell script
  now lives in the skill.
- **`setup` skill** — guided first-time onboarding: runs `findash-doctor`'s safe
  auto-fixes, then walks the user through the steps only a human can do (creating
  `.secrets/findash`, the rclone Google Drive OAuth, the bank-scraper browser seeding).
- **`fetch-investments` skill** — ingests your Interactive Brokers **trade history**
  straight into SQLite (deduped by `trades.external_id`), mapped onto a findash account you
  choose, plus a reconciliation positions snapshot and cash — all as a live API source
  (`source_doc_id = NULL`). IBKR auth uses Anthropic's official **Interactive Brokers
  connector**, added via Claude's connector directory, so it is **interactive-only** and
  is deliberately not part of the unattended `daily-run`. See `docs/live-sources.md`.
- Shared, unit-tested secret parsers: `scripts/lib/secrets.mjs` and
  `scripts/lib/findash_secrets.py`.

### Changed
- **Brokerage valuation is now data-driven.** The dashboard decides whether an account is
  trade-fed (valued from its `trades` ledger) or snapshot-fed (valued from a `positions`
  snapshot) by the data the account actually has, not a hardcoded account id — so any
  brokerage, including a trade-fed IBKR mapping, is valued once with no double-counting.
- **Skills moved** from `.claude/skills/` to `skills/` (the plugin layout); they are now
  namespaced `/findash:<skill>`.
- **`scripts/run_daily.sh`** now loads the plugin and runs
  `claude --plugin-dir "$REPO_ROOT" -p "/findash:daily-run"` (it `cd`s to the repo root
  first). The two-attempt retry and status checks are unchanged.
- Documentation (README, CLAUDE.md, AGENTS.md, `docs/`) rewritten to be plugin-centric.

### Removed
- **Multi-file secrets.** All credentials now live in a single `.secrets/findash` INI
  file with `[drive] [hapoalim] [cal] [telegram] [pdf-passwords]` sections. The previous
  per-file layout (`.secrets/drive`, `.secrets/hapoalim`, `.secrets/cal`,
  `.secrets/telegram`, `.secrets/pdf-passwords`) is **no longer read** — move each value
  into the matching section of `.secrets/findash` and delete the old files. (`rclone.conf`
  is unchanged — it remains rclone's own OAuth config and stays a separate file.)
- The internal `docs/fetch-bank-data-plan.md` planning document.
