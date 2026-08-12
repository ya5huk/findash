---
name: findash-doctor
description: Use when the user says "doctor", "finance doctor", "check finance setup", "what's missing", or any "is everything set up?" equivalent. Audits binaries, configs, secrets, npm deps, vendor assets, DB, live Drive connectivity, and Chromium profiles. Auto-fixes safe local gaps and prints exact commands for everything that needs human action.
---

# findash-doctor

You audit setup, fix what's safe, print a short scannable report. **Diagnose → auto-fix → report.** Anything needing the user goes into the report, not into a shell call.

## Where things live

- Run from the project root (the dir holding `CLAUDE.md`, `scripts/`, `templates/`, `docs/`). Paths below are relative.
- Drive root ID: `root_folder_id=…` in `.secrets/findash` under `[drive]` (chmod 600, gitignored).
- Seed script: `scripts/init-db.sql`. Vendor script: `scripts/bundle-assets.py`. npm manifests: `scripts/package.json` (bank fetch) and `dashboard/package.json` (React app).

## Checks

| # | Category | What | Auto-fix? |
|---|---|---|---|
| 1 | Binaries | Python ≥3.10; feature-gated `qpdf`, `rclone`, and Node ≥22.13.0 | No |
| 2 | Private state | local dirs exist at mode 700; files under `data/`, `inbox/`, `output/`, `.secrets/`, plus `rclone.conf` and browser-profile files, are mode 600 | `python3 scripts/harden_permissions.py --include-browser-profile` |
| 3 | rclone.conf | when Drive is configured or explicitly selected: `./rclone.conf` exists + mode 600 | Perms; OR safely copy `~/.config/rclone/rclone.conf` when local is absent and Drive is opted into |
| 4 | Drive root ID | `.secrets/findash` `[drive]` has a non-placeholder `root_folder_id=` | Perms only |
| 5 | Secrets | `.secrets/findash` exists + mode 600; note which `[sections]` are filled | Perms only |
| 6 | Bank npm deps | when `[hapoalim]` or `[cal]` is configured: `scripts/node_modules/` populated and `npm ls --omit=dev` succeeds | `scripts/install_node_deps.sh` |
| 7 | Vendor | all 3 files pass `python3 scripts/bundle-assets.py --check` | `python3 scripts/bundle-assets.py` |
| 8 | DB | `data/finance.db` exists, passes integrity/FK checks, and is at the current schema version | `python3 scripts/migrate_db.py --init` when missing; `python3 scripts/migrate_db.py` when outdated |
| 9 | Chromium profiles | `~/.cache/findash/chromium-profile/{hapoalim,visaCal}/` (only when matching secrets exist) | No |
| 10 | Drive live + layout | `scripts/drive.sh probe` reaches the configured vault, then `scripts/drive.sh ensure-layout` creates/validates every fixed public folder without listing private names | Fixed folders only |
| 11 | IBKR connector | optional — the official Interactive Brokers connector is added in Claude (claude.ai), not findash config; interactive auth (unattended syncs skip it) | No |
| 12 | React dashboard | `dashboard/node_modules/` populated and `npm --prefix dashboard run build` succeeds | `scripts/install_dashboard_deps.sh` |

Run `python3 scripts/check_config.py` for checks 4–6 and the optional-integration
gates. Its output contains booleans and known section names only—never values.
A section is configured only when every required value is non-empty and not a
setup placeholder; a header by itself does not count.

Versions: parse `node --version` (major ≥23, or major=22 with minor ≥13) and `python3 --version` (≥3.10). Perms: anything with non-zero group/others digit is wrong. For Drive live, run `scripts/drive.sh probe`; if it succeeds, run `scripts/drive.sh ensure-layout`. If the root ID is missing or still a placeholder, skip both and let check #4 own it. Never invoke raw `rclone` or print a vault listing.

