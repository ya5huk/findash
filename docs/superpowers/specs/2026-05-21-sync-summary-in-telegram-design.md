# Sync summary in Telegram render

## Problem

Today the `render-finance-dashboard` skill sends the dashboard HTML to Telegram with a one-line caption: `Finance — <date> · <net worth>`. After a sync run, the user has no in-Telegram record of *what changed* — which files were ingested, which trades were added, which balances refreshed, what got moved around in the vault. Reviewing the sync therefore requires opening the dashboard and inferring from diffs, or scrolling back through the Claude conversation.

We want the Telegram delivery to also surface a per-file narrative of the most recent sync.

## Goals

- One bullet per file the sync touched, in Claude's voice (judgment, not mechanical formatting).
- Two sections: **Ingested** (files that produced data rows) and **Triaged** (files that were only moved, plus new doc_types created and reorganizations).
- Delivered alongside the HTML dashboard via Telegram, without truncation.
- No-op when `render` runs with no prior sync (or after the previous summary has already been delivered).

## Non-goals

- Computing net-worth deltas since last sync — deferred until a stable balance-snapshot history exists (≥30 days), same reasoning as the existing caption choice.
- Persisting sync history beyond the most recent run — the file is transient.
- Generating the bullets mechanically from the DB — bullets are written by Claude during sync with full document context.

## Architecture

```
sync-finance-data           render-finance-dashboard
─────────────────           ────────────────────────
  ingest files                read data/last_sync_summary.md
  classify, parse             (if present)
  insert rows                 ↓
  ↓                           send HTML to Telegram (existing)
  append to                   ↓
  data/last_sync_summary.md   send summary as a second
  (markdown, two sections)    Telegram text message
                              ↓
                              delete data/last_sync_summary.md
```

Two skills, one filesystem handoff. The sync skill produces the file; the render skill consumes and deletes it. No new SQL tables, no schema changes.

## The summary file

**Path:** `data/last_sync_summary.md` (alongside `finance.db`, gitignored via the existing `data/` ignore rule).

**Format:** plain Markdown with two top-level sections. Each sync run **appends** its bullets under the matching section (does not overwrite), so two syncs before one render still produce a complete list.

```markdown
## Ingested

- added MSFT buy at <price> (5 shares, <fee> fee)
- sold RR.LSE for <price> (3 shares; net of 25% tax + <fee> fee)
- Hapoalim <acct> Feb statement: 18 txns, closing balance <amount> ILS
- April payslip (Acme): <amount> ILS net
- Harel pension Mar statement: 3 monthly deposits, total balance <amount> ILS

## Triaged

- moved screenshot.png → investments/hafenix/2026-05-buy-msft.png
- created doc_type `insurance_statement` → folder `insurance/`; 1 file routed
- reorganized: harel-pension-old.pdf → long-term-savings/harel-pension/2024-03.pdf
```

Empty sections are omitted entirely. If a sync produced no ingests and no triage activity, the file is not written at all.

**Writer responsibilities (sync skill):**

- Append to (or create) the file at the end of step 7 (Summarize), after all rows are committed and the backup is uploaded.
- One bullet per **file** ingested, summarizing what was extracted. Bullets are Claude's prose — they may reference the ticker, employer, account name, key amounts, fees, tax treatment, or any anomaly worth noting.
- A bullet under "Ingested" represents a file that produced ≥1 row in a fact table. A file that was only moved (no rows added) goes under "Triaged".
- Triage entries cover three things: dump/ moves, new `doc_type` creation, and reorganizations of already-classified files into a newly-introduced folder.

**Reader responsibilities (render skill):**

- After the dashboard HTML is sent and Telegram confirms `ok: true`, check whether `data/last_sync_summary.md` exists.
- If absent or empty: silently skip the second message and proceed to the existing report.
- If present: build the second message as a single Telegram text message (UTF-8, Markdown disabled — send as plain text to avoid formatting surprises with Hebrew names or special characters). Prepend a one-line header: `Sync — <date_today>`.
- Post via `sendMessage` (not `sendDocument`) to the same `chat_id`.
- On `ok: true` from `sendMessage`, delete `data/last_sync_summary.md`.
- On `sendMessage` failure: keep the file, surface the error in the report, do not retry. The next render attempt will pick it up.
- If the file exceeds Telegram's 4096-char message limit (very unlikely in practice, but possible after several sync runs), split on bullet boundaries into multiple sequential messages, each <4096 chars.

## Edge cases

- **Render without sync.** File is absent → second message skipped. Existing flow unchanged.
- **Sync twice before render.** Second sync appends under the same section headers; render still sees one consolidated file.
- **Sync with no ingests and no triage.** File is not written. Next render still consumes whatever was on disk from a prior unsent run, or skips.
- **Render fails to send the HTML.** Per existing skill: don't delete local files on Telegram failure. The summary file is also preserved — next render retries both.
- **Render sends HTML successfully but `sendMessage` fails.** HTML is delivered; summary file is preserved; user is informed. Next render re-attempts the summary send.
- **Stale summary from a much earlier sync.** Acceptable — if the user hasn't rendered between syncs, the bullets describe everything since the last successful delivery. The file lives in `data/`, not `/tmp/`, so it survives reboots.

## Telegram caption (unchanged)

The HTML attachment still uses `Finance — <date> · <net worth>` as its caption. The sync bullets are a *separate* message, not appended to the caption. Reasons:

- Caption limit is 1024 chars; a real sync run (5+ files with prose) blows past it.
- Two messages let the user scan the dashboard first, then read the bullets — natural reading order.
- Caption stays terse and predictable for at-a-glance Telegram notifications.

## What changes, file by file

| File | Change |
|---|---|
| `.claude/skills/sync-finance-data/SKILL.md` | Add step 7b: append per-file bullets + triage entries to `data/last_sync_summary.md` (format as specified). Update existing step 7 stdout summary to remain (it's still useful in the conversation). |
| `.claude/skills/render-finance-dashboard/SKILL.md` | Add step 2b after the HTML sendDocument: if `data/last_sync_summary.md` exists, send as a second Telegram text message via `sendMessage`, then delete on success. Update step 3 (Report) to mention summary delivery status. |
| `.gitignore` | Already covers `data/` — no change. Verify. |

No code changes to `scripts/render_dashboard.py` — the Telegram step lives in the skill, not the script. (The script just writes `/tmp/dashboard_meta.json` and the HTML; the skill handles all Telegram I/O via curl.)

## Open questions

None outstanding.

## Out of scope (future iterations)

- A "summary archive" of past sync narratives (would need a `data/sync_history/` folder).
- Per-file diff against prior balances (e.g., "Hapoalim balance: <amount> ILS, +<delta> since last statement").
- Rich Markdown formatting in Telegram (bold tickers, monospace amounts) — text-only is more portable and avoids escaping pitfalls.
