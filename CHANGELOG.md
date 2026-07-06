# Changelog

All notable changes to findash are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/); the project is versioned via the
plugin manifest (`.claude-plugin/plugin.json`).

## [Unreleased]

### Changed
- **Local staging is the fetch→sync handoff.** `fetch-bank-data` stages its pairs in
  `inbox/staging/fetched/` and never writes to Drive; sync ingests everything staged
  and deletes files after commit. Fetched pairs get synthetic `documents` rows
  (`drive_id = 'local:fetch:<filename>:<sha256-first-12>'`, sidecar notes folded into
  `documents.notes`) — SQLite is their only persistence.
- **Every Drive touch is best-effort.** Sync gathers manual Drive drops into
  `inbox/staging/drive/<driveId>__<name>`, sorts `dump/` drops after ingest (deferred +
  retried via `documents.drive_path` when Drive is down), and a Drive outage now degrades
  to a `⚠️` Telegram warning instead of failing the run.
- **DB backup is weekly and gated.** The Drive backup (vault `finance.db` overwrite +
  timestamped `backups/` copy) fires only when the last success is >7 days old, tracked
  in the new `meta` table (`last_db_backup_at`). `fetch-investments` no longer pushes
  the DB to Drive.
- **IBKR joined `daily-run` best-effort**, gated on prior onboarding
  (`accounts.live_source='ibkr'`); unattended runs skip it with a warning since the
  connector authenticates only in interactive sessions.
- **Telegram summary restyle** — emoji-prefixed bullets, ≤10 words each, with a
  `## ⚠️ Warnings` section leading the summary on degraded runs.

### Added
- `meta` table in `scripts/init-db.sql` (flow state; added idempotently to older DBs) —
  holds the backup gate (`last_db_backup_at`) plus a run journal (`last_sync_at`,
  `last_ingest_<source>_at`, `last_ibkr_at`) that feeds new doctor staleness warnings.
- `scripts/drive.sh` — the one front door for skill Drive access
  (`ls`/`pull`/`push`/`move`/`delete`); owns `rclone.conf` + the root-ID and fast-fails
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
