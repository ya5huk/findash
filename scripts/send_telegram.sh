#!/bin/bash
# Deterministic Telegram delivery for the finance dashboard.
#
# Delivery lives in one committed helper so the token is read at runtime and
# never expanded into an agent command or printed.
#
# Used by send-finance-dashboard: sends output/dashboard.html
# as a document (caption read from that same HTML), then data/last_sync_summary.md
# as Telegram-formatted message(s) if present (deleting it on success).
#
# Usage: scripts/send_telegram.sh [--note "<extra caption line>"] [--dry-run]
#                                 [--allow-stale] [--alert "<status line>"]
#   --note     append a line to the caption (e.g. a best-effort fetch warning)
#   --alert    send a plain-text status instead of the dashboard, chunked only
#              when Telegram's limit requires it (used for unattended failures)
#   --dry-run  validate locally and print actions; no secrets or network needed
#   --allow-stale  explicitly permit a dashboard older than the freshness limit
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
umask 077

NOTE=""
DRY=0
MODE=""
ALERT_TEXT=""
ALLOW_STALE=0
MAX_AGE_SECONDS="${FINDASH_DASHBOARD_MAX_AGE_SECONDS:-1800}"
need_value() {
  [ "$#" -ge 2 ] || { echo "send_telegram: $1 requires a value" >&2; exit 2; }
}
while [ $# -gt 0 ]; do
  case "$1" in
    --note) need_value "$@"; NOTE="$2"; shift 2 ;;
    --alert) need_value "$@"; MODE="alert"; ALERT_TEXT="$2"; shift 2 ;;
    --dry-run) DRY=1; shift ;;
    --allow-stale) ALLOW_STALE=1; shift ;;
    --max-age-seconds) need_value "$@"; MAX_AGE_SECONDS="$2"; shift 2 ;;
    *) echo "send_telegram: unknown argument" >&2; exit 2 ;;
  esac
done

case "$MAX_AGE_SECONDS" in
  ''|*[!0-9]*) echo "send_telegram: --max-age-seconds must be a positive integer" >&2; exit 2 ;;
esac
[ "$MAX_AGE_SECONDS" -gt 0 ] || { echo "send_telegram: --max-age-seconds must be positive" >&2; exit 2; }
[ "$MODE" != "alert" ] || [ -n "$ALERT_TEXT" ] || { echo "send_telegram: --alert must not be empty" >&2; exit 2; }
[ "$MODE" = "alert" ] || [ -z "$ALERT_TEXT" ] || { echo "send_telegram: invalid alert state" >&2; exit 2; }

NOTE="$NOTE" DRY="$DRY" MODE="$MODE" ALERT_TEXT="$ALERT_TEXT" \
ALLOW_STALE="$ALLOW_STALE" MAX_AGE_SECONDS="$MAX_AGE_SECONDS" python3 - <<'PY'
import html, json, os, re, sys, time, urllib.request, urllib.error, uuid
from html.parser import HTMLParser

sys.path.insert(0, 'scripts/lib')
dry = os.environ.get('DRY') == '1'
if dry:
    bot = chat = None
else:
    import findash_secrets
    try:
        sec = findash_secrets.read_section('telegram')  # .secrets/findash [telegram]
    except (OSError, UnicodeError, ValueError):
        print("send_telegram: private configuration is unsafe or unreadable", file=sys.stderr)
        sys.exit(1)
    bot, chat = sec.get('bot_token'), sec.get('chat_id')
    if not all(findash_secrets.is_usable_secret(value) for value in (bot, chat)):
        print("Telegram delivery skipped: bot_token/chat_id missing in .secrets/findash [telegram]")
        sys.exit(0)

note = os.environ.get('NOTE', '').strip()
MESSAGE_LIMIT = 4096
CAPTION_LIMIT = 1024
MAX_SUMMARY_BYTES = 256 * 1024


def utf16_units(text):
    """Return Telegram's text-length unit without splitting non-BMP characters."""
    return len(text.encode('utf-16-le')) // 2


def split_plain_utf16(text, limit=MESSAGE_LIMIT):
    if limit < 2:
        raise ValueError('Telegram chunk limit must fit one Unicode character')
    parts, current, current_units = [], [], 0
    for character in text:
        character_units = 2 if ord(character) > 0xFFFF else 1
        if current and current_units + character_units > limit:
            parts.append(''.join(current))
            current, current_units = [], 0
        current.append(character)
        current_units += character_units
    if current:
        parts.append(''.join(current))
    return parts


