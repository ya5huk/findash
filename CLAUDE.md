# findash

Personal finance system. Drive vault → SQLite → HTML dashboard. Three skills do the work — `fetch-bank-data` pulls fresh transactions from Hapoalim + Cal into Drive `dump/`; `sync-finance-data` ingests everything in the vault into SQLite; `render-finance-dashboard` reads SQLite into the HTML. Everything else is data, templates, and documentation.

## First principles

These shape every decision in this project. Re-read them when you're about to write code.

1. **Judgment over scripts.** Claude's reasoning is the asset, not a parser script. Don't write categorization rules, don't pattern-match filenames mechanically, don't hard-code "if counterparty contains X then category is Y". The example: a 10,000 ILS outflow to Excellence is *not* an expense — it's a transfer to a brokerage you own. Only Claude can see that, because Claude reads both the bank statement and the brokerage's deposit confirmation in the same session. Scripts can't.

   Mechanical work (parsing XLSX bytes, executing SQL, decrypting a PDF with a known password, calling `rclone`) is fine as a script. *Interpretation* of what the data means is always done by Claude.

2. **One topic per file.** A skill describes a flow; it never repeats schema details. The schema doc never repeats the Drive layout. If you're about to write the same fact in two places, stop and decide which file owns it.

3. **Instruct, don't hardcode.** Tell Claude what tables exist and what each doc type generally looks like. Don't dictate the SQL queries to run or the regexes to match. The exception is artifacts that can only be code: the SQL schema (`init-db.sql`), the XLSX byte-parser (`scripts/xlsx_to_rows.py`), the HTML template, the CSS.

4. **Money as integers.** All amounts stored as `amount_minor INTEGER`. Multiply on the way in, divide on the way out. Never `REAL` for money.

5. **Audit trail is non-negotiable.** Every row in every fact table has a `source_doc_id` pointing back to `documents`. If a row can't cite its source, it doesn't get inserted.

6. **Idempotency.** Running any skill twice on the same Drive state must be a no-op. Dedup keys: `documents.drive_id` for files; `(account_id, as_of, component)` for balances; `(period_start, period_end, employer)` for payslips.

## What lives where

```
~/findash/
├── CLAUDE.md                 ← you are here
├── .gitignore
├── rclone.conf               ← OAuth token, chmod 600, gitignored
├── .secrets/
│   ├── drive                 ← root_folder_id=… for the Drive vault, chmod 600
│   ├── pdf-passwords         ← <pattern>=<password> lines, chmod 600
│   ├── telegram              ← bot_token=… / chat_id=… for dashboard delivery
│   ├── hapoalim              ← user_code=… / password=… for fetch-bank-data
│   └── cal                   ← username=… / password=… for fetch-bank-data
├── .claude/skills/
│   ├── sync-finance-data/SKILL.md
│   ├── render-finance-dashboard/SKILL.md
│   └── fetch-bank-data/SKILL.md
├── docs/
│   ├── sqlite-schema.md      ← schema conventions + example queries
│   ├── drive-layout.md       ← Drive folder structure (ID lives in .secrets/drive)
│   ├── doc-types.md          ← what each kind of document contains + judgment calls
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
│   └── finance.db            ← local SQLite, synced to Drive
├── inbox/                    ← transient downloads from Drive
├── output/
│   └── dashboard.html        ← single self-contained file (CSS + JS + fonts inlined)
└── scripts/
    ├── init-db.sql           ← schema definition
    ├── xlsx_to_rows.py       ← stdlib-only XLSX → JSON parser
    ├── bundle-assets.py      ← one-shot vendor script for templates/vendor/
    ├── fetch_bank.js         ← Puppeteer wrapper around israeli-bank-scrapers (Hapoalim + Cal)
    ├── package.json          ← npm deps for fetch_bank.js (`cd scripts && npm install`)
    └── node_modules/         ← gitignored
```

## One-time setup notes

