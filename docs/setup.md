# Setup

FinDash is a Claude Code plugin (named `findash`). Its skills sync financial evidence into SQLite and optionally send a deterministic Telegram snapshot; a separate localhost React app provides the live dashboard.

The minimal local setup is Claude Code + Python (including its built-in SQLite
module). Google Drive/rclone adds manual-document intake and archival database
backups, but it is optional. Telegram and automatic bank fetching are optional too.

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
/findash:send-finance-dashboard
/findash:fetch-bank-data
/findash:fetch-investments
```

`/findash:setup` is the guided path. `/findash:sync-finance-data` runs every configured source and ingests it without rendering or delivery.

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

Required only for the optional Drive vault and backup:

```bash
rclone version
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
means the optional Drive steps below can write their configuration in order.

## 3. Optional: connect Google Drive with rclone

Skip this section if you do not want Drive-based document intake or archival
backups. The full sync and local dashboard still work; Drive operations warn and continue.

Create a Google Drive folder for the vault, for example `finance-vault`. Its contents should follow [docs/drive-layout.md](./drive-layout.md).

Configure rclone with a Google Drive remote named exactly `gdrive`:

```bash
rclone config
```

During setup:

- Choose Google Drive.
- Name the remote `gdrive`.
- Use a scope that can read, write, move, and delete files in the vault.
- Complete the browser OAuth flow.

FinDash expects a project-local config file. Copy rclone's default config with
the fixed helper; it preserves the original, refuses symlinks/overwrites, and
creates the project copy at mode 600:

```bash
python3 scripts/adopt_rclone_config.py
```

If you configured a non-default rclone path, copy that file manually to
`./rclone.conf` and run `chmod 600 ./rclone.conf`; the helper intentionally does
not search arbitrary locations.

Get the Drive folder ID from the vault URL:

```text
https://drive.google.com/drive/folders/<THIS_PART_IS_THE_ID>
```

