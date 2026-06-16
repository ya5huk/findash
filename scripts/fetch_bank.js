// Multi-company Israeli-bank scraper wrapper.
//
// Invocation:
//   node scripts/fetch_bank.js --company=<hapoalim|visaCal> [--start-date=YYYY-MM-DD] [--setup] [--remote-debug-port=<N>]
//
// Credentials are read from .secrets/<company> (loaded automatically) or env:
//   hapoalim → HAPOALIM_USER_CODE, HAPOALIM_PASSWORD
//   visaCal  → CAL_USERNAME, CAL_PASSWORD
// The scraper reads creds itself, so they never need to appear on the command
// line — keeping the unattended invocation allowlist-safe and leak-free.
// Start date: --start-date=YYYY-MM-DD flag, else START_DATE env, else 60 days back.
//
// --setup launches a visible browser locally so login + 2FA / CAPTCHA can be
// solved interactively; the resulting cookies persist in the per-company
// profile dir (~/.cache/findash/chromium-profile/<companyId>/).
//
// --remote-debug-port=N (server-without-display alternative): launches
// Chromium headless on the server with DevTools open on port N. SSH-tunnel
// that port to a laptop, open chrome://inspect, attach, complete the login
// manually. Cookie persists in the same profile dir. Press Ctrl+C to close
// once the trusted-device cookie is seeded. Skips the scrape step entirely;
// just seeds the profile.
//
// Output: pretty-printed JSON of the library's scrape result to stdout. Exit
// code 0 on success:true, 1 on success:false (errorType/errorMessage to stderr),
// 2 on Node version too old / missing creds / launch failure.