- `qpdf` is required to unlock payslip PDFs. Install: `sudo apt install -y qpdf`.
- `rclone` is already configured for the project; auth lives in `./rclone.conf` (chmod 600). Always pass `--config ./rclone.conf` to any rclone invocation.
- The Drive vault root folder ID lives in `.secrets/drive` (chmod 600) as `root_folder_id=<ID>`. Get the ID from your vault folder's Drive URL (`drive.google.com/drive/folders/<ID>`). Skills read it from there and pass it to rclone via `--drive-root-folder-id=<ID>`. Folder *structure* (what lives under it) is documented in `docs/drive-layout.md`.
- Payslip passwords go in `.secrets/pdf-passwords`, one `<filename-pattern>=<password>` per line. Keep this file local and chmod 600.
- **Offline assets for the dashboard:** run `python3 scripts/bundle-assets.py` once to vendor Chart.js, the date adapter, and base64-embedded EB Garamond + Cormorant Garamond fonts into `templates/vendor/` (gitignored). Re-run only if pinned versions change.
- **Telegram delivery for the dashboard:** `.secrets/telegram` holds the bot credentials, two lines:
  ```
  bot_token=<from @BotFather>
  chat_id=<your numeric Telegram user id, from @userinfobot>
  ```
  Create the bot once via `@BotFather` → `/newbot`, then tap **Start** on the bot so it's allowed to DM you. `render-finance-dashboard` reads this file and sends the dashboard as a single HTML attachment. If the file is absent, the skill still writes the local file and just skips the send.
- **Hapoalim + Cal auto-fetch (`fetch-bank-data` skill):**
  1. Node ≥ 22.13.0 required (`israeli-bank-scrapers` engine constraint). Check with `node --version`; if older, `nvm install 22 && nvm use 22`.
  2. `cd scripts && npm install` — installs `israeli-bank-scrapers` + Puppeteer + a bundled Chromium. One `package.json` covers both companies.
  3. Create `.secrets/hapoalim` (chmod 600):
     ```
     user_code=<your hapoalim user code>
     password=<your hapoalim password>
     ```
  4. Create `.secrets/cal` (chmod 600). Note the key is `username` (matches Cal's login UI and the library's credential shape), not `user_code`:
     ```
     username=<your cal username>
     password=<your cal password>
     ```
  5. One-time Hapoalim browser run, so the trusted-device cookie is seeded:
     ```
     node scripts/fetch_bank.js --company=hapoalim --setup
     ```
     A real Chromium opens. Log in, complete the SMS OTP. Profile is saved to `~/.cache/findash/chromium-profile/hapoalim/` and re-used silently on subsequent runs.
  6. One-time Cal browser run (Cal doesn't always 2FA, but `--setup` seeds the profile and lets a CAPTCHA be solved interactively if it appears):
     ```
     node scripts/fetch_bank.js --company=visaCal --setup
     ```
     Profile is saved to `~/.cache/findash/chromium-profile/visaCal/`.
  7. Either source whose `.secrets/<company>` file is absent is silently skipped — a one-bank user can still run the skill.

## Files to never commit

**Never commit `rclone.conf`, `data/finance.db`, `.secrets/`, `inbox/`, or `output/`** — `.gitignore` covers them, but double-check before any `git add -A`.

## Trigger phrases

- **"fetch bank data"** / **"pull from bank"** / **"fetch hapoalim"** / **"fetch cal"** / **"fetch credit card"** / **"pull from cal"** → `fetch-bank-data` skill
- **"sync finance"** / **"sync my finance"** / **"ingest new docs"** → `sync-finance-data` skill
- **"render dashboard"** / **"show my finances"** / **"build the dashboard"** → `render-finance-dashboard` skill
- **"doctor"** / **"finance doctor"** / **"check finance setup"** / **"what's missing"** → `findash-doctor` skill

The typical morning flow is `fetch → sync → render`: fetch lands new transactions in Drive `dump/`; sync ingests everything in `dump/` (plus anything else newly in the vault) into SQLite; render reads SQLite into the HTML dashboard and ships it to Telegram.
