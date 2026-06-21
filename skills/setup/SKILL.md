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

### 4. Re-run the doctor

Run `findash-doctor` again and confirm it reports all-systems-go (or only optional gaps for features the user doesn't want). Point the user at [`docs/setup.md`](../../docs/setup.md) for the full reference.

## Principles

- **You automate the safe, local, idempotent work; the human owns secrets and OAuth.** Never type the user's passwords / tokens for them, never run `rclone config` or any `--setup` on their behalf (these need a human at a browser), never print secret values.
- **One secrets file.** `.secrets/findash` is the only place findash reads credentials from — there is no per-file fallback.