import { mkdirSync, readFileSync, existsSync } from 'node:fs';
import { homedir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { CompanyTypes, createScraper } from 'israeli-bank-scrapers';
import puppeteer from 'puppeteer';

const PROJECT_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');

// Map: --company flag → (.secrets/<filename>, { fileKey: envVarName })
const SECRETS_MAP = {
  hapoalim: { file: 'hapoalim', keys: { user_code: 'HAPOALIM_USER_CODE', password: 'HAPOALIM_PASSWORD' } },
  visaCal:  { file: 'cal',      keys: { username:  'CAL_USERNAME',       password: 'CAL_PASSWORD' } },
};

function loadSecretsIntoEnv(company) {
  const spec = SECRETS_MAP[company];
  if (!spec) return;
  const path = join(PROJECT_ROOT, '.secrets', spec.file);
  if (!existsSync(path)) return;
  const text = readFileSync(path, 'utf8');
  for (const rawLine of text.split('\n')) {
    const line = rawLine.trim();
    if (!line || line.startsWith('#')) continue;
    const eq = line.indexOf('=');
    if (eq < 0) continue;
    const key = line.slice(0, eq).trim();
    const value = line.slice(eq + 1).trim();
    const envName = spec.keys[key];
    if (envName && !process.env[envName]) process.env[envName] = value;
  }
}

const REQUIRED_NODE_MAJOR = 22;
const REQUIRED_NODE_MINOR = 13;

function checkNodeVersion() {
  const [major, minor] = process.versions.node.split('.').map(Number);
  if (major < REQUIRED_NODE_MAJOR || (major === REQUIRED_NODE_MAJOR && minor < REQUIRED_NODE_MINOR)) {
    console.error(
      `Node ${process.versions.node} too old; israeli-bank-scrapers needs >= ${REQUIRED_NODE_MAJOR}.${REQUIRED_NODE_MINOR}.0.\n` +
      `Install via nvm:  nvm install 22 && nvm use 22`,
    );
    process.exit(2);
  }
}

function parseArgs(argv) {
  const out = { company: null, setup: false, remoteDebugPort: null, startDate: null };
  for (const arg of argv) {
    if (arg.startsWith('--company=')) out.company = arg.slice('--company='.length);
    else if (arg.startsWith('--start-date=')) out.startDate = arg.slice('--start-date='.length);
    else if (arg === '--setup') out.setup = true;
    else if (arg.startsWith('--remote-debug-port=')) {
      const v = Number(arg.slice('--remote-debug-port='.length));
      if (!Number.isInteger(v) || v < 1 || v > 65535) {
        console.error(`Invalid --remote-debug-port=${arg.slice('--remote-debug-port='.length)}; expected an integer 1..65535.`);
        process.exit(2);
      }
      out.remoteDebugPort = v;
    } else if (arg === '--help' || arg === '-h') {
      console.error('Usage: node scripts/fetch_bank.js --company=<hapoalim|visaCal> [--start-date=YYYY-MM-DD] [--setup] [--remote-debug-port=<N>]');
      process.exit(0);
    }
  }
  return out;
}

// Where to land the browser when seeding the profile in remote-debug mode.
const LOGIN_URLS = {
  hapoalim: 'https://login.bankhapoalim.co.il/ng-portals/auth/he/',
  visaCal:  'https://www.cal-online.co.il/',
};

async function runRemoteDebugSetup(company, userDataDir, port) {
  let browser;
  try {
    browser = await puppeteer.launch({
      userDataDir,
      headless: true,
      args: [
        '--no-sandbox',
        `--remote-debugging-port=${port}`,
        '--remote-debugging-address=127.0.0.1',
        // Chrome 111+ rejects WebSocket connections to /devtools/* unless the
        // Origin header is in an explicit allow-list. The DevTools page we
        // hand the user is served from http://localhost:<port> itself, so its
        // Origin would otherwise be blocked → "WebSocket disconnected".
        // Safe here: the port binds only to 127.0.0.1 and is reachable
        // exclusively through the SSH tunnel.
        '--remote-allow-origins=*',
      ],
    });
  } catch (e) {
    console.error(`Puppeteer launch failed: ${e.message}`);
    process.exit(2);
  }

  const page = await browser.newPage();
  await page.setViewport({ width: 1280, height: 900 });
  const loginUrl = LOGIN_URLS[company] ?? 'about:blank';
  try {
    await page.goto(loginUrl, { waitUntil: 'domcontentloaded', timeout: 60000 });
  } catch (e) {
    console.error(`(navigation warning: ${e.message} — continuing anyway; you can navigate manually from DevTools)`);
  }

  // Ask Chromium for its own DevTools-frontend URL for this page, so the user
  // can open it directly in their laptop browser instead of going through the
  // chrome://inspect "Configure → add host:port" dance.
  let frontendUrl = null;
  try {
    const resp = await fetch(`http://127.0.0.1:${port}/json/list`);
    const targets = await resp.json();
    const target = targets.find((t) => t.type === 'page') ?? targets[0];
    if (target?.devtoolsFrontendUrl) {
      // devtoolsFrontendUrl is path-relative ("/devtools/inspector.html?ws=…");
      // prefix with the SSH-tunneled origin so the user can paste it directly.
      frontendUrl = `http://localhost:${port}${target.devtoolsFrontendUrl}`;
    }
  } catch (e) {
    // Non-fatal — fall back to the chrome://inspect instructions.
  }

  console.error('');
  console.error('========================================================================');
  console.error(`Remote-debug Chromium running, DevTools listening on 127.0.0.1:${port}`);
  console.error(`Profile dir: ${userDataDir}`);
  console.error('');
  console.error('On your laptop:');
  console.error(`  1. Open an SSH tunnel (keep this terminal open):`);
  console.error(`       ssh -L ${port}:localhost:${port} <this-server>`);
  if (frontendUrl) {
    console.error('  2. Paste this URL into your laptop browser:');
    console.error(`       ${frontendUrl}`);
    console.error('     (a DevTools tab opens with the bank page rendered inside.)');
  } else {
    console.error('  2. Open chrome://inspect/#devices in your laptop Chrome.');
    console.error(`     Click "Configure..." → add  localhost:${port}`);
    console.error('     Wait a moment, click "inspect" under the page target.');
  }
  console.error('  3. Log in + complete SMS / CAPTCHA in the rendered page.');
  console.error('     The trusted-device cookie writes into the profile dir on the server.');
  console.error('  4. Come back here and press Ctrl+C to close cleanly.');
  console.error('========================================================================');
  console.error('');

  await new Promise((resolveWait) => {
    const onSig = () => {
      process.off('SIGINT', onSig);
      process.off('SIGTERM', onSig);
      resolveWait();
    };
    process.on('SIGINT', onSig);
    process.on('SIGTERM', onSig);
  });

  console.error('Closing browser to flush profile…');
  await browser.close().catch(() => {});
  console.error(`Done. Trusted-device cookie should now live in ${userDataDir}`);
  console.error('Test it with:  node scripts/fetch_bank.js --company=' + company);
  process.exit(0);
}

function buildCredentials(company) {
  if (company === 'hapoalim') {
    const userCode = process.env.HAPOALIM_USER_CODE;
    const password = process.env.HAPOALIM_PASSWORD;
    if (!userCode || !password) {
      console.error(
        'Missing Hapoalim credentials.\n' +
        '  Provide via .secrets/hapoalim (lines: user_code=… / password=…)\n' +
        '  or via env: HAPOALIM_USER_CODE, HAPOALIM_PASSWORD',
      );
      process.exit(2);
    }
    return { userCode, password };
  }
  if (company === 'visaCal') {
    const username = process.env.CAL_USERNAME;
    const password = process.env.CAL_PASSWORD;
    if (!username || !password) {
      console.error(
        'Missing Cal credentials.\n' +
        '  Provide via .secrets/cal (lines: username=… / password=…)\n' +
        '  or via env: CAL_USERNAME, CAL_PASSWORD',
      );
      process.exit(2);
    }
    return { username, password };
  }
  console.error(`Unsupported --company=${company}. Use hapoalim or visaCal.`);
  process.exit(2);
}

function resolveCompanyId(company) {
  const id = CompanyTypes[company];
  if (!id) {
    console.error(`Unknown CompanyTypes.${company}. Library may have renamed it.`);
    process.exit(2);
  }
  return id;
}

function resolveStartDate(flagVal) {
  const val = flagVal || process.env.START_DATE;
  if (val) {
    const d = new Date(val);
    if (Number.isNaN(d.valueOf())) {
      console.error(`Invalid start date "${val}"; expected YYYY-MM-DD.`);
      process.exit(2);
    }
    return d;
  }
  const d = new Date();
  d.setDate(d.getDate() - 60);
  return d;
}

function profileDir(company) {
  const dir = join(homedir(), '.cache', 'findash', 'chromium-profile', company);
  mkdirSync(dir, { recursive: true });
  return dir;
}

async function main() {
  checkNodeVersion();
  const { company, setup, remoteDebugPort, startDate: startDateArg } = parseArgs(process.argv.slice(2));
  if (!company) {
    console.error('Missing --company=<hapoalim|visaCal>.');
    process.exit(2);
  }

  const userDataDir = profileDir(company);

  // Remote-debug seeding path: no creds, no scrape — just open the login page
  // headlessly and wait for the user to drive it via DevTools over SSH tunnel.
  if (remoteDebugPort !== null) {
    await runRemoteDebugSetup(company, userDataDir, remoteDebugPort);
    return;
  }

  loadSecretsIntoEnv(company);
  const credentials = buildCredentials(company);
  const companyId = resolveCompanyId(company);
  const startDate = resolveStartDate(startDateArg);

  let browser;
  try {
    browser = await puppeteer.launch({
      userDataDir,
      headless: !setup,
      args: ['--no-sandbox'],
    });
  } catch (e) {
    console.error(`Puppeteer launch failed: ${e.message}`);
    process.exit(2);
  }

  let result;
  try {
    const scraper = createScraper({
      companyId,
      startDate,
      combineInstallments: false,
      browser,
      skipCloseBrowser: true,
    });
    result = await scraper.scrape(credentials);
  } catch (e) {
    await browser.close().catch(() => {});
    console.error(`Scrape threw: ${e.message}`);
    process.exit(1);
  }

  await browser.close().catch(() => {});

  process.stdout.write(JSON.stringify(result, null, 2) + '\n');

  if (!result || result.success !== true) {
    const t = result?.errorType ?? 'unknown';
    const m = result?.errorMessage ?? '(no message)';
    console.error(`Scrape failed: errorType=${t} errorMessage=${m}`);
    process.exit(1);
  }
}

main();
