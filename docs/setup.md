# Setup

FinDash is a Claude Code plugin (named `findash`). Its skills sync financial evidence into SQLite; a separate localhost React app provides the live dashboard.

The minimal local setup is Claude Code + Python (including its built-in SQLite
module). The Google Drive connector adds manual-document intake from a Drive
vault, but it is optional. Automatic bank fetching is optional too.

## 1. Install Claude Code and load the plugin

Install Claude Code from Anthropic's docs:

- [Claude Code setup](https://code.claude.com/docs/en/setup)
- [Claude Code plugins](https://code.claude.com/docs/en/plugins)

Verify it works, then start Claude from the repo root with the plugin loaded:

```bash
claude --version
cd /path/to/findash
claude --plugin-dir .
```

`--plugin-dir .` points Claude Code at the plugin manifest in this repo, so the namespaced skills become available in the session:

```text
/findash:setup
/findash:findash-doctor
/findash:sync-finance-data
/findash:fetch-bank-data
/findash:fetch-investments
```

`/findash:setup` is the guided path. `/findash:sync-finance-data` runs every configured source and ingests it into SQLite.

FinDash currently requires a local checkout because its private state and
mechanical scripts are rooted beside the repository. Do not install it as a
standalone marketplace plugin yet; clone it and launch with `--plugin-dir .` as
shown above.

## 2. Install local tools

Required for the core sync and dashboard flows:

```bash
python3 --version
node --version
```

Python 3.10 or newer and Node 22.13 or newer are required. Install the locked React dependencies once:

```bash
scripts/install_dashboard_deps.sh
```

Required only for password-protected payslip PDFs:

```bash
qpdf --version
```

Automatic bank/card fetching uses the same Node minimum plus its separate `scripts/install_node_deps.sh` bundle.

Before configuring any optional integration, create the single local secrets
file with user-only permissions. A fresh clone does not contain this gitignored
file:

```bash
mkdir -p .secrets
chmod 700 .secrets
touch .secrets/findash
chmod 600 .secrets/findash
```

Section 5 shows the sections you can add. Creating the empty private file here
means the optional integration sections below have somewhere to go.

## 3. Optional: connect Google Drive

Skip this section if you do not want Drive-based document intake. The full sync
and local dashboard still work; Drive operations warn and continue.

Drive access is Anthropic's official **Google Drive connector** from Claude's own
connector directory — it is *not* a findash MCP server, no OAuth token lands in
this checkout, and the connector's sign-in is managed by your Claude account.
Add it once (needs a browser): in Claude, `+` → **Connectors** → **Add
connector** → **Browse connectors** → search **"Google Drive"** → pick it and sign
in with the Google account that owns the vault. Confirm it's live:

```text
/mcp
```

It should list the Google Drive connector as connected. Restart Claude Code after
adding it so the session picks it up. Connectors appear in Claude Code only when
you're signed in with your **Claude.ai subscription** (not an `ANTHROPIC_API_KEY`).

Create a Google Drive folder for the vault, for example `finance-vault`. Its contents should follow [docs/drive-layout.md](./drive-layout.md).

Get the Drive folder ID from the vault URL:

```text
https://drive.google.com/drive/folders/<THIS_PART_IS_THE_ID>
```

Put it in the `[drive]` section of `.secrets/findash` (the private file created
above; [section 4](#4-fill-in-local-secrets) shows the complete optional layout):

```ini
[drive]
root_folder_id=<Drive folder ID>
```

Skills only ever read the ID through the fixed helper, which prints it and
nothing else:

```bash
python3 scripts/drive_root.py
```

Every connector call is scoped to that folder tree; findash never searches or
lists the rest of your Drive. The setup doctor probes the root through the
connector and creates any missing fixed vault folder idempotently, leaving
existing folders alone.

Documents pulled from the vault travel through the connector into local staging
(`inbox/staging/drive/`), where the skills read the real bytes. The connector is
read/write on your Drive, so during sync deny any tool call that would share or
trash a file — findash only downloads, creates the fixed folders, and moves
`dump/` files into their category folders.

## 4. Fill in local secrets

All credentials live in a single INI file, `.secrets/findash`, with one section
per integration. No integration section is required universally; add `[drive]`
only if you enabled the optional Drive workflow, and omit anything you do not
use:

```ini
# .secrets/findash — chmod 600. Omit any section you don't use.
[drive]
root_folder_id=<from your vault folder's Drive URL: drive.google.com/drive/folders/<ID>>

[hapoalim]
user_code=<your hapoalim user code>
password=<your hapoalim password>

[cal]
username=<your cal username>
password=<your cal password>

[pdf-passwords]
<payslip-filename-pattern>=<password>
```

After editing it, re-assert the private directory and file modes:

```bash
chmod 700 .secrets
chmod 600 .secrets/findash
```

FinDash keeps all local financial state user-only. Run the deterministic mode
check after setup or after copying local data from elsewhere:

```bash
python3 scripts/harden_permissions.py
```

Drive authorization is **not** part of this file — it lives in the Google Drive
connector inside your Claude account (see [section 3](#3-optional-connect-google-drive)).

## 5. Initialize the database

The doctor skill can create the database for you. Manually:

```bash
python3 scripts/harden_permissions.py
python3 scripts/migrate_db.py --init
```

The same command without `--init` upgrades an existing database.
`data/finance.db` is gitignored. Sync also archives a consistent weekly snapshot
under `data/backups/` (kept to the newest eight); include `data/` in whatever
machine backup you already trust. The local database is the source of truth.

## 6. Run the setup doctor

From Claude Code:

```text
/findash:findash-doctor
```

It checks the local runtime, directories, and SQLite, then reports
the Drive connector, bank sources, and browser profiles as feature-gated
optional checks. (`/findash:setup` runs the same checks as part of the guided
onboarding.)

## 7. First sync and dashboard

If you enabled Drive, drop source documents into the vault's `dump/` folder.
They can be PDFs, XLSX exports, or screenshots — `dump/` is yours alone. Bank
fetching and supported live feeds stage or ingest their data locally and do not
require Drive.

Run the complete data update:

```text
/findash:sync-finance-data
```

It fetches bank/card data, attempts IBKR, gathers Drive drops, ingests/reconciles SQLite, refreshes market references, and performs the gated backup.

Run the live dashboard independently:

```bash
./run_dashboard.sh
```

Open `http://127.0.0.1:4173`. The private local server refreshes stored stock and FX observations every 30 minutes while it runs; the browser only reads its JSON snapshot.

## Automatic Bank Fetch Optional

You do not need automatic fetching to use FinDash. If you enabled the optional
Drive workflow, you can instead export statements or screenshots and drop them
into Drive `dump/`.

Automatic fetching uses `israeli-bank-scrapers` through `scripts/fetch_bank.js`. Currently wired sources:

- Bank Hapoalim
- Cal / Visa Cal

Install dependencies:

```bash
scripts/install_node_deps.sh
```

Add a `[hapoalim]` section to `.secrets/findash` if you use Hapoalim:

```ini
[hapoalim]
user_code=<your hapoalim user code>
password=<your hapoalim password>
```

Add a `[cal]` section if you use Cal (the key is `username`, matching Cal's login UI and the scraper's credential shape, not `user_code`):

```ini
[cal]
username=<your cal username>
password=<your cal password>
```

Seed browser profiles once for every configured source. This step is required
before scheduled runs can fetch from that source. It lets you solve OTP/CAPTCHA
interactively and reuse trusted-device cookies:

```bash
node scripts/fetch_bank.js --company=hapoalim --setup
node scripts/fetch_bank.js --company=visaCal --setup
```

The setup command opens Chromium and waits. In the browser, log in, complete
OTP/CAPTCHA, approve or trust the device if the site offers it, and wait for the
account/dashboard page. Then return to the terminal and press Enter so Chromium
closes cleanly and the profile is flushed to disk.

Chromium's sandbox stays enabled by default. If you are running as root inside a
trusted, isolated container where Chromium refuses to start, you may append
`--disable-browser-sandbox`; do not use that flag on a normal workstation or server.

Then Claude can run:

```text
/findash:fetch-bank-data
```

If one source is not configured, the skill skips it. If the bank sends an OTP
or CAPTCHA during a later scheduled run, the fetch step is expected to fail
best-effort; rerun the relevant `--setup` command to refresh the trusted-device
profile, then rerun the full sync.

## Interactive Brokers (IBKR) Portfolio Optional

You do not need IBKR to use FinDash. If you have an Interactive Brokers account, `fetch-investments` pulls your IBKR **trade history** straight into SQLite and attaches it as a feed to the logical brokerage it describes. A wrapper statement and the underlying IBKR feed therefore do not become two assets. It also writes position and cash observations for reconciliation.

IBKR is **not** a findash MCP server. It uses Anthropic's official **Interactive Brokers connector** from Claude's own connector directory. `sync-finance-data` attempts it best-effort; a session where the connector is connected ingests, any other session skips with a warning. There's **no password** in `.secrets/findash`.

1. **Add the IBKR connector once (needs a browser).** In Claude:

   `+` → **Connectors** → **Add connector** → **Browse connectors** → search **"ibkr"** → **Interactive Brokers (IBKR)** (listed under *Anthropic & Partners*) → log in with your IBKR credentials and approve.

   Then confirm it's live:

   ```text
   /mcp
   ```

   It should show `Interactive Brokers (IBKR) · connected`. Restart Claude Code after adding the connector so the session picks it up. The connector appears in Claude Code only when you're signed in with your **Claude.ai subscription** (not an `ANTHROPIC_API_KEY`).

2. **Account mapping is automatic.** IBKR attaches to one of your existing findash accounts rather than getting its own — often an existing brokerage, since a broker that's an IBKR wrapper (e.g. some Israeli brokers) would otherwise double-count net worth. `/findash:fetch-investments` resolves the account itself on first run (attaching where the ledgers clearly match, creating an account for IBKR-only users, asking only when genuinely ambiguous) and stores the choice in the DB. Nothing to configure here; the `[ibkr]` section is only for the optional keys:

   ```ini
   [ibkr]
   # Optional — only for multiple IBKR accounts or a non-default base currency:
   # account_ids=<comma-separated IBKR account ids>
   # base_currency=ILS
   # account_name=<legacy override only — pre-existing installs>
   ```

3. **Pull your trades.** In an interactive session:

   ```text
   /findash:fetch-investments
   ```

   It ingests your IBKR trades onto the mapped account (deduped by trade id) and writes reconciliation position/cash evidence. The connector use is **read-only** — if Claude prompts for any tool that places an order or moves funds, deny it. If the connector isn't connected, the skill skips. See [docs/live-sources.md](./live-sources.md).

## Payslip Passwords Optional

For password-protected PDFs, add a `[pdf-passwords]` section to `.secrets/findash`, one `<filename-pattern>=<password>` per line:

```ini
[pdf-passwords]
<filename-pattern>=<password>
```

Example:

```ini
[pdf-passwords]
*-payroll.pdf=<password>
```

## Daily Sync Optional

After the interactive flow works, schedule the sync as a **Claude scheduled task**
(routine) in the Claude desktop app. A scheduled task runs inside your signed-in
Claude session, so the Google Drive and IBKR connectors are available to it. A
headless `claude -p` job would not see claude.ai connectors, which is why FinDash
no longer ships a cron/LaunchAgent wrapper.

1. Open this checkout in Claude Code inside the desktop app (the plugin loads from
   the repo root, exactly like `claude --plugin-dir .`).
2. Create a daily scheduled task with the `/schedule` skill and give it a prompt
   along these lines:

   ```text
   Run the local /sync-finance-data skill.
   Report a summary of what you found / ingested / anything interesting.
   Talk short and straight to the point.
   ```

3. Before the first scheduled run, verify interactively:

   - `data/finance.db` is present.
   - If you expect Drive intake, the Google Drive connector is connected and the
     `[drive]` section of `.secrets/findash` is filled; otherwise the doctor's
     Drive warning is expected.
   - If a bank/card source is configured, `scripts/node_modules/` exists from
     `scripts/install_node_deps.sh`, and the matching `--setup` command above has
     completed at least once.
   - A manual `/findash:sync-finance-data` completes without approval prompts and
     ends with `FINDASH_SYNC_STATUS=ok`; keep any machine-specific script
     approvals in `.claude/settings.local.json` (gitignored).

The committed settings allow only the repository's fixed-path scripts; they
intentionally do not allow raw `sqlite3`, `qpdf`, `rm`, or arbitrary Python. Do
not add those broad prefixes to public settings.

The task writes the database only; run the React app separately to look at
the result. Each run reports its own count-only summary in the task's
conversation.
