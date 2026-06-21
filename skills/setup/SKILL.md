---
name: setup
description: Use when the user says "set up findash", "onboard", "first-time setup", "get findash running", "configure findash", or is installing the plugin for the first time. Runs findash-doctor's safe auto-fixes, then guides the human through the steps only they can do — creating `.secrets/findash`, the rclone Google Drive OAuth, the Telegram bot, and the one-time bank-scraper browser seeding.
---

# setup

First-time onboarding for findash. You **automate everything safe and local**, then **guide the human** through everything that needs them. You never enter the user's secrets yourself and never run anything that captures their passwords through this session — you tell them exactly what to put where, and they do it.

## Steps

### 1. Run the doctor's auto-fixes

Run the `findash-doctor` skill (diagnose → auto-fix → report). It creates `data/ inbox/ output/ .secrets/`, fixes file modes, runs `npm install`, bundles the vendor assets, and inits the DB. Read its report: the **🚫 Blockers** and **⚠️ Optional** lines are exactly the steps below that the human still has to do. See [`../findash-doctor/SKILL.md`](../findash-doctor/SKILL.md).

### 2. Secrets — one file, filled in by the user

Tell the user to create `.secrets/findash` and `chmod 600` it. Give them this single block to paste and edit — they keep only the sections they need:

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

[ibkr]
# IBKR auth is the official Interactive Brokers connector you add in Claude (no
# password here). account_name maps IBKR onto ONE findash account — you set it in
# the IBKR step below. account_ids/base_currency are optional (multiple IBKR
# accounts / non-default base currency).
# account_name=<the findash account IBKR attaches to>
# account_ids=<comma-separated IBKR account ids>
# base_currency=ILS
```

Then: `chmod 600 .secrets/findash`.

**Do not collect these values in the conversation or write the file yourself** — the user pastes their own secrets in. rclone's OAuth token lives separately in `rclone.conf` (step 3), not here.

**Coming from the old per-file layout?** Legacy `.secrets/{drive,hapoalim,cal,telegram,pdf-passwords}` files are **no longer read**. Tell the user to copy each value into the matching `[section]` of `.secrets/findash` and delete the old files — until they do, that integration won't work. Don't move their secrets for them.

### 3. Steps only the human can do

These need a browser or an interactive terminal — you cannot do them, so print the exact commands and let the user run them:

- **rclone Google Drive OAuth:** `rclone config` to authorize Drive, then make sure the token ends up at `./rclone.conf` (chmod 600). doctor moves `~/.config/rclone/rclone.conf` → `./rclone.conf` automatically if it finds it there.
- **Telegram bot (only if you want delivery):** create a bot via `@BotFather` → `/newbot`, tap **Start** so it can DM you, then put `bot_token` + `chat_id` in `.secrets/findash` `[telegram]`.
- **Bank-scraper browser seeding (only if you use fetch):** one interactive run per source to seed the trusted-device cookie:
  ```
  node scripts/fetch_bank.js --company=hapoalim --setup
  node scripts/fetch_bank.js --company=visaCal --setup
  ```
  Log in, complete OTP / CAPTCHA, trust the device, then press Enter. The profile is saved under `~/.cache/findash/chromium-profile/<companyId>/` and reused silently afterward.
- **IBKR portfolio (only if you use Interactive Brokers):** IBKR is **not** a findash MCP server — it's Anthropic's official **Interactive Brokers connector**, added through Claude's own connector directory, and it's **interactive-only** (so it is never part of the unattended cron run).
  1. **Add the connector once:** in Claude, `+` → **Connectors** → **Add connector** → **Browse connectors**, search **"ibkr"**, pick **Interactive Brokers (IBKR)** (under *Anthropic & Partners*), and log in with your IBKR credentials. Confirm with `/mcp` (`Interactive Brokers (IBKR) · connected`), and restart Claude Code so the session picks it up.
  2. **Map IBKR onto a findash account.** Every fact row attaches to an *account*, so mapping IBKR to the wrong one double-counts your net worth or splits your portfolio. Crucially, **your IBKR holdings may already be tracked under another broker** — some Israeli brokers (e.g. a Phoenix/Hafenix wrapper) *are* IBKR underneath, so a *separate* IBKR account would count the same money twice. List your accounts and decide which one IBKR really is (attach), or pick a new name (create):
     ```
     sqlite3 data/finance.db 'SELECT id, name, kind, institution FROM accounts ORDER BY id'
     ```
     Then record the choice under `[ibkr]` in `.secrets/findash` (it's a plain account name, not a secret):
     ```
     [ibkr]
     account_name=<the findash account name IBKR attaches to>
     ```
  3. **Pull your trades:** run `/findash:fetch-investments` in an interactive session. It ingests your IBKR trade history onto the mapped account (so you stop screenshotting trades) plus a reconciliation snapshot. It's read-only — deny any tool that would place an order or move funds. If `account_name` is unset it asks once and prints the line to paste. If you don't use IBKR, skip this entirely.

### 4. Re-run the doctor

Run `findash-doctor` again and confirm it reports all-systems-go (or only optional gaps for features the user doesn't want). Point the user at [`docs/setup.md`](../../docs/setup.md) for the full reference.

## Principles

- **You automate the safe, local, idempotent work; the human owns secrets and OAuth.** Never type the user's passwords / tokens for them, never run `rclone config` or any `--setup` on their behalf (these need a human at a browser), never print secret values.
- **One secrets file.** `.secrets/findash` is the only place findash reads credentials from — there is no per-file fallback.
