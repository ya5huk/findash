// Pure helpers for reading findash secrets. No I/O and no side effects — callers
// pass file *contents* in — so the parsing is trivially unit-testable.
// See scripts/lib/secrets.test.mjs.

// Parse a minimal INI: `[section]` headers and `key=value` lines, with `#` / `;`
// comments. Lines before the first header live under the '' (default) section.
export function parseIni(text) {
  const out = { '': {} };
  let section = '';
  const normalizedText = String(text).replace(/^\uFEFF/, '');
  for (const raw of normalizedText.split('\n')) {
    const line = raw.trim();
    if (!line || line[0] === '#' || line[0] === ';') continue;
    const header = line.match(/^\[(.+)\]$/);
    if (header) {
      section = header[1].trim();
      if (!out[section]) out[section] = {};
      continue;
    }
    const eq = line.indexOf('=');
    if (eq < 0) continue;
    out[section][line.slice(0, eq).trim()] = line.slice(eq + 1).trim();
  }
  return out;
}

const PLACEHOLDER_WORDS = new Set([
  'changeme',
  'change-me',
  'replace-me',
  'replace_me',
  'todo',
  'your-id',
  'your-value',
  'your_chat_id',
  'your_password',
  'your_root_folder_id',
  'your_token',
  'your_user_code',
  'your_username',
  'your_value',
]);

export function isPlaceholderValue(value) {
  const normalized = String(value ?? '').trim();
  if (!normalized) return false;
  const lowered = normalized.toLowerCase();
  return (
    (normalized.startsWith('<') && normalized.endsWith('>')) ||
    PLACEHOLDER_WORDS.has(lowered)
  );
}

export function isUsableSecret(value) {
  return String(value ?? '').trim() !== '' && !isPlaceholderValue(value);
}

// Resolve `{ ENV_VAR: value }` for one company from the consolidated
// `.secrets/findash` `[section]`. Pass '' (or null) for an absent file.
export function resolveCompanyCreds(consolidatedText, spec) {
  const section = consolidatedText ? parseIni(consolidatedText)[spec.section] || {} : {};
  const env = {};
  for (const [fileKey, envName] of Object.entries(spec.keys)) {
    const value = section[fileKey];
    if (isUsableSecret(value)) env[envName] = value;
  }
  return env;
}