def truncate_plain_utf16(text, limit):
    if utf16_units(text) <= limit:
        return text
    suffix = '…'
    head_limit = limit - utf16_units(suffix)
    return split_plain_utf16(text, head_limit)[0] + suffix


def read_dashboard():
    path = 'output/dashboard.html'
    if os.path.islink('output'):
        print("send_telegram: output/ must not be a symlink", file=sys.stderr)
        sys.exit(1)
    try:
        st = os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        print("send_telegram: output/dashboard.html not found — run the renderer first", file=sys.stderr)
        sys.exit(1)
    if not os.path.isfile(path) or os.path.islink(path):
        print("send_telegram: dashboard path must be a regular file", file=sys.stderr)
        sys.exit(1)
    age = time.time() - st.st_mtime
    max_age = int(os.environ['MAX_AGE_SECONDS'])
    not_before_text = os.environ.get('FINDASH_DASHBOARD_NOT_BEFORE_EPOCH', '')
    try:
        not_before = int(not_before_text) if not_before_text else None
    except ValueError:
        print("send_telegram: invalid dashboard freshness marker", file=sys.stderr)
        sys.exit(1)
    stale_for_run = not_before is not None and st.st_mtime < not_before
    allow_stale = os.environ.get('ALLOW_STALE') == '1'
    if stale_for_run or (not allow_stale and (age < -300 or age > max_age)):
        print(
            f"send_telegram: dashboard is not fresh for this run ({max(0, int(age))}s old); "
            "render again or pass --allow-stale explicitly",
            file=sys.stderr,
        )
        sys.exit(1)
    if not dry:
        try:
            os.chmod(path, 0o600)
        except OSError as exc:
            print(f"send_telegram: could not secure dashboard permissions: {exc}", file=sys.stderr)
            sys.exit(1)
    with open(path, 'rb') as stream:
        data = stream.read()
    text = data.decode('utf-8', errors='replace')
    date_match = re.search(r'<div\s+class="as-of-date">\s*As of\s+([^<]+)</div>', text)
    worth_match = re.search(r'<div\s+class="headline-amount">\s*([^<]+)</div>', text)
    if not date_match or not worth_match:
        print("send_telegram: dashboard metadata missing; render again", file=sys.stderr)
        sys.exit(1)
    as_of = html.unescape(date_match.group(1).strip()) if date_match else ''
    net_worth = html.unescape(worth_match.group(1).strip()) if worth_match else ''
    return path, data, as_of, net_worth


dashboard_path = dashboard_data = None
as_of = nw = ''
if os.environ.get('MODE') != 'alert':
    dashboard_path, dashboard_data, as_of, nw = read_dashboard()

caption = f"Finance — {as_of} · {nw}".strip(' —·')
if note:
    caption += "\n" + note
caption = truncate_plain_utf16(caption, CAPTION_LIMIT)


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
        # Exception strings from URL libraries may include the request URL,
        # which contains the bot token. Keep runtime diagnostics secret-safe.
        return {'ok': False, 'description': f'network request failed ({type(e).__name__})'}


# --alert mode: send a plain-text status and stop. No document or sync summary.
if os.environ.get('MODE') == 'alert':
    alert = os.environ.get('ALERT_TEXT', '').strip() or 'findash sync failed.'
    alert_parts = split_plain_utf16(alert)
    if dry:
        largest = max(utf16_units(part) for part in alert_parts)
        print(
            f"[dry-run] sendMessage alert in {len(alert_parts)} part(s); "
            f"max {largest} UTF-16 units"
        )
        sys.exit(0)
    for alert_part in alert_parts:
        r = post('sendMessage', {'chat_id': chat, 'text': alert_part,
                                 'disable_web_page_preview': 'true'})
        if not r.get('ok'):
            print("Telegram alert failed:", r.get('description'))
            sys.exit(1)
    print(f"Telegram alert: Sent ({len(alert_parts)} message(s))")
    sys.exit(0)


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


