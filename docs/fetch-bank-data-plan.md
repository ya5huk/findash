# Auto-fetch Hapoalim + Cal data → `dump/` with reasoned filenames

## Context

The existing findash flow is Drive-first: you (or someone) manually exports statements from Hapoalim and Cal, drops them into `dump/`, and `sync-finance-data` ingests them. The `sync` skill leans on Claude's judgment, but it can only judge what's in the file — for Hapoalim, it can't see that a 5000 ILS outflow to a brokerage is followed three days later by a 5000 ILS inflow back unless both transactions land in the same statement window; for Cal, the Hapoalim checking statement only shows the consolidated monthly bill ("כאל" `card_payment` entry per `docs/doc-types.md:267`), not the itemized merchant charges, installments, or foreign-currency conversions.

A new skill, **`fetch-bank-data`**, will pull data directly from both customer-facing sites — Bank Hapoalim (`companyId: 'hapoalim'`) and Cal (`companyId: 'visaCal'`) — via `israeli-bank-scrapers` (Puppeteer-based; PSD2 is gated behind AISP licensing and not accessible to individuals), pre-process it with Claude's reasoning, and drop pairs of files into Drive `dump/` for `sync` to ingest as it normally would. The reasoning passes generate **filename tags** + a **sidecar `.notes.md`** that capture observations sync needs but couldn't derive from a single file: for bank accounts, round-trips, internal transfers to known own-accounts (Hafenix, Excellence, etc.), and first-time counterparties; for Cal, installment chains, foreign-currency charges, first-time merchants, and pending vs. completed status.

Two skills, two trigger phrases, clean separation: fetch → drop → exit; sync picks up later. Adding more issuers later (`max`, `isracard`, `amex`) is mechanically additive — one more `.secrets/<company>` file, one more mapping line in the skill, no architectural change.

## Approach

### The skill

**`.claude/skills/fetch-bank-data/SKILL.md`**

Trigger phrases: *"fetch bank data"*, *"pull from bank"*, *"fetch hapoalim"*, *"fetch cal"*, *"fetch credit card"*, *"pull from cal"*, morning-equivalents.

