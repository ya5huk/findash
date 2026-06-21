# Changelog

All notable changes to findash are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/); the project is versioned via the
plugin manifest (`.claude-plugin/plugin.json`).

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
- Shared, unit-tested secret parsers: `scripts/lib/secrets.mjs` and
  `scripts/lib/findash_secrets.py`.

### Changed
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
