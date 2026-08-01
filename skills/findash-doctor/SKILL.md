---
name: findash-doctor
description: Use when the user says "doctor", "finance doctor", "check finance setup", "what's missing", or any "is everything set up?" equivalent. Audits binaries, configs, secrets, npm deps, vendor assets, DB, live Drive connectivity, and Chromium profiles. Auto-fixes safe local gaps and prints exact commands for everything that needs human action.
---

# findash-doctor

You audit setup, fix what's safe, print a short scannable report. **Diagnose → auto-fix → report.** Anything needing the user goes into the report, not into a shell call.

## Where things live

- Run from the project root (the dir holding `CLAUDE.md`, `scripts/`, `templates/`, `docs/`). Paths below are relative.
- Drive root ID: `root_folder_id=…` in `.secrets/findash` under `[drive]` (chmod 600, gitignored).
- Seed script: `scripts/init-db.sql`. Vendor script: `scripts/bundle-assets.py`. npm manifest: `scripts/package.json`.

## Checks

| # | Category | What | Auto-fix? |
|---|---|---|---|
| 1 | Binaries | `qpdf`, `rclone`, `node ≥22.13.0`, `python3`, `sqlite3` on PATH | No |
| 2 | Dirs | `data/`, `inbox/staging/fetched/`, `inbox/staging/drive/`, `output/`, `.secrets/` exist | `mkdir -p` |
| 3 | rclone.conf | `./rclone.conf` exists + mode 600 | Perms; OR move `~/.config/rclone/rclone.conf` → `./rclone.conf` if local is absent |
| 4 | Drive root ID | `.secrets/findash` `[drive]` has a non-placeholder `root_folder_id=` | Perms only |
| 5 | Secrets | `.secrets/findash` exists + mode 600; note which `[sections]` are filled | Perms only |
| 6 | npm deps | `scripts/node_modules/` populated | `cd scripts && npm install` |
| 7 | Vendor | 3 files under `templates/vendor/` | `python3 scripts/bundle-assets.py` |
| 8 | DB | `data/finance.db` + has tables (incl. `positions`, `meta`) | `sqlite3 data/finance.db < scripts/init-db.sql` (fresh DB only) |
| 9 | Chromium profiles | `~/.cache/findash/chromium-profile/{hapoalim,visaCal}/` (only when matching secrets exist) | No |
| 10 | Drive live | `rclone lsf` against vault root succeeds in 5 s | No |
| 11 | IBKR connector | optional — the official Interactive Brokers connector is added in Claude (claude.ai), not findash config; interactive auth (unattended daily runs skip it) | No |

Node: parse `node --version`; major ≥ 23, or major = 22 with patch ≥ 22.13.0. Perms: anything with non-zero group/others digit is wrong → `chmod 600`. For Drive live: read `root_folder_id=…` from `.secrets/findash` `[drive]`; if it's missing or the value is empty/placeholder/<20 chars, skip the live call and let check #4 own it.

