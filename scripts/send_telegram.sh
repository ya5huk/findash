#!/bin/bash
# Deterministic Telegram delivery for the finance dashboard.
#
# Why this exists: the unattended `claude -p` daily run can't construct the
# read-secret + curl send inline (the permission analyzer won't auto-approve a
# command that sources .secrets and expands ${bot_token}), and can't write a
# helper at runtime (Write tool isn't granted). So delivery lives here as ONE
# committed, allowlisted command — the token is read at runtime and never printed.
#
# Mirrors render-finance-dashboard SKILL.md steps 2-3: sends output/dashboard.html
# as a document (caption from /tmp/dashboard_meta.json), then data/last_sync_summary.md
# as Telegram-formatted message(s) if present (deleting it on success).
#
# Usage: scripts/send_telegram.sh [--note "<extra caption line>"] [--dry-run]
#   --note     append a line to the caption (e.g. a best-effort fetch warning)
#   --dry-run  print what would be sent, send nothing
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

NOTE=""
DRY=0
while [ $# -gt 0 ]; do
  case "$1" in
    --note) NOTE="${2:-}"; shift 2 ;;
    --dry-run) DRY=1; shift ;;
    *) echo "send_telegram: unknown arg '$1'" >&2; exit 2 ;;
  esac
done

if [ ! -f .secrets/telegram ]; then
  echo "Telegram delivery skipped: .secrets/telegram not configured — see CLAUDE.md"
  exit 0
fi
if [ ! -f output/dashboard.html ]; then
  echo "send_telegram: output/dashboard.html not found — run the renderer first" >&2
  exit 1
fi

NOTE="$NOTE" DRY="$DRY" python3 - <<'PY'
import html, json, os, re, sys, urllib.request, urllib.error, uuid

sec = {}
for ln in open('.secrets/telegram', encoding='utf-8'):
    ln = ln.strip()
    if ln and not ln.startswith('#') and '=' in ln:
        k, v = ln.split('=', 1)
        sec[k.strip()] = v.strip()
bot, chat = sec.get('bot_token'), sec.get('chat_id')
if not bot or not chat:
    print("Telegram delivery skipped: bot_token/chat_id missing in .secrets/telegram")
    sys.exit(0)

try:
    m = json.load(open('/tmp/dashboard_meta.json'))
    as_of, nw = m.get('as_of', ''), m.get('net_worth_text', '')
except Exception:
    as_of, nw = '', ''

dry = os.environ.get('DRY') == '1'
note = os.environ.get('NOTE', '').strip()
caption = f"Finance — {as_of} · {nw}".strip(' —·')
if note:
    caption += "\n" + note


def post(method, fields, files=None):
    b = uuid.uuid4().hex
    body = b''
    for n, v in fields.items():
        body += (f'--{b}\r\nContent-Disposition: form-data; name="{n}"\r\n\r\n{v}\r\n').encode()
    for n, (fn, ct, data) in (files or {}).items():
        body += (f'--{b}\r\nContent-Disposition: form-data; name="{n}"; '
                 f'filename="{fn}"\r\nContent-Type: {ct}\r\n\r\n').encode() + data + b'\r\n'
    body += (f'--{b}--\r\n').encode()
    req = urllib.request.Request(
        f'https://api.telegram.org/bot{bot}/{method}',
        data=body, headers={'Content-Type': f'multipart/form-data; boundary={b}'})
    try:
        return json.load(urllib.request.urlopen(req, timeout=60))
    except urllib.error.HTTPError as e:
        try:
            return json.load(e)
        except Exception:
            return {'ok': False, 'description': f'HTTP {e.code}'}
    except Exception as e:
        return {'ok': False, 'description': str(e)}


SECTION_LABELS = {
    'Ingested': 'New',
    'Triaged': 'Filed',
}
KNOWN_SECTIONS = {
    'New', 'Cleaned', 'Filed', 'Fixed', 'Updated', 'Notes',
    'Ingested', 'Triaged',
}
BOLD_RE = re.compile(r'\*\*([^*\n]+)\*\*')


def html_inline(text):
    out, last = [], 0
    for match in BOLD_RE.finditer(text):
        out.append(html.escape(text[last:match.start()], quote=False))
        out.append(f"<b>{html.escape(match.group(1), quote=False)}</b>")
        last = match.end()
    out.append(html.escape(text[last:], quote=False))
    return ''.join(out)


def summary_as_telegram_html(raw):
    title = "Sync"
    if as_of:
        title += f" · {as_of}"
    out = [f"<b>{html.escape(title, quote=False)}</b>"]
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith('##'):
            label = stripped.lstrip('#').strip().rstrip(':')
            label = SECTION_LABELS.get(label, label)
            if out[-1] != '':
                out.append('')
            out.append(f"<b>{html.escape(label, quote=False)}</b>")
            continue
        if stripped.rstrip(':') in KNOWN_SECTIONS:
            label = stripped.rstrip(':')
            label = SECTION_LABELS.get(label, label)
            if out[-1] != '':
                out.append('')
            out.append(f"<b>{html.escape(label, quote=False)}</b>")
            continue
        if stripped.startswith('- '):
            out.append('• ' + html_inline(stripped[2:].strip()))
            continue
        out.append(html_inline(stripped))
    return '\n'.join(out).strip()


# 1) dashboard document
fn = f"finance-{as_of or 'latest'}.html"
if dry:
    print(f"[dry-run] sendDocument {fn} | caption={caption!r}")
else:
    r = post('sendDocument', {'chat_id': chat, 'caption': caption},
             {'document': (fn, 'text/html', open('output/dashboard.html', 'rb').read())})
    if not r.get('ok'):
        print("Telegram delivery failed:", r.get('description'))
        sys.exit(1)
    print("Sent to chat", r['result']['chat']['id'])

# 2) sync summary (optional) — split on line boundaries, <=3900 chars/message
p = 'data/last_sync_summary.md'
if os.path.exists(p):
    text = summary_as_telegram_html(open(p, encoding='utf-8').read())
    parts, cur = [], ''
    for line in text.split('\n'):
        if cur and len(cur) + len(line) + 1 > 3900:
            parts.append(cur)
            cur = ''
        cur += line + '\n'
    if cur.strip():
        parts.append(cur)
    if dry:
        print(f"[dry-run] sendMessage: sync summary in {len(parts)} part(s)")
    else:
        for part in parts:
            r = post('sendMessage', {
                'chat_id': chat,
                'text': part,
                'parse_mode': 'HTML',
                'disable_web_page_preview': 'true',
            })
            if not r.get('ok'):
                print("Sync summary: Failed:", r.get('description'))
                sys.exit(1)
        os.remove(p)
        print(f"Sync summary: Sent ({len(parts)} message(s))")
else:
    print("Sync summary: Skipped (no summary)")
PY
