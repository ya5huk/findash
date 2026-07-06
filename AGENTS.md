# findash

Personal finance system, packaged as a Claude Code **plugin** (`findash`). Drive vault + local staging → SQLite → HTML dashboard. Skills do the work — `fetch-bank-data` pulls fresh transactions from Hapoalim + Cal and stages them in local `inbox/staging/fetched/` (never Drive); `fetch-investments` pulls live Interactive Brokers trades straight into SQLite (best-effort in the daily flow; interactive auth); `sync-finance-data` gathers new Drive drops into staging (best-effort) and ingests everything staged into SQLite, then backs the DB up to Drive weekly; `render-finance-dashboard` reads SQLite into the HTML. `daily-run` chains the steps for the morning flow (it's what the unattended wrapper invokes), `setup` handles first-time onboarding, and `findash-doctor` audits the install. Every Drive touch is best-effort — failures degrade to `⚠️` Telegram warnings, never an abort. Everything else is data, templates, and documentation.

Skills are namespaced by the plugin, so they're invoked as `/findash:<skill>`. Load the plugin by running from the repo root with `--plugin-dir .` (the cron wrapper passes the same flag).

## First principles

These shape every decision in this project. Re-read them when you're about to write code.

1. **Public-project hygiene.** This repository is public. Keep committed code, docs, examples, and prompts generalized so they work for any user. Never include a real user's account numbers, balances, transaction amounts, counterparties, credentials, tokens, Drive IDs, personal identifiers, or local secret values. Provider names are fine when documenting supported integrations; user-specific financial details are not. Never print, paste, upload, or otherwise share secrets from `.secrets/`, `rclone.conf`, local databases, downloaded statements, or generated dashboards.

2. **Judgment over scripts.** Codex's reasoning is the asset, not a parser script. Don't write categorization rules, don't pattern-match filenames mechanically, don't hard-code "if counterparty contains X then category is Y". The example: a large outflow to a brokerage you own is *not* an expense — it's a transfer. Only Codex can see that, because Codex reads both the bank statement and the brokerage's deposit confirmation in the same session. Scripts can't.

   Mechanical work (parsing XLSX bytes, executing SQL, decrypting a PDF with a known password, calling `rclone`) is fine as a script. *Interpretation* of what the data means is always done by Codex.

3. **One topic per file.** A skill describes a flow; it never repeats schema details. The schema doc never repeats the Drive layout. If you're about to write the same fact in two places, stop and decide which file owns it.

4. **Instruct, don't hardcode.** Tell Codex what tables exist and what each doc type generally looks like. Don't dictate the SQL queries to run or the regexes to match. The exception is artifacts that can only be code: the SQL schema (`init-db.sql`), the XLSX byte-parser (`scripts/xlsx_to_rows.py`), the HTML template, the CSS.

5. **Money as integers.** All amounts stored as `amount_minor INTEGER`. Multiply on the way in, divide on the way out. Never `REAL` for money.

6. **Audit trail is non-negotiable.** Every row in every fact table has a `source_doc_id` pointing back to `documents`. If a row can't cite its source, it doesn't get inserted.

7. **Idempotency.** Running any skill twice on the same Drive state must be a no-op. Dedup keys: `documents.drive_id` for files; `(account_id, as_of, component)` for balances; `(period_start, period_end, employer)` for payslips.

## What lives where

```
~/findash/
├── AGENTS.md                 ← you are here
├── .gitignore
├── rclone.conf               ← OAuth token, chmod 600, gitignored
├── .secrets/
│   └── findash               ← one chmod-600 INI: [drive] [hapoalim] [cal] [telegram] [pdf-passwords]
├── .claude-plugin/
│   ├── plugin.json           ← plugin manifest (name: findash → /findash:<skill>)
│   └── marketplace.json      ← lets others `/plugin marketplace add ya5huk/findash`
├── skills/                   ← plugin skills (auto-scanned)
│   ├── daily-run/SKILL.md          ← full morning flow; what run_daily.sh invokes
│   ├── fetch-bank-data/SKILL.md
│   ├── sync-finance-data/SKILL.md
│   ├── render-finance-dashboard/SKILL.md
│   ├── findash-doctor/SKILL.md
│   └── setup/SKILL.md              ← guided first-time onboarding
├── docs/
│   ├── sqlite-schema.md      ← schema conventions + example queries
│   ├── drive-layout.md       ← Drive folder structure (ID lives in .secrets/findash [drive])
│   ├── doc-types/            ← per-folder doc-type catalogue + judgment calls (README = index)
│   ├── design-system.md      ← the booky aesthetic
│   └── inspiration/          ← reference images
├── templates/
│   ├── dashboard.html.tpl    ← HTML shell with {{PLACEHOLDER}}s
│   ├── styles.css            ← inlined into dashboard.html at render time
│   ├── charts.js             ← inlined into dashboard.html at render time
│   └── vendor/               ← gitignored; produced by scripts/bundle-assets.py
│       ├── chart.umd.min.js
│       ├── chartjs-adapter-date-fns.bundle.min.js
│       └── fonts-inline.css  ← EB Garamond + Cormorant Garamond as base64 woff2
├── data/
│   └── finance.db            ← local SQLite (source of truth), backed up to Drive weekly
├── inbox/
│   └── staging/              ← the fetch→sync handoff; sync deletes files after ingest
│       ├── fetched/          ← bank pairs staged by fetch-bank-data
│       └── drive/            ← Drive pulls, filenames prefixed <driveId>__
├── output/
│   └── dashboard.html        ← single self-contained file (CSS + JS + fonts inlined)
└── scripts/
    ├── init-db.sql           ← schema definition
    ├── xlsx_to_rows.py       ← stdlib-only XLSX → JSON parser
    ├── bundle-assets.py      ← one-shot vendor script for templates/vendor/
    ├── fetch_bank.js         ← Puppeteer wrapper around israeli-bank-scrapers (Hapoalim + Cal)
    ├── run_daily.sh          ← unattended wrapper: loads the plugin, runs /findash:daily-run
    ├── lib/                  ← shared secret-file parsers (secrets.mjs, findash_secrets.py)
    ├── package.json          ← npm deps for fetch_bank.js (`cd scripts && npm install`)
    └── node_modules/         ← gitignored
```

## One-time setup notes

- `qpdf` is required to unlock payslip PDFs. Install: `sudo apt install -y qpdf`.
- `rclone` is already configured for the project; auth lives in `./rclone.conf` (chmod 600). Always pass `--config ./rclone.conf` to any rclone invocation.
- **All findash secrets live in one chmod-600 file, `.secrets/findash`** — an INI with `[drive] [hapoalim] [cal] [telegram] [pdf-passwords]` sections. `rclone.conf` stays separate (rclone's own OAuth config). The Drive vault root folder ID is `root_folder_id=<ID>` under `[drive]`; get the ID from your vault folder's Drive URL (`drive.google.com/drive/folders/<ID>`). Skills read it and pass it to rclone via `--drive-root-folder-id=<ID>`. Folder *structure* is documented in `docs/drive-layout.md`.
- Payslip passwords go under `[pdf-passwords]` in `.secrets/findash`, one `<filename-pattern>=<password>` per line.
- **Offline assets for the dashboard:** run `python3 scripts/bundle-assets.py` once to vendor Chart.js, the date adapter, and base64-embedded EB Garamond + Cormorant Garamond fonts into `templates/vendor/` (gitignored). Re-run only if pinned versions change.
- **Telegram delivery for the dashboard:** the `[telegram]` section of `.secrets/findash` holds the bot credentials:
  ```
  [telegram]
  bot_token=<from @BotFather>
  chat_id=<your numeric Telegram user id, from @userinfobot>
  ```
  Create the bot once via `@BotFather` → `/newbot`, then tap **Start** on the bot so it's allowed to DM you. `render-finance-dashboard` reads it and sends the dashboard as a single HTML attachment. If no `[telegram]` section is present, the skill still writes the local file and just skips the send.
- **Hapoalim + Cal auto-fetch (`fetch-bank-data` skill):**
  1. Node ≥ 22.13.0 required (`israeli-bank-scrapers` engine constraint). Check with `node --version`; if older, `nvm install 22 && nvm use 22`.
  2. `cd scripts && npm install` — installs `israeli-bank-scrapers` + Puppeteer + a bundled Chromium. One `package.json` covers both companies.
  3. Add Hapoalim credentials to `.secrets/findash` (chmod 600) under `[hapoalim]`:
     ```
     [hapoalim]
     user_code=<your hapoalim user code>
     password=<your hapoalim password>
     ```
  4. Add Cal credentials to `.secrets/findash` under `[cal]`. The key is `username` (matches Cal's login UI and the library's credential shape), not `user_code`:
     ```
     [cal]
     username=<your cal username>
     password=<your cal password>
     ```
  5. One-time Hapoalim browser run, so the trusted-device cookie is seeded:
     ```
     node scripts/fetch_bank.js --company=hapoalim --setup
     ```
     A real Chromium opens. Log in, complete the SMS OTP, trust the device if offered, wait for the account page, then press Enter in the terminal. Profile is saved to `~/.cache/findash/chromium-profile/hapoalim/` and re-used silently on subsequent runs.
  6. One-time Cal browser run (Cal doesn't always 2FA, but `--setup` seeds the profile and lets a CAPTCHA be solved interactively if it appears):
     ```
     node scripts/fetch_bank.js --company=visaCal --setup
     ```
     Log in if prompted, solve CAPTCHA/2FA if it appears, trust the device if offered, then press Enter in the terminal. Profile is saved to `~/.cache/findash/chromium-profile/visaCal/`.
  7. Either source whose credentials are absent (no `[<company>]` section in `.secrets/findash`) is silently skipped — a one-bank user can still run the skill.

## Files to never commit

**Never commit `rclone.conf`, `data/finance.db`, `.secrets/`, `inbox/`, or `output/`** — `.gitignore` covers them, but double-check before any `git add -A`.

## Trigger phrases

Skills are invoked as `/findash:<skill>`; the phrases below also trigger them by description.

- **"run the daily flow"** / **"morning flow"** / **"run everything"** / **"fetch sync render"** / **"do my finances"** → `daily-run` skill (the whole flow)
- **"fetch bank data"** / **"pull from bank"** / **"fetch hapoalim"** / **"fetch cal"** / **"fetch credit card"** / **"pull from cal"** → `fetch-bank-data` skill
- **"sync finance"** / **"sync my finance"** / **"ingest new docs"** → `sync-finance-data` skill
- **"render dashboard"** / **"show my finances"** / **"build the dashboard"** → `render-finance-dashboard` skill
- **"doctor"** / **"finance doctor"** / **"check finance setup"** / **"what's missing"** → `findash-doctor` skill
- **"set up findash"** / **"onboard"** / **"first-time setup"** / **"configure findash"** → `setup` skill

The typical morning flow is `fetch → IBKR → sync → render` — wrapped by `daily-run` (one command, or `scripts/run_daily.sh` unattended): fetch stages new transactions in local `inbox/staging/fetched/`; the IBKR step runs best-effort (interactive auth; unattended runs skip with a `⚠️` warning); sync gathers new Drive drops into staging (best-effort) and ingests everything staged into SQLite, deleting staged files after commit, then backs the DB up to Drive weekly; render reads SQLite into the HTML dashboard and ships it to Telegram, warnings riding along in the summary message.