Flow:
1. For each source in `[hapoalim, cal]`: check if `.secrets/<source>` exists. Silently skip a source whose secrets file is absent (lets a one-bank user still run the skill). Hapoalim file format: `user_code=…`/`password=…`; Cal file format: `username=…`/`password=…`.
2. Per source: determine date range by querying SQLite for the latest `transactions.date` on accounts at that institution → start a few days earlier (overlap for safety), end today. If DB has nothing for that institution, fall back to 60 days back (library supports up to a year if a deeper backfill is ever needed).
3. Invoke `scripts/fetch_bank.js --company=<companyId>` once per source with credentials in env; capture JSON output.
4. **Reasoning pass** (Claude, not script) — observation vocabulary depends on source kind:
   - **Bank-account observations (Hapoalim)**: round-trip detection (same-magnitude opposite-sign transactions within ~14 days, same counterparty pattern); internal transfer detection (counterparty names matching known own-account vocabulary — Excellence, Hafenix, savings vehicles — cross-referenced against the `accounts` table); first-time counterparty (string never seen in `transactions` for this account); amount anomaly (e.g., > 2× typical for that counterparty).
   - **Credit-card observations (Cal)**: installment chain (rows where `type === 'installments'`; group by `identifier`, note "N/M installments of group #<id>"); foreign-currency charge (`originalCurrency !== 'ILS'`; note implied rate `chargedAmount / originalAmount`); first-time merchant (`description` never seen on any credit-card account in `transactions` — broader than Hapoalim's per-account check, since one merchant might have appeared on the Mastercard before); pending status (`status === 'pending'`; flag so sync can mark provisional and reconcile on a later run).
   - **Cross-source**: when Cal's run shows a sizable recent processed total, sanity-check against the most-recent or next-scheduled `card_payment` row on the Hapoalim checking account. Note any material divergence.
5. Build filename tags from the observations. Compact, kebab-cased, joined with `__`. Keep under ~180 chars total.
6. Write paired files to a local staging dir (`inbox/staging/`):
   - `<YYYY-MM-DD>-<company>-<acct-suffix>-api-fetch[__tag…].json` — full raw scraper output, untouched.
   - `<YYYY-MM-DD>-<company>-<acct-suffix>-api-fetch.notes.md` — prose reasoning, one bullet per observation.
   Examples: Hapoalim quiet day → `2026-05-22-hapoalim-<acct>-api-fetch.json`; Cal with installments + FX → `2026-05-22-cal-<acct>-api-fetch__installments-amazon__fx-usd-zara.json`.
7. Upload both to Drive `dump/` via `rclone copy --config ./rclone.conf`. Same drop target as a manual XLSX, so sync's normal triage handles them.
8. Report a per-source summary (no Telegram — sync owns delivery): e.g., "Hapoalim: 23 txns, 0 flags. Cal: 17 txns, 2 flags (installments, fx)." If a source was skipped because secrets were absent, name it explicitly so the user knows.

### The script

**`scripts/fetch_bank.js`** + **`scripts/package.json`**

One script, parameterized by `--company`. Hapoalim and Cal share all mechanics; only credentials and the `companyId` differ.

- **Args**: `node scripts/fetch_bank.js --company=<hapoalim|visaCal> [--setup]`.
- **Env contract** (the skill loads `.secrets/<company>` and re-exports the parsed `key=value` lines under company-prefixed names before invoking):
  - `--company=hapoalim` reads `HAPOALIM_USER_CODE`, `HAPOALIM_PASSWORD`, `START_DATE`.
  - `--company=visaCal` reads `CAL_USERNAME`, `CAL_PASSWORD`, `START_DATE`.
  Credential shape passed to the library matches what `israeli-bank-scrapers` expects per company: Hapoalim → `{ userCode, password }`; Cal → `{ username, password }`. Note Cal uses `username`, not `userCode`.
- **Node version check**: first lines verify `process.versions.node` is ≥ 22.12.0 (library requirement). Exit cleanly with a pointer to the one-time-setup section if older.
- **Per-company Chromium profile**: `~/.cache/findash/chromium-profile/<companyId>/` — isolates cookies so Hapoalim and Cal logins don't cross-contaminate. Trusted-device cookies (Hapoalim) and any soft anti-bot state (Cal) persist between runs.
- **Profile reuse mechanism**: the library does not accept `userDataDir` directly. Launch Puppeteer ourselves with `userDataDir`, hand the browser to the scraper via the `browser` option, set `skipCloseBrowser: true`, and close the browser after `scrape()`:
  ```js
  const browser = await puppeteer.launch({ userDataDir, headless: !setup });
  const scraper = createScraper({
    companyId, startDate,
    combineInstallments: false,   // per-installment rows preserved for cash-flow accuracy
    browser, skipCloseBrowser: true,
  });
  const result = await scraper.scrape(credentials);
  await browser.close();
  ```
- **`combineInstallments: false`** — pinned with the rationale above. Each future-month installment surfaces as its own row with its own `processedDate` aligned to the month it will be billed. A future change to `true` should be a deliberate decision, not a default drift.
- **`showBrowser`** defaults to false. `--setup` flag flips the underlying Puppeteer launch to `headless: false` for the one-time interactive run.
- **Output**: pretty-printed JSON of the library's `{success, accounts: [{accountNumber, balance, txns: [...]}]}` to stdout, no transformation. Exit non-zero on `success: false`, printing the `errorType`/`errorMessage` to stderr.
- **Memory-friendly**: don't transform the library output. Reasoning happens in the skill, not the script.

### One-time setup

Documented in the skill body and `CLAUDE.md`'s setup section:

1. `cd scripts && npm install` (installs `israeli-bank-scrapers` + Puppeteer + Chromium; one `package.json` covers both companies).
2. Verify Node version: `node --version` must be ≥ 22.12.0 (library requirement). Older default? Install via nvm and `nvm use 22`.
3. Create `.secrets/hapoalim`:
   ```
   user_code=<your hapoalim user code>
   password=<your hapoalim password>
   ```
   `chmod 600 .secrets/hapoalim`.
4. Create `.secrets/cal`:
   ```
   username=<your cal username>
   password=<your cal password>
   ```
   `chmod 600 .secrets/cal`. Note the key is `username` (matches Cal's login UI and the library's credential shape), not `user_code`.
5. Hapoalim one-time browser run: `node scripts/fetch_bank.js --company=hapoalim --setup`. A real browser opens. Log in, complete SMS OTP. Profile saved to `~/.cache/findash/chromium-profile/hapoalim/`. Close browser.
6. Cal one-time browser run: `node scripts/fetch_bank.js --company=visaCal --setup`. Cal doesn't always 2FA, but the `--setup` run seeds the profile dir, verifies credentials, and lets a CAPTCHA be solved interactively if it appears. Profile saved to `~/.cache/findash/chromium-profile/visaCal/`.
7. Subsequent automated runs use each saved profile silently — neither bank should re-challenge on the same Chromium profile, except occasionally for fresh consent screens.

### Doc-types entry

Add sections to **`docs/doc-types.md`** describing the API-dump pairs. Sync auto-creates doc types per existing feedback, but documenting the shapes helps Claude reason about them on first encounter.

**Hapoalim (bank account):**

- `bank_api_dump`: the `.json` file. Field shape: `{success, accounts:[{accountNumber, balance, txns:[{date, processedDate, originalAmount, chargedAmount, description, memo, identifier, …}]}]}`. Amounts are floats (multiply ×100 for `amount_minor`). Per-transaction running balance NOT included; only the latest `balance` on each account object — treat as a `balances` snapshot with `as_of = today`.
- `bank_api_notes`: the sidecar `.notes.md`. Read alongside the JSON. Each bullet is one observation Claude made at fetch time; sync should treat the notes as informational hints, not ground truth — verify against the data and against cross-source documents (Hafenix statements, etc.).

**Cal (credit card):**

- `cal_api_dump`: the `.json` file. Same outer shape as `bank_api_dump`; per-transaction fields differ:
  - `type` ∈ {`'normal'`, `'installments'`}
  - `identifier` (int): groups all installment rows of one physical purchase
  - `date`: original purchase date
  - `processedDate`: when **this particular installment** hits the bank (the bill-charge date). Sync maps this to `transactions.value_date`.
  - `originalAmount` + `originalCurrency`: present always; equal `chargedAmount`/`ILS` for domestic purchases. Non-ILS values indicate a foreign-currency charge — sync stashes the FX context in `description`.
  - `chargedAmount`: ILS amount billed (this is what becomes `amount_minor`).
  - `installments: {number, total}`: present when `type === 'installments'`.
  - `status` ∈ {`'completed'`, `'pending'`}. `pending` rows are not yet on a closed bill; sync ingests them with a `[pending]` marker in `description` and reconciles when they reappear as `completed` (dedup key: `(account_id, date, identifier)`).
  - Per account, the library returns a single `balance` value — for Cal this is the next-bill amount (סכום לחיוב). Treat as a `balances` snapshot with `component='next_bill'`, `amount_minor` signed **negative** (liability), `as_of = today`.
- `cal_api_notes`: the sidecar `.notes.md`. Same convention as `bank_api_notes`. Bullet vocabulary: installment chains, FX merchants, first-time merchants, pending status. Hints, not ground truth.

### Sync's perspective (no code changes needed)

Sync already does judgment-based triage and category assignment. The new pairs land in `dump/` like any other drop. Sync reads filename + sidecar, ingests `txns[]` as `transactions` rows, ingests `balance` as a `balances` snapshot, and uses the notes to inform categorization (e.g., a flagged round-trip → mark both legs as transfer/wash, not `expense`).

Hapoalim mapping is the existing bank-statement story — no special notes.

Cal mapping is new; spelled out here so sync can reach it via judgment + the doc-types entries above:

- Map each Cal txn to `transactions` with `account_id` = the Cal credit-card account. `date` = `date`; `value_date` = `processedDate` (so cash-flow ordering matches the actual bank-charge date for each installment); `amount_minor` = `round(chargedAmount × 100)` with sign per the schema convention in `scripts/init-db.sql:62-63` (merchant charge → negative; refund/payment to the card → positive); `currency = 'ILS'`.
- When `originalCurrency !== 'ILS'`: stash FX context in `description`, e.g. `"AMAZON.COM (USD 45.20 → ILS 167.10 @ 3.697)"`. The schema has no structured FX columns; the text form is lossy but recoverable.
- When `type === 'installments'`: append the installment marker to `description`, e.g. `"<merchant> (2/6, group #<identifier>)"`. The `identifier` is the group key, so all installments of one purchase are findable.
- When `status === 'pending'`: append `"[pending]"` to `description`. On a later sync, when the same `identifier` reappears as `completed`, dedup by `(account_id, date, identifier)` and update in place rather than inserting a duplicate.
- First-ever Cal fetch: no `accounts` row exists for the returned `accountNumber`. Sync auto-creates a row with `kind='credit_card'`, `institution='Cal'`, `name='Cal <last4-of-accountNumber>'`, `currency='ILS'`. Autonomy precedent: sync may create accounts/doc types without confirmation.
- Map Cal's per-account `balance` to a `balances` row: `as_of` = today, `component='next_bill'`, `amount_minor` signed negative, `currency='ILS'`, `source_doc_id` → the JSON `documents` row.

No code change in `sync-finance-data/SKILL.md` is required. If sync turns out to mis-handle these files in practice, that's a sync update, not a fetch concern.

## Critical files

**New:**
- `.claude/skills/fetch-bank-data/SKILL.md` — the skill itself (frontmatter + body, mirroring `sync-finance-data/SKILL.md`'s structure)
- `scripts/fetch_bank.js` — the Puppeteer wrapper, multi-company via `--company` flag
- `scripts/package.json` — dependency declaration for `israeli-bank-scrapers` (and `puppeteer` as a peer if the library doesn't pull a matching version automatically)

**Modified:**
- `docs/doc-types.md` — add entries for `bank_api_dump`, `bank_api_notes`, `cal_api_dump`, `cal_api_notes`
- `CLAUDE.md` — extend "One-time setup notes" (npm install, Node version check, both `.secrets/` files, both `--setup` runs)
- `.gitignore` — add `scripts/node_modules/`

**User-created (not in git):**
- `.secrets/hapoalim` — `user_code=`, `password=` lines, chmod 600
- `.secrets/cal` — `username=`, `password=` lines, chmod 600 (note: `username`, not `user_code`)

## Reused patterns

- **Frontmatter + trigger phrases**: copy `sync-finance-data/SKILL.md` structure (`name`, `description` with explicit trigger phrases).
- **Script-does-mechanical, skill-does-judgment**: same split as render-dashboard (`scripts/render_dashboard.py` is canonical) and sync (XLSX parsing via `scripts/xlsx_to_rows.py`).
- **`.secrets/` `key=value` lines**: same format as `.secrets/telegram` (`bot_token=…`/`chat_id=…`) and `.secrets/pdf-passwords`. One file per service, chmod 600.
- **rclone upload**: same `--config ./rclone.conf` invocation pattern sync uses for Drive operations.
- **Drive `dump/` as the universal drop**: sync's existing triage handles any file dropped there; no new sync code path.
- **`accounts.kind = 'credit_card'`**: already an established kind. Cal becomes another `credit_card` row; no new account-kind vocabulary.
- **Sign convention for credit cards** (charges negative, payments positive) is documented in `scripts/init-db.sql:62-63` and `docs/sqlite-schema.md:11`. No new convention.
- **Per-company Chromium profile** (`~/.cache/findash/chromium-profile/<companyId>/`) — new but minor; mirrors the same isolation idea as one-`.secrets/`-file-per-service.
- **`israeli-bank-scrapers` companyId enum**: extending to `max`, `isracard`, `amex`, etc. later requires only a new `.secrets/<company>` file and the corresponding env-mapping in the skill — no architectural change.

## Verification

End-to-end test (after both one-time setup runs):

1. Run trigger phrase ("fetch bank data"). Confirm: no OTP/CAPTCHA prompts (profiles trusted), JSON returned for both sources, two pairs of files appear in `inbox/staging/`, then in Drive `dump/`.
2. Inspect the dropped filenames: should contain reasoned tags only when corresponding patterns exist in the data. On a quiet day, plain `<date>-<company>-<acct>-api-fetch.json` is correct for each source. Examples: `2026-05-22-hapoalim-1234-api-fetch__roundtrip-5000-excellence.json`; `2026-05-22-cal-5678-api-fetch__installments-amazon__fx-usd-zara.json`.
3. Inspect both `.notes.md` sidecars: bullets should match the filename tags 1:1 and add prose context. Cal's sidecar should call out installments, FX merchants, and `[pending]` rows where they exist.
4. Run `sync` next. Confirm:
   - Triage moves both pairs out of `dump/` into the appropriate folders.
   - Hapoalim txns appear in `transactions` with `source_doc_id` pointing to the JSON `documents` row.
   - Cal txns appear on a `credit_card`-kind `accounts` row (auto-created on first ever Cal fetch); `value_date = processedDate`; installment and FX context preserved in `description`.
   - `balances` gains: today's Hapoalim account snapshot, today's Cal `next_bill` snapshot (negative).
   - Re-running fetch + sync on the same day is a no-op (idempotency via `documents.drive_id` dedup).
5. Run `render dashboard`. Confirm: Hapoalim total reflects today's API-fetched balance (not the older XLSX statement balance); Cal's `next_bill` balance reduces net worth as a liability (the existing render aggregation already includes `credit_card` accounts — it just had no balance data until now).

Edge cases to spot-check:

- **Hapoalim 2FA mid-week**: SMS OTP fires at Hapoalim's discretion. Skill surfaces the error clearly and tells the user to re-run `--setup` for hapoalim.
- **Cal CAPTCHA / soft-block**: `success: false`, `errorType: 'GENERIC'` or `'TIMEOUT'`. Skill surfaces the error verbatim and tells the user to re-run `--setup` for visaCal specifically.
- **Single-source failure**: one company's `--company` invocation exits non-zero; the other continues. Skill reports per-source success/failure. No partial files for the failing source.
- **Source-secrets missing**: `.secrets/cal` (or `.secrets/hapoalim`) absent — skill silently skips that source and names it in the summary.
- **DB completely empty for one source**: start date defaults to 60 days back; subsequent runs use the DB max date for that institution.
- **Cal silent day**: `accounts[0].txns = []`. Skill skips writing files for Cal; summary says "no Cal activity since <last-date>".
- **Cal installments crossing the start-date**: with `combineInstallments: false`, future-dated installments may appear. Let them through; their `processedDate` is correctly in the future, and sync stores them with `value_date` in the future — they show up in cash-flow forecasts.
- **First-ever Cal fetch (no `accounts` row)**: sync auto-creates `kind='credit_card'`, `institution='Cal'`, `name='Cal <last4>'`, `currency='ILS'`.
- **Pending Cal row that later completes**: next sync sees the same `identifier` but with `status: 'completed'`; updates the row in place rather than inserting a duplicate.

## Out of scope

Called out explicitly so they're not lost:

- **Schema enrichment** for FX `original_amount`/`original_currency` and installment `number`/`total` fields. Lossy stashing in `description` is the YAGNI choice for now. If FX or installment analytics become important, that's a follow-up plan touching `scripts/init-db.sql` + `docs/sqlite-schema.md` + render logic.
- **Dashboard "Liabilities" section**. Cal's `next_bill` balance will reduce net worth via the existing aggregation, but no separate liabilities breakdown is added in this plan. A dedicated credit-card-debt panel is a future render-dashboard plan.
- **Other Cal-like issuers** (`max`, `isracard`, `amex`). The architecture supports them with only a new `.secrets/<company>` file and the corresponding env-mapping in the skill; no plan work needed now.
