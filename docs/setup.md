# Setup

FinDash is a Claude Code project. The repo contains Claude skills in `.claude/skills/`; those skills orchestrate local scripts, Google Drive, SQLite, and optional Telegram delivery.

The minimal setup is Claude Code + Python + SQLite + rclone + a Google Drive vault. Telegram and automatic bank fetching are optional.

## 1. Install and authenticate Claude Code

Install Claude Code from Anthropic's docs:

- [Claude Code setup](https://code.claude.com/docs/en/setup)
- [Claude Code skills](https://code.claude.com/docs/en/skills)

Verify it works:

```bash
claude --version
cd /path/to/findash
claude
```

Start Claude from the repo root. Project skills are discovered from `.claude/skills/`, so these commands should be available in the Claude session:

```text
/findash-doctor
/sync-finance-data
/render-finance-dashboard
/fetch-bank-data
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

Create `.secrets/drive`:

```bash
mkdir -p .secrets
printf 'root_folder_id=<Drive folder ID>\n' > .secrets/drive
chmod 600 .secrets/drive
```

Test access:

```bash
ROOT_ID="$(sed -n 's/^root_folder_id=//p' .secrets/drive)"
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

Only `.secrets/drive` is required for the core Drive workflow.

```text
.secrets/drive       # root_folder_id=<Drive folder ID>
.secrets/telegram    # optional: bot_token=... / chat_id=...
.secrets/hapoalim    # optional: user_code=... / password=...
.secrets/cal         # optional: username=... / password=...
.secrets/pdf-passwords
```

Keep all secret files local:

```bash
chmod 600 .secrets/*
```

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
/findash-doctor
```

It checks binaries, local directories, rclone config, Drive credentials, vendor assets, SQLite, optional Telegram secrets, optional bank secrets, and browser profiles.

## 8. First sync and render

Drop source documents into the Drive vault's `dump/` folder. They can be PDFs, XLSX exports, screenshots, or API dumps produced by `/fetch-bank-data`.

Then run:

```text
/sync-finance-data
/render-finance-dashboard
```

The first command lets Claude inspect and classify documents, insert rows into SQLite, and back up the DB. The second command renders `output/dashboard.html` and sends it to Telegram if Telegram is configured.

## Telegram Optional

Telegram delivery sends the rendered dashboard as an HTML attachment. Without Telegram, `/render-finance-dashboard` still writes `output/dashboard.html`.

Create a bot:

1. Open Telegram and message `@BotFather`.
2. Run `/newbot`.
3. Copy the bot token.
4. Start a chat with the new bot so it can message you.
5. Get your numeric chat ID, for example from `@userinfobot`.

Create `.secrets/telegram`:

```bash
cat > .secrets/telegram <<'EOF'
bot_token=<from BotFather>
chat_id=<your numeric chat id>
EOF
chmod 600 .secrets/telegram
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

Create Hapoalim credentials if you use Hapoalim:

```bash
cat > .secrets/hapoalim <<'EOF'
user_code=<your hapoalim user code>
password=<your hapoalim password>
EOF
chmod 600 .secrets/hapoalim
```

Create Cal credentials if you use Cal:

```bash
cat > .secrets/cal <<'EOF'
username=<your cal username>
password=<your cal password>
EOF
chmod 600 .secrets/cal
```

Seed browser profiles once. This lets you solve OTP/CAPTCHA interactively and reuse trusted-device cookies:

```bash
node scripts/fetch_bank.js --company=hapoalim --setup
node scripts/fetch_bank.js --company=visaCal --setup
```

Then Claude can run:

```text
/fetch-bank-data
```

If one source is not configured, the skill skips it. If the bank forces a fresh OTP later, rerun the relevant `--setup` command.

## Payslip Passwords Optional

For password-protected PDFs, create `.secrets/pdf-passwords`:

```text
<filename-pattern>=<password>
```

Example:

```text
*-payroll.pdf=<password>
```

Then:

```bash
chmod 600 .secrets/pdf-passwords
```

## Daily Run Optional

After the interactive flow works, the daily wrapper can run all three skills unattended:

```bash
CLAUDE_BIN="$(command -v claude)" scripts/run_daily.sh
```

It runs fetch best-effort, then sync, then render. Fetch failures are reported but do not block sync/render.