Database check (#8): run `python3 scripts/migrate_db.py --check`. A missing database is initialized with `--init`; an outdated populated database is upgraded by running `python3 scripts/migrate_db.py`. Never apply ad-hoc `ALTER TABLE` statements. A migration that reports conflicting same-date balances is a blocker requiring source reconciliation—never pick a value automatically. Verify `PRAGMA foreign_key_check` and `PRAGMA integrity_check` through `scripts/findash_db.py`. If `meta.last_db_backup_at` is older than 14 days, add one ⚠️ line. Apply the same staleness checks to configured sources, using active `account_feeds.provider='ibkr'` rows to decide whether IBKR is onboarded. Every active IBKR feed must point to a non-closed logical account.

Also audit logical-account identity: compare active brokerage accounts' latest holdings and surrounding canonical trades, especially when one account has an IBKR feed. Substantially identical portfolios may be a wrapper and its underlying IBKR account represented twice. Treat a clear duplicate as an accuracy blocker for an interactive `/findash:fetch-investments` reconciliation; never sum it, auto-merge it, or print the matching securities/quantities. Similar amounts or one matching trade are not enough evidence. IBKR connector (#11) remains optional and interactive. Never print account ids, balances, positions, source identifiers, or counterparties.

Audit bank-account transitions too. Multiple same-institution checking rows are an accuracy blocker when their activity does not economically overlap, an exact closing/opening transfer joins their boundary, or pre-boundary manual snapshots on the successor track the predecessor ledger. Treat a bank-renumbering migration as one logical account with separate feeds and recommend the reviewed `reconcile-account` flow. Do not infer identity from a similar balance alone, and never print the private comparison evidence.

Count legacy brokerage `transactions` with `flow_type='investment'` that still lack normalized canonical evidence. Report only the count as a migration warning: where those rows overlap raw trade history, they cannot prove which trade cash they duplicate, so the renderer preserves the trade-derived legacy treatment and warns. Reconcile them interactively into canonical cash/trade events and add `trade_cash_links` only from full source evidence; never auto-match by amount/date or print row details.

## Auto-fix order

1. `python3 scripts/harden_permissions.py --include-browser-profile` — creates the required private directories and fixes modes without printing private filenames. A refused symlink is a blocker that needs manual inspection; never follow it.
2. Only when `[drive]` is configured with a usable `root_folder_id`, or the user explicitly opted into Drive during the current setup conversation: if `./rclone.conf` is missing but `~/.config/rclone/rclone.conf` exists (rclone's default XDG path — what plain `rclone config` writes to), run `python3 scripts/adopt_rclone_config.py`. It copies atomically at mode 600 and preserves the original for other rclone uses; it refuses symlinks and never overwrites an existing project config. Without that Drive opt-in, do not inspect or copy the global rclone config.
3. Re-run `python3 scripts/harden_permissions.py --include-browser-profile` after copying `rclone.conf`.
4. When the Drive preconditions hold and `scripts/drive.sh probe` succeeds, run `scripts/drive.sh ensure-layout`. It idempotently creates only the fixed public folder names from `docs/drive-layout.md`; it never prints or inspects private filenames. Report those folders as fixed only when the command succeeds.
5. Run `python3 scripts/migrate_db.py --init` if the DB is missing/empty; otherwise run `python3 scripts/migrate_db.py` to apply pending migrations transactionally.
6. Only when `[hapoalim]` or `[cal]` credentials are configured, run `scripts/install_node_deps.sh` when `node_modules/` is missing or `(cd scripts && npm ls --omit=dev)` fails (needs valid Node/npm; the wrapper uses `npm ci` with `package-lock.json`; stream stderr). Do not download Puppeteer/Chromium for users who did not opt into automatic bank fetching.
7. Run `scripts/install_dashboard_deps.sh` when `dashboard/node_modules/` is missing or `npm --prefix dashboard ls` fails, then verify with `npm --prefix dashboard run build`.
8. Run `python3 scripts/bundle-assets.py --check`. If any asset is missing or fails its pinned hash, run `python3 scripts/bundle-assets.py`, then re-run `--check` (needs `python3`). On macOS, an `SSL: CERTIFICATE_VERIFY_FAILED` is a blocker with the Python.org installer remediation (`/Applications/Python\ <MAJOR.MINOR>/Install\ Certificates.command`); do not mutate the user's global/user Python packages automatically.

Re-check after each fix so the report shows post-fix state. **Don't** auto-install OS-level binaries (Homebrew / apt), touch nvm, generate creds, run `rclone config`, or run anything `--setup`. (Pip / npm / vendored asset downloads are fine — they're local, idempotent, and the whole point of auto-fix.)

If the Drive live check's preconditions don't hold (rclone or rclone.conf missing), report `skipped — <reason>`, not a fake OAuth error. If the private probe succeeds but `ensure-layout` fails, report `Drive vault layout — could not prepare fixed folders`; do not call that an outage or OAuth failure.

## Blocker vs Optional

- **Blocker** = the local pipeline literally can't run. (`python3`, DB init/migration; `node`/dashboard npm deps for the live app; bank npm deps when bank creds exist; vendor assets for Telegram HTML; `qpdf` when protected payslips are configured.)
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
  qpdf, python3 3.13, node 22.14.0, rclone.conf, Drive root ID,
  .secrets/findash ([hapoalim] [pdf-passwords]), finance.db (12 MB), node_modules/, vendor/
```

## Judgment

- **Don't lie about fixes.** ✅ Fixed = you ran it AND re-checked. A failed attempt goes into 🚫 Blockers with the real error.
- **Diagnose top-down.** If `rclone` is missing, the Drive-live line says "skipped — rclone missing", not "OAuth expired".
- **Idempotent.** Second run on a healthy machine = one line. Only list under ✅ Fixed what you actually changed this run.
- **Trust reality over setup prose.** If `scripts/package.json` or its lockfile is missing, flag the missing manifest instead of attempting an install.
- **No new categories silently.** Surprise findings go under a `🔍 Note` line, not into the matrix.