class TelegramHtmlParser(HTMLParser):
    """Parse the small trusted HTML subset produced above into styled text."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.bold_depth = 0
        self.runs = []

    def handle_starttag(self, tag, attrs):
        if tag != 'b':
            raise ValueError(f'unsupported Telegram HTML tag: {tag}')
        self.bold_depth += 1

    def handle_endtag(self, tag):
        if tag != 'b' or self.bold_depth == 0:
            raise ValueError('unbalanced Telegram HTML')
        self.bold_depth -= 1

    def handle_data(self, data):
        if data:
            self.runs.append((data, self.bold_depth > 0))


def telegram_html_characters(text):
    parser = TelegramHtmlParser()
    parser.feed(text)
    parser.close()
    if parser.bold_depth:
        raise ValueError('unbalanced Telegram HTML')
    return [
        (character, bold)
        for run, bold in parser.runs
        for character in run
    ]


def render_telegram_html(characters):
    out, bold_open = [], False
    for character, bold in characters:
        if bold != bold_open:
            out.append('<b>' if bold else '</b>')
            bold_open = bold
        out.append(html.escape(character, quote=False))
    if bold_open:
        out.append('</b>')
    return ''.join(out)


def telegram_html_units(text):
    return sum(
        2 if ord(character) > 0xFFFF else 1
        for character, _bold in telegram_html_characters(text)
    )


def split_telegram_html(text, limit=MESSAGE_LIMIT):
    """Split valid generated HTML by rendered UTF-16 units, preserving bold tags."""
    characters = telegram_html_characters(text)

    def following_line_fits(line_start):
        line_units = 0
        for character, _bold in characters[line_start:]:
            if character == '\n':
                break
            line_units += 2 if ord(character) > 0xFFFF else 1
            if line_units > limit:
                return False
        return True

    parts, start = [], 0
    while start < len(characters):
        end, used, last_newline = start, 0, None
        while end < len(characters):
            character = characters[end][0]
            character_units = 2 if ord(character) > 0xFFFF else 1
            if used + character_units > limit:
                break
            used += character_units
            if character == '\n':
                last_newline = end
            end += 1

        if end == start:
            raise ValueError('Telegram chunk limit cannot fit a Unicode character')

        next_start = end
        if (
            end < len(characters)
            and last_newline is not None
            and last_newline > start
            and following_line_fits(last_newline + 1)
        ):
            end = last_newline
            next_start = last_newline + 1

        chunk = characters[start:end]
        if chunk:
            parts.append(render_telegram_html(chunk))
        start = next_start
    return parts


# 1) dashboard document
safe_date = as_of if re.fullmatch(r'\d{4}-\d{2}-\d{2}', as_of) else 'latest'
fn = f"finance-{safe_date}.html"
if dry:
    print(
        f"[dry-run] sendDocument {fn} ({len(dashboard_data)} bytes; "
        f"caption {utf16_units(caption)} UTF-16 units)"
    )
else:
    r = post('sendDocument', {'chat_id': chat, 'caption': caption},
             {'document': (fn, 'text/html', dashboard_data)})
    if not r.get('ok'):
        print("Telegram delivery failed:", r.get('description'))
        sys.exit(1)
    print("Telegram delivery: Sent")

# 2) sync summary (optional) — split on line boundaries within Telegram's
# 4096 rendered UTF-16-unit message limit.
p = 'data/last_sync_summary.md'
if os.path.exists(p):
    if os.path.islink('data'):
        print("Sync summary: Failed: data/ must not be a symlink")
        sys.exit(1)
    if os.path.islink(p) or not os.path.isfile(p):
        print("Sync summary: Failed: path must be a regular file")
        sys.exit(1)
    if not dry:
        try:
            os.chmod(p, 0o600)
        except OSError as exc:
            print(f"Sync summary: Failed: could not secure permissions: {exc}")
            sys.exit(1)
    if os.stat(p, follow_symlinks=False).st_size > MAX_SUMMARY_BYTES:
        print("Sync summary: Failed: file is too large")
        sys.exit(1)
    with open(p, 'rb') as stream:
        raw_summary = stream.read(MAX_SUMMARY_BYTES + 1)
    if len(raw_summary) > MAX_SUMMARY_BYTES:
        print("Sync summary: Failed: file is too large")
        sys.exit(1)
    try:
        decoded_summary = raw_summary.decode('utf-8')
    except UnicodeDecodeError:
        print("Sync summary: Failed: file is not valid UTF-8")
        sys.exit(1)
    text = summary_as_telegram_html(decoded_summary)
    parts = split_telegram_html(text)
    if dry:
        largest = max((telegram_html_units(part) for part in parts), default=0)
        print(
            f"[dry-run] sendMessage: sync summary in {len(parts)} part(s); "
            f"max {largest} UTF-16 units"
        )
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
