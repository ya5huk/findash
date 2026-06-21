# Setup

FinDash is a Claude Code plugin (named `findash`). The repo contains the plugin manifest in `.claude-plugin/` and the skills in `skills/`; those skills orchestrate local scripts, Google Drive, SQLite, and optional Telegram delivery.

The minimal setup is Claude Code + Python + SQLite + rclone + a Google Drive vault. Telegram and automatic bank fetching are optional.

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
/findash:daily-run
/findash:findash-doctor
/findash:sync-finance-data
/findash:render-finance-dashboard
/findash:fetch-bank-data
```

`/findash:setup` is the guided path for a first-time setup; it auto-fixes the safe pieces and walks you through the rest. `/findash:daily-run` runs the whole flow once everything is configured.

Others can install the plugin without cloning. From any Claude Code session:

```text
/plugin marketplace add ya5huk/findash
/plugin install findash@findash
```

## 2. Install local tools

Required for the core sync/render flow:

```bash
python3 --version
sqlite3 --version
rclone version
```

Usually needed:

```bash
qpdf --version
```

`qpdf` is only required for password-protected payslip PDFs.

Required only for automatic bank/card fetching:

```bash
node --version
```

The scraper wrapper needs Node `>=22.13.0`.

## 3. Connect Google Drive with rclone

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

FinDash expects a project-local config file:

```bash
cp ~/.config/rclone/rclone.conf ./rclone.conf
chmod 600 ./rclone.conf
```

Get the Drive folder ID from the vault URL:

```text
https://drive.google.com/drive/folders/<THIS_PART_IS_THE_ID>
```

Put it in the `[drive]` section of `.secrets/findash` (the single secrets file, created in [section 5](#5-create-local-secrets)):

```ini
[drive]
root_folder_id=<Drive folder ID>
```

Test access (reads the ID from `.secrets/findash`):

```bash
ROOT_ID="$(sed -n '/^\[drive\]/,/^\[/{s/^root_folder_id=//p;}' .secrets/findash)"
rclone --config ./rclone.conf --drive-root-folder-id "$ROOT_ID" lsf gdrive:
```

For deeper rclone details, see [rclone's Google Drive docs](https://rclone.org/drive/).

## 4. Bundle dashboard assets

The dashboard is rendered as one self-contained HTML file. Vendor Chart.js and fonts once:

```bash
python3 scripts/bundle-assets.py
```

This writes gitignored files under `templates/vendor/`.

## 5. Create local secrets

All credentials live in a single INI file, `.secrets/findash`, with one section per integration. Only the `[drive]` section is required for the core Drive workflow; omit any section you don't use:

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

Keep it local and locked down:

```bash
mkdir -p .secrets
chmod 600 .secrets/findash
```

`rclone.conf` is **not** part of this file — it stays a separate file because it is rclone's own OAuth config, passed via `--config ./rclone.conf` (see [section 3](#3-connect-google-drive-with-rclone)).

### Migrating from legacy per-file secrets

Earlier versions used one file per integration (`.secrets/drive`, `.secrets/hapoalim`, `.secrets/cal`, `.secrets/telegram`, `.secrets/pdf-passwords`). These are **no longer read** — move each value into the matching `[section]` of `.secrets/findash` (see the block above) and delete the old files.

## 6. Initialize the database

The doctor skill can create the database for you. Manually:

```bash
mkdir -p data inbox output
sqlite3 data/finance.db < scripts/init-db.sql
```

`data/finance.db` is gitignored. Sync uploads the current DB snapshot back to the Drive vault.

## 7. Run the setup doctor

From Claude Code:

```text
/findash:findash-doctor
```

It checks binaries, local directories, rclone config, Drive credentials, vendor assets, SQLite, optional Telegram secrets, optional bank secrets, and browser profiles. (`/findash:setup` runs the same checks as part of the guided onboarding.)

## 8. First sync and render

Drop source documents into the Drive vault's `dump/` folder. They can be PDFs, XLSX exports, screenshots, or API dumps produced by `/findash:fetch-bank-data`.

Then run the whole flow with one command:

```text
/findash:daily-run
```

It fetches, syncs, renders, and delivers to Telegram. To run the steps by hand instead:

```text
/findash:sync-finance-data
/findash:render-finance-dashboard
```

`/findash:sync-finance-data` lets Claude inspect and classify documents, insert rows into SQLite, and back up the DB. `/findash:render-finance-dashboard` renders `output/dashboard.html` and sends it to Telegram if Telegram is configured.

## Telegram Optional

Telegram delivery sends the rendered dashboard as an HTML attachment. Without Telegram, `/findash:render-finance-dashboard` still writes `output/dashboard.html`.

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

Test after rendering:

```bash
scripts/send_telegram.sh
```

Telegram's own bot walkthrough is here: [From BotFather to Hello World](https://core.telegram.org/bots/tutorial).

## Automatic Bank Fetch Optional

You do not need automatic fetching to use FinDash. You can manually export statements or screenshots and drop them into Drive `dump/`.

Automatic fetching uses `israeli-bank-scrapers` through `scripts/fetch_bank.js`. Currently wired sources:

- Bank Hapoalim
- Cal / Visa Cal

Install dependencies:

```bash
cd scripts
npm install
cd ..
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

Then Claude can run:

```text
/findash:fetch-bank-data
```

If one source is not configured, the skill skips it. If the bank sends an OTP
or CAPTCHA during a later unattended run, the fetch step is expected to fail
best-effort; rerun the relevant `--setup` command to refresh the trusted-device
profile, then rerun the daily flow.

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

## Daily Run Optional

After the interactive flow works, the daily wrapper can run the whole flow
unattended. `scripts/run_daily.sh` loads the plugin and runs the `/findash:daily-run`
skill via `claude -p` (internally `claude --plugin-dir "$REPO_ROOT" -p "/findash:daily-run"`),
retrying once after 60s if the first attempt fails.

`claude -p` cannot ask for approvals while running from cron. Keep broad,
machine-specific permissions in `.claude/settings.local.json` (gitignored), not
in public docs or committed settings. If a manual wrapper test says commands
like `rclone`, `sqlite3`, `python3`, or `curl` require approval, copy the
working allowlist from a trusted local checkout or create a local settings file
that allows those commands for this project.

Before scheduling it, verify:

- `rclone.conf`, the `[drive]` section of `.secrets/findash`, and
  `data/finance.db` are present.
- Telegram is configured if you expect a Telegram attachment.
- `templates/vendor/` exists from `python3 scripts/bundle-assets.py`.
- `scripts/node_modules/` exists from `cd scripts && npm install`.
- `.claude/settings.local.json` exists locally with unattended permissions for
  the mechanical commands used by the flow.
- For every configured bank/card source, the matching `--setup` command above
  has completed at least once.

Test the exact wrapper manually:

```bash
CLAUDE_BIN="$(command -v claude)" scripts/run_daily.sh
```

It runs fetch best-effort, then sync, then render. Fetch failures are reported but do not block sync/render.