Positions + meta tables (#8): if `data/finance.db` exists but lacks the `positions` or `meta` table (an older install), **don't** re-run `init-db.sql` against a populated DB — its plain `CREATE TABLE`s will error. `positions` is added idempotently on the next `/findash:fetch-investments`, `meta` on the next `/findash:sync-finance-data` (or apply the matching `init-db.sql` block by hand); note under ⚠️ Optional, not a blocker. If `meta.last_db_backup_at` exists and is older than 14 days, add one ⚠️ line — the weekly Drive backup hasn't succeeded lately. Same idea for the run journal: for each configured source, one ⚠️ staleness line when its journal key is missing or >5 days old — `last_ingest_hapoalim_at`/`last_ingest_cal_at` (when the matching `[section]` exists in `.secrets/findash`), `last_ibkr_at` (when an `accounts.live_source='ibkr'` row exists), and `last_sync_at` >3 days regardless. IBKR connector (#11): purely informational and **optional** — the official Interactive Brokers connector is added by the user via Claude's connector directory, not by findash, and is interactive-only, so never treat its absence as an install error. If it's visible in this session (`/mcp` shows `Interactive Brokers (IBKR) · connected`), note ✓ OK; otherwise a one-line ⚠️ Optional pointer ("add it via Claude → Connectors if you want portfolio snapshots"). If `data/finance.db` exists and has the `live_source` column, also sanity-check the marker: `SELECT COUNT(*) FROM accounts WHERE live_source='ibkr'` should be 0 or 1, and a marked account must not have `closed_on` set — more than one marked row, or a closed marked row, is a ⚠️ with a one-line explanation (`fetch-investments` refuses to run until it's resolved); zero rows just means `fetch-investments` hasn't run yet. Never print balances, account ids, or positions (principle #1).

## Auto-fix order

1. `mkdir -p data inbox/staging/fetched inbox/staging/drive output .secrets`
2. If `./rclone.conf` is missing but `~/.config/rclone/rclone.conf` exists (rclone's default XDG path — what plain `rclone config` writes to), move it: `mv ~/.config/rclone/rclone.conf ./rclone.conf`. Then re-check perms in step 3. (Not a symlink — the project owns this file.)
3. `chmod 600` `rclone.conf` and `.secrets/findash` if either exists and is looser than 600
4. Init DB if missing/empty (needs `sqlite3`)
5. `(cd scripts && npm install)` if `node_modules/` empty (needs valid `node`; stream stderr)
6. `python3 scripts/bundle-assets.py` if any vendor file missing (needs `python3`). `SSL: CERTIFICATE_VERIFY_FAILED` should no longer occur: the HTTPS-fetching scripts point a cert-less Python at the system CA bundle themselves (`scripts/lib/ssl_bootstrap.py`). If it appears anyway, no known system bundle exists on this machine - fix and retry once: `pip3 install --upgrade certifi` then run the Python.org cert installer for the active version (`/Applications/Python\ <MAJOR.MINOR>/Install\ Certificates.command`). If the retry succeeds, list the cert fix and the asset bundle as two separate ✅ Fixed bullets. If it still fails, that's a 🚫 Blocker.

Re-check after each fix so the report shows post-fix state. **Don't** auto-install OS-level binaries (Homebrew / apt), touch nvm, generate creds, run `rclone config`, or run anything `--setup`. (Pip / npm / vendored asset downloads are fine — they're local, idempotent, and the whole point of auto-fix.)

If the Drive live check's preconditions don't hold (rclone or rclone.conf missing), report `skipped — <reason>`, not a fake OAuth error.

## Blocker vs Optional

- **Blocker** = the local pipeline literally can't run. (`python3`, `sqlite3`, vendor assets, DB init; `node`/npm deps when bank creds exist; `qpdf` when unsure.)
- **Optional** = one feature degrades. (`rclone` / `rclone.conf` / Drive root ID / Drive-live — Drive down just means manual-drop ingest pauses and the weekly backup skips, with `⚠️` warnings; bank fetch + dashboard still flow. Likewise the `[telegram]`, `[pdf-passwords]`, `[hapoalim]`, `[cal]` sections of `.secrets/findash` individually, chromium profile when its source's secrets are absent, and the IBKR connector when not added/connected — gates the investments feed.)
- **Promotion:** chromium profile for a source becomes a blocker the moment that source's credentials land (a `[<source>]` section in `.secrets/findash`) — creds without a trusted cookie → SMS challenge.

## Report format

Emojis, compact, sections only when non-empty. Today's date.

```
🩺 findash — <YYYY-MM-DD>

✅ Fixed
  <comma-separated short phrases on one or two wrapped lines>

🚫 Blockers
  • <thing> — <one-line fix>

⚠️ Optional   (skip if you don't use that feature)
  • <thing> — <what it gates>

✓ OK
  <comma-separated inline list, wrap ~80 cols>
```

Drop the ✅ block if nothing was fixed. If zero blockers AND zero optional gaps, the whole report collapses to:

```
🩺 findash — all systems go.
```

Remediation = exact shell command when possible. Detect OS (`uname -s` → Darwin / Linux) and match install hints (`brew install …` vs `sudo apt install -y …`). For OAuth / `--setup`, name the exact command.

Example on a partially-set-up macOS box:

```
🩺 findash — 2026-05-27

✅ Fixed
  inbox/staging/, output/ created • .secrets/findash chmod 600 (was 644)

⚠️ Optional
  • rclone — `brew install rclone` (Drive: manual-drop ingest + weekly backup)
  • Drive vault — connect failed (5s); `rclone --config ./rclone.conf config reconnect gdrive:`
  • .secrets/findash [cal] — only for fetching credit-card data
  • chromium-profile/visaCal — not blocking yet (no [cal] creds)
  • IBKR connector — not connected; add it via Claude → Connectors for portfolio snapshots

✓ OK
  qpdf, python3 3.13, node 22.14.0, sqlite3, rclone.conf, Drive root ID,
  .secrets/findash ([hapoalim] [pdf-passwords]), finance.db (12 MB), node_modules/, vendor/
```

## Judgment

- **Don't lie about fixes.** ✅ Fixed = you ran it AND re-checked. A failed attempt goes into 🚫 Blockers with the real error.
- **Diagnose top-down.** If `rclone` is missing, the Drive-live line says "skipped — rclone missing", not "OAuth expired".
- **Idempotent.** Second run on a healthy machine = one line. Only list under ✅ Fixed what you actually changed this run.
- **Trust reality over CLAUDE.md.** If setup notes say "npm install" but `scripts/package.json` is missing, flag the missing manifest.
- **No new categories silently.** Surprise findings go under a `🔍 Note` line, not into the matrix.
