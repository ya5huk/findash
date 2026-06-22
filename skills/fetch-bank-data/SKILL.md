---
name: fetch-bank-data
description: Use when the user says "fetch bank data", "pull from bank", "fetch hapoalim", "fetch cal", "fetch credit card", "pull from cal", or any morning-equivalent. Pulls fresh transactions + balances from Bank Hapoalim and Cal via `israeli-bank-scrapers`, reasons about each source's data to attach observation tags + a sidecar notes file, and drops paired files into Drive `dump/` for the next `sync-finance-data` run to ingest.
---

# fetch-bank-data

You pull fresh data from the user's customer-facing bank + credit-card sites and drop reasoned files into Drive `dump/`. **Sync owns ingestion** — your job ends when the files are in `dump/`. You do not touch SQLite, do not call the dashboard, do not send Telegram. Adding more issuers later (`max`, `isracard`, `amex`) is just one more `[section]` in `.secrets/findash` + one more mapping line below.

## Where things live

- Scraper wrapper: `scripts/fetch_bank.js` (parameterized by `--company`)
- Node deps: `scripts/package.json` (`israeli-bank-scrapers` + `puppeteer`; run `cd scripts && npm install` once)
- Credentials: the `[hapoalim]` / `[cal]` sections of `.secrets/findash` (chmod 600)
- Per-company Chromium profile (persists trusted-device cookies + soft anti-bot state): `~/.cache/findash/chromium-profile/<companyId>/`
- Local staging dir for paired files before upload: `inbox/staging/`
- rclone config: `./rclone.conf` (always pass `--config ./rclone.conf`)
- Drive root folder ID: the `[drive]` section of `.secrets/findash` (`root_folder_id=…`, chmod 600). Folder layout: [`docs/drive-layout.md`](../../docs/drive-layout.md) (root is the `dump/` parent).
- SQLite (read-only here, for date-range + own-account vocabulary): `data/finance.db`
- Doc-type shapes sync will see: [`docs/doc-types/full-statements.md`](../../docs/doc-types/full-statements.md) (`bank_api_dump`, `bank_api_notes`, `cal_api_dump`, `cal_api_notes`)

## Sources

| company    | scraper `companyId` | secrets section | env vars consumed by the script         |
|------------|---------------------|-----------------|------------------------------------------|
| `hapoalim` | `hapoalim`          | `[hapoalim]`    | `HAPOALIM_USER_CODE`, `HAPOALIM_PASSWORD` |
| `cal`      | `visaCal`           | `[cal]`         | `CAL_USERNAME`, `CAL_PASSWORD`            |

Both sections are `key=value` lines under their `[…]` header in `.secrets/findash`. Hapoalim uses `user_code=` and `password=`. Cal uses `username=` and `password=` (not `user_code` — matches Cal's login UI and the library's credential shape).

Both also accept `START_DATE` from env (ISO `YYYY-MM-DD`) — the script falls back to 60 days back if unset.

## Flow

Run **all configured sources in parallel** unless the user explicitly named one ("fetch cal", "pull from hapoalim" → just that one). For each source:

### 1. Skip if no secrets

If a source has no credentials (no `[<source>]` section in `.secrets/findash`), silently skip that source and name it in the final summary. A one-bank user still gets a working skill.

### 2. Pick a start date

Query SQLite for the latest transaction date on accounts at that institution:

```sql
SELECT MAX(t.date)
FROM transactions t JOIN accounts a ON a.id = t.account_id
WHERE a.institution = ?;   -- 'Bank Hapoalim' or 'Cal'
```

Subtract a few days (3–5) for overlap safety — sync dedups, so re-sending recent rows is harmless. If the query returns NULL (no data yet for that institution), fall back to 60 days back. Cal-specific note: the first-ever Cal fetch has no `accounts` row at all — institution will not match anything; that's fine, the same 60-day fallback applies.

### 3. Run the scraper

