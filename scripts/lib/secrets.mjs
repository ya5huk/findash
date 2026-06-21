// Pure helpers for reading findash secrets. No I/O and no side effects — callers
// pass file *contents* in — so the parsing is trivially unit-testable.
// See scripts/lib/secrets.test.mjs.

// Parse a minimal INI: `[section]` headers and `key=value` lines, with `#` / `;`
// comments. Lines before the first header live under the '' (default) section.
export function parseIni(text) {
  const out = { '': {} };
  let section = '';
  for (const raw of String(text).split('\n')) {
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

// Resolve `{ ENV_VAR: value }` for one company from the consolidated
// `.secrets/findash` `[section]`. Pass '' (or null) for an absent file.
export function resolveCompanyCreds(consolidatedText, spec) {
  const section = consolidatedText ? parseIni(consolidatedText)[spec.section] || {} : {};
  const env = {};
  for (const [fileKey, envName] of Object.entries(spec.keys)) {
    const value = section[fileKey];
    if (value != null && value !== '') env[envName] = value;
  }
  return env;
}