Put it in the `[drive]` section of `.secrets/findash` (the private file created
above; [section 5](#5-fill-in-local-secrets) shows the complete optional layout):

```ini
[drive]
root_folder_id=<Drive folder ID>
```

Test private connectivity, then create or validate the fixed vault folders
through the wrapper. Both commands read the ID internally and never list or
print private filenames:

```bash
scripts/drive.sh probe
scripts/drive.sh ensure-layout
```

`ensure-layout` is idempotent: it creates any missing public folder from the
committed Drive layout and leaves existing folders alone. The setup doctor runs
the same two checks whenever Drive is fully configured.

For deeper rclone details, see [rclone's Google Drive docs](https://rclone.org/drive/).

## 4. Bundle dashboard assets

The dashboard is rendered as one self-contained HTML file. Vendor Chart.js and fonts once:

```bash
python3 scripts/bundle-assets.py
```

This writes gitignored files under `templates/vendor/`.
Verify an existing bundle without network access or changes:

```bash
python3 scripts/bundle-assets.py --check
```

## 5. Fill in local secrets

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

[telegram]
bot_token=<from @BotFather>
chat_id=<your numeric id, from @userinfobot>

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

`rclone.conf` is **not** part of this file — when Drive is enabled, it stays a
separate file because it is rclone's own OAuth config, passed via `--config
./rclone.conf` (see [section 3](#3-optional-connect-google-drive-with-rclone)).

## 6. Initialize the database

The doctor skill can create the database for you. Manually:

```bash
python3 scripts/harden_permissions.py
python3 scripts/migrate_db.py --init
```

The same command without `--init` upgrades an existing database.
`data/finance.db` is gitignored. When Drive is configured, sync also uploads a
consistent archival snapshot there; the local database remains the source of
truth either way.

## 7. Run the setup doctor

From Claude Code:

```text
/findash:findash-doctor
```

It checks the local runtime, directories, vendor assets, and SQLite, then reports
rclone/Drive, Telegram, bank sources, and browser profiles as feature-gated
optional checks. (`/findash:setup` runs the same checks as part of the guided
onboarding.)

## 8. First sync and dashboard

If you enabled Drive, drop source documents into the vault's `dump/` folder.
They can be PDFs, XLSX exports, or screenshots — `dump/` is yours alone. Bank
fetching and supported live feeds stage or ingest their data locally and do not
require Drive.

Run the complete data update:

```text
/findash:sync-finance-data
```

It fetches bank/card data, attempts IBKR, gathers Drive drops, ingests/reconciles SQLite, refreshes market references, and performs the gated backup. It does not render or send.

Run the live dashboard independently:

```bash
./run_dashboard.sh
```

Open `http://127.0.0.1:4173`. The private local server refreshes stored stock and FX observations every 30 minutes while it runs; the browser only reads its JSON snapshot.

## Telegram Optional

Telegram delivery is an explicit output action. `/findash:send-finance-dashboard` renders `output/dashboard.html` from the stored cache, then sends it as an attachment.

Create a bot:

1. Open Telegram and message `@BotFather`.
2. Run `/newbot`.
3. Copy the bot token.
4. Start a chat with the new bot so it can message you.
5. Get your numeric chat ID, for example from `@userinfobot`.

Add a `[telegram]` section to `.secrets/findash`:

```ini
[telegram]
bot_token=<from @BotFather>
chat_id=<your numeric id, from @userinfobot>
```

Test the complete render-and-send flow:

```text
/findash:send-finance-dashboard
```

Telegram's own bot walkthrough is here: [From BotFather to Hello World](https://core.telegram.org/bots/tutorial).

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
before unattended cron/launchd runs can fetch from that source. It lets you
solve OTP/CAPTCHA interactively and reuse trusted-device cookies:

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
or CAPTCHA during a later unattended run, the fetch step is expected to fail
best-effort; rerun the relevant `--setup` command to refresh the trusted-device
profile, then rerun the full sync.

## Interactive Brokers (IBKR) Portfolio Optional

You do not need IBKR to use FinDash. If you have an Interactive Brokers account, `fetch-investments` pulls your IBKR **trade history** straight into SQLite and attaches it as a feed to the logical brokerage it describes. A wrapper statement and the underlying IBKR feed therefore do not become two assets. It also writes position and cash observations for reconciliation.

IBKR is **not** a findash MCP server. It uses Anthropic's official **Interactive Brokers connector** from Claude's own connector directory. `sync-finance-data` attempts it best-effort; interactive syncs can authenticate, while unattended syncs skip with a warning. There's **no password** in `.secrets/findash`.

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

After the interactive flow works, the daily wrapper can update data unattended.
`scripts/run_sync.sh` loads the plugin and runs `/findash:sync-finance-data`
via `claude -p`,
retrying once after 120s if the first attempt fails. Each attempt has a 30-minute
timeout by default, and an atomic per-checkout lock prevents overlapping runs.
Override the timeout with `FINDASH_ATTEMPT_TIMEOUT_SECONDS` (60–7200) if needed.

The wrapper never renders or contacts Telegram. Run the React app or invoke the Telegram skill separately.

`claude -p` cannot ask for approvals while running from cron. The committed
settings allow only the repository's fixed-path wrappers; they intentionally do
not allow raw `rclone`, `sqlite3`, `qpdf`, `rm`, or arbitrary Python. Do not add
those broad prefixes to public settings. If your Claude installation still asks
about a trusted repository script during a manual wrapper test, approve that
specific script in `.claude/settings.local.json` (gitignored).

Before scheduling it, verify:

- `data/finance.db` is present.
- If you expect Drive intake/backups, `rclone.conf` and the `[drive]` section of
  `.secrets/findash` are present; otherwise their doctor warnings are expected.
- If a bank/card source is configured, `scripts/node_modules/` exists from
  `scripts/install_node_deps.sh`.
- A manual wrapper test completes without approval prompts; keep any necessary
  machine-specific script approvals in `.claude/settings.local.json`.
- For every configured bank/card source, the matching `--setup` command above
  has completed at least once.

(IBKR needs no setup here — its connector can't authenticate unattended, so the
daily sync's best-effort IBKR step will just skip with a warning; see the IBKR
section above.)

Test the exact wrapper manually:

```bash
CLAUDE_BIN="$(command -v claude)" scripts/run_sync.sh
```

It fetches configured sources, attempts an already-onboarded IBKR connection best-effort, ingests, refreshes reference data, and backs up when due. The wrapper requires a structured sync-completion marker.

### macOS LaunchAgent

The repository includes a 10:00 local-time LaunchAgent template. From the repo
root, render it safely for the current checkout and the executables currently on
your `PATH`:

```bash
python3 scripts/install_launch_agent.py
launchctl bootstrap gui/$(id -u) "$HOME/Library/LaunchAgents/com.findash.daily.plist"
```

The installer uses XML-safe serialization, includes the active nvm/Homebrew
binary directories when present, and creates the plist and log directories with
private permissions. It does not load or restart the job. To choose another
local schedule, pass `--hour 8 --minute 30`, then boot out and bootstrap the job
again. The template sets umask 077, so its dashboard, staging artifacts,
captures, and log are readable only by the current user.