The scraper reads credentials from `.secrets/findash` **itself** — never put them on the command line. That keeps the unattended `claude -p` run allowlist-safe (the command stays a clean `node scripts/fetch_bank.js …` prefix) and your password out of the transcript. Pass the step-2 window as a flag and read the JSON straight from stdout — **no `>` redirection** (the unattended run can't write to arbitrary paths):

```bash
node scripts/fetch_bank.js --company=hapoalim --start-date=YYYY-MM-DD
```

`--company=visaCal` for Cal. Substitute the step-2 start date for `YYYY-MM-DD` (the skill auto-computes one — this flag is just the override). Omit `--start-date` to default to 60 days back. Read the result from the command's stdout. The script exits:
- `0` — success, full library result as JSON on stdout
- `1` — scrape ran but `success: false`, `errorType` / `errorMessage` on stderr
- `2` — missing creds / Node too old / Puppeteer launch failure

On `1` or `2`: surface the error verbatim, recommend `--setup` (see "When things break" below), and proceed to the next source. **Don't write any files for the failing source.**

### 4. Reason about the data

This is the whole point of the skill. The script returns raw library output; you interpret it. Different vocabulary per source kind:

**Bank-account observations (Hapoalim):**

- **Round-trip** — same-magnitude opposite-sign txns within ~14 days, same or related counterparty (e.g. `<amount>` ILS out to your brokerage on day 1, `<amount>` ILS back from your brokerage on day 4). Sync needs to know so it can classify both legs as `transfer`, not `expense`.
- **Internal transfer** — counterparty matches an own-account vocabulary string. Pull the vocabulary from the live `accounts` table:
  ```sql
  SELECT DISTINCT institution, name FROM accounts WHERE closed_on IS NULL;
  ```
  Then fuzzy-match against `description` / `counterparty` strings on each new txn — match your brokerage's name(s) and any savings/sprint indicators. Israeli brokerages often surface under several names (Hebrew + English), so match each known alias from the vocabulary above rather than a single exact string.
- **First-time counterparty** — the `description`/`לטובת` string has never been seen on this account before:
  ```sql
  SELECT 1 FROM transactions
  WHERE account_id = ? AND (description LIKE ? OR counterparty = ?)
  LIMIT 1;
  ```
  Flag with the counterparty name so sync knows to categorize carefully.
- **Amount anomaly** — outflow is > 2× the typical for that counterparty (compute typical from historical txns to the same counterparty on the same account).

**Credit-card observations (Cal):**

- **Installment chain** — rows where `type === 'installments'`. Group by `identifier`. Note "<N>/<M> installments of group #<id>" with the merchant name. All installments of one physical purchase share the same `identifier`, so this groups them naturally.
- **Foreign-currency charge** — `originalCurrency !== 'ILS'`. Note the implied rate `chargedAmount / originalAmount` along with merchant + original currency.
- **First-time merchant** — `description` never seen on **any** credit-card account in `transactions` (broader than Hapoalim's per-account check — the same merchant might have appeared on the Hapoalim Mastercard before). Query:
  ```sql
  SELECT 1 FROM transactions t JOIN accounts a ON a.id = t.account_id
  WHERE a.kind = 'credit_card' AND t.description LIKE ?
  LIMIT 1;
  ```
- **Pending status** — `status === 'pending'`. Flag so sync inserts with a `[pending]` marker and reconciles when the row reappears `completed`.

**Cross-source observation (when both ran this turn):**

- Sum Cal's `completed` charged amounts since the last Hapoalim `card_payment` (כאל) on Hapoalim's checking. Compare against the most recent (or next scheduled) `card_payment` on Hapoalim's side. Note any material divergence — the consolidated Cal-on-Hapoalim row should roughly match the Cal-side total.

Each observation becomes one bullet in the sidecar notes file and (if compact enough) one tag in the filename.

### 5. Build filename + tag string

Per source, per account, compose:

```
<YYYY-MM-DD>-<company>-<acct-suffix>-api-fetch[__<tag>__<tag>…].json
<YYYY-MM-DD>-<company>-<acct-suffix>-api-fetch[__<tag>__<tag>…].notes.md
```

Where:
- `<YYYY-MM-DD>` = today (the fetch date, not the txn date)
- `<company>` = `hapoalim` or `cal`
- `<acct-suffix>` = last 4 of the `accountNumber` field (Hapoalim returns a branch/account string; Cal returns a long card number)
- `<tag>` = compact kebab-case observation marker, joined with `__`

Tag vocabulary (keep terse so the filename stays under ~180 chars total):

- `roundtrip-<amount-thousands>-<counterparty-slug>` — e.g. `roundtrip-5000-<brokerage>`
- `internal-<counterparty-slug>` — e.g. `internal-<brokerage>`
- `firsttime-<counterparty-slug>`
- `anomaly-<counterparty-slug>`
- `installments-<merchant-slug>` — e.g. `installments-amazon`
- `fx-<currency-lower>-<merchant-slug>` — e.g. `fx-usd-zara`
- `pending-<count>` — e.g. `pending-3`
- `cross-divergence-<thousands>` — Cal vs Hapoalim card_payment mismatch

If a tag would push the filename past ~180 chars, drop the lowest-priority tags (anomaly < firsttime < internal < fx < installments < roundtrip < pending) and keep the rest in the sidecar.

**Quiet day = no tags.** Plain `2026-05-22-hapoalim-<acct-suffix>-api-fetch.json` is correct when no patterns triggered.

### 6. Write the pair to `inbox/staging/`

For each account on each source:

- `.json` — the full library result for **that account only** (slice `accounts[i]`), pretty-printed. Preserves every field including ones we don't currently use. Sync re-parses from this.
- `.notes.md` — one bullet per observation:

```markdown
# fetch notes — 2026-05-22 — cal <acct-suffix>

- installments: 2/6 of group #<identifier>, <merchant> <amount> ILS
- fx: AMAZON.COM USD 45.20 → ILS 167.10 (rate 3.697)
- first-time merchant: SHOPIFY APP STORE
- 3 rows pending; expect to reconcile on next fetch
```

Header line names date + company + acct suffix so a sync run reading both files together has the linkage even if the filenames get truncated by a UI.

If an account has zero txns since the start date, skip writing files for that account — the summary should say "no activity since `<last-date>`".

### 7. Upload pairs to Drive `dump/`

```bash
rclone --config ./rclone.conf copy --drive-root-folder-id=<ROOT_ID> \
  inbox/staging/ gdrive:dump/
```

Root ID is `root_folder_id=…` from `.secrets/findash` `[drive]`. After upload succeeds, delete the local `inbox/staging/<file>` (the file lives in Drive now; sync owns the next move).

### 8. Report

Print a compact per-source summary, nothing else. Examples:

```
Hapoalim: 23 txns on <acct-a>, 7 txns on <acct-b>, 0 flags. 2 files uploaded to dump/.
Cal: 17 txns on <acct-suffix>, 2 flags (installments-amazon, fx-usd-zara, pending-3). 1 file uploaded.
```

If a source was skipped: `Hapoalim: skipped (no [hapoalim] credentials).`
If a source failed: `Cal: failed (errorType=GENERIC, errorMessage=…). Re-run: node scripts/fetch_bank.js --company=visaCal --setup`.

**Do not** send to Telegram. Do not invoke sync. Do not touch the DB. End here.

## When things break

- **Hapoalim SMS OTP fires mid-week** — Hapoalim may re-challenge for trusted device at its discretion. The scrape returns `success: false, errorType: 'INVALID_PASSWORD'` or similar. Tell the user to re-run interactively to re-trust the device:
  ```
  node scripts/fetch_bank.js --company=hapoalim --setup
  ```
- **Cal CAPTCHA / soft-block** — usually `errorType: 'GENERIC'` or `'TIMEOUT'`. Re-run with `--setup` and solve the CAPTCHA visually:
  ```
  node scripts/fetch_bank.js --company=visaCal --setup
  ```
- **Single-source failure** — the other source proceeds. The summary names which one failed and which succeeded.
- **First run ever, both sources** — no DB rows for either institution → start date defaults to 60 days back for each. Cal's first run will also have no `accounts` row; sync auto-creates one on ingest.
- **`scripts/node_modules/` missing** — `cd scripts && npm install` first. Skill stops before invoking the script if the install hasn't happened.
- **Node version error** (exit code 2 with version pointer) — `nvm install 22 && nvm use 22`, retry.

## Principles

- **Reasoning is yours, parsing is the script's.** The script never categorizes, never decorates, never normalizes. It returns the library's raw output verbatim; everything else (tags, notes, cross-source checks) is your judgment.
- **Notes are hints, not ground truth.** Sync should verify against the underlying JSON and against cross-source documents (your brokerage's periodic statements, etc.) before trusting any bullet.
- **Idempotency belongs to sync, not here.** It's fine for two runs in the same morning to upload two near-identical files; sync dedups via `documents.drive_id` (which differs per upload) and via `(account_id, date, …)` content keys when it actually ingests the txns.
- **Atomicity per source.** If Cal fails mid-flow, no Cal files land in `dump/`. Hapoalim's files (if its run succeeded) are unaffected.
- **Adding a new issuer is mechanical.** New `[section]` in `.secrets/findash`, new mapping row in the table above, new env-var pair to load. No script change unless the new issuer's credential shape doesn't match `{username, password}` or `{userCode, password}`.
