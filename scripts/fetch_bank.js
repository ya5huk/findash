// Multi-company Israeli-bank scraper wrapper.
//
// Invocation:
//   node scripts/fetch_bank.js --company=<hapoalim|visaCal> [--start-date=YYYY-MM-DD] [--setup]
//
// Credentials are read from .secrets/findash ([hapoalim]/[cal] sections), or env:
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
// Chromium headless on the server with unauthenticated DevTools open on port N.
// Loopback prevents direct network access, but every local process can control
// that browser. This mode therefore requires --acknowledge-local-devtools-risk
// and is only suitable for a trusted, single-user host. SSH-tunnel the port to a
// laptop, attach, and complete login manually. It only seeds the profile.
//
// Output: raw JSON is written atomically to a private fixed capture under
// inbox/staging/captures/. Stdout contains counts only. Failures emit only a
// privacy-safe class to stderr. Exit code 0 on success:true, 1 on provider
// failure, 2 on configuration/browser/capture failure.

import {
  chmodSync,
  closeSync,
  constants,
  mkdirSync,
  existsSync,
  fstatSync,
  lstatSync,
  openSync,
  readSync,
  rmSync,
} from 'node:fs';
import { homedir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { stdin as input, stdout as output } from 'node:process';
import { createInterface } from 'node:readline/promises';
import { fileURLToPath } from 'node:url';
import { isUsableSecret, resolveCompanyCreds } from './lib/secrets.mjs';
import { writePrivateJsonCapture } from './lib/private_capture.mjs';

const PROJECT_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const MAX_SECRETS_BYTES = 1024 * 1024;
process.umask(0o077);

let CompanyTypes;
let createScraper;
let puppeteer;

async function loadDependencies() {
  try {
    const [bankModule, puppeteerModule] = await Promise.all([
      import('israeli-bank-scrapers'),
      import('puppeteer'),
    ]);
    CompanyTypes = bankModule.CompanyTypes ?? bankModule.default?.CompanyTypes;
    createScraper = bankModule.createScraper ?? bankModule.default?.createScraper;
    puppeteer = puppeteerModule.default;
    if (!CompanyTypes || typeof createScraper !== 'function' || !puppeteer?.launch) {
      throw new Error('unexpected dependency interface');
    }
  } catch {
    console.error('Bank scraper dependencies are unavailable; run scripts/install_node_deps.sh.');
    process.exit(2);
  }
}

// Map: --company flag → consolidated [section] + key→envVar.
const SECRETS_MAP = {
  hapoalim: { section: 'hapoalim', keys: { user_code: 'HAPOALIM_USER_CODE', password: 'HAPOALIM_PASSWORD' } },
  visaCal:  { section: 'cal',      keys: { username:  'CAL_USERNAME',       password: 'CAL_PASSWORD' } },
};

// Credentials come from the consolidated `.secrets/findash` [<section>] block.
// An already-set env var always wins (lets a caller override without files).
function loadSecretsIntoEnv(company) {
  const spec = SECRETS_MAP[company];
  if (!spec) return;
  const path = join(PROJECT_ROOT, '.secrets', 'findash');
  let text = '';
  try {
    const secretsDir = join(PROJECT_ROOT, '.secrets');
    if (existsSync(secretsDir) && lstatSync(secretsDir).isSymbolicLink()) {
      throw new Error('private configuration directory is unsafe');
    }
    if (existsSync(path)) {
      let descriptor;
      try {
        descriptor = openSync(
          path,
          constants.O_RDONLY | (constants.O_NOFOLLOW ?? 0),
        );
        const metadata = fstatSync(descriptor);
        if (!metadata.isFile() || metadata.size > MAX_SECRETS_BYTES) {
          throw new Error('private configuration file is unsafe');
        }
        const buffer = Buffer.alloc(MAX_SECRETS_BYTES + 1);
        let bytesRead = 0;
        while (bytesRead < buffer.length) {
          const count = readSync(
            descriptor,
            buffer,
            bytesRead,
            buffer.length - bytesRead,
            null,
          );
          if (count === 0) break;
          bytesRead += count;
        }
        if (bytesRead > MAX_SECRETS_BYTES) {
          throw new Error('private configuration file is too large');
        }
        text = buffer.subarray(0, bytesRead).toString('utf8');
      } finally {
        if (descriptor !== undefined) closeSync(descriptor);
      }
    }
  } catch {
    console.error('Could not read the private bank configuration safely.');
    process.exit(2);
  }
  const creds = resolveCompanyCreds(text, spec);
  for (const [envName, value] of Object.entries(creds)) {
    if (!isUsableSecret(process.env[envName])) process.env[envName] = value;
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

function usage() {
  return 'Usage: node scripts/fetch_bank.js --company=<hapoalim|visaCal> ' +
    '[--start-date=YYYY-MM-DD] [--setup] [--remote-debug-port=<N>] ' +
    '[--acknowledge-local-devtools-risk] [--disable-browser-sandbox]';
}

function parseArgs(argv) {
  const out = {
    company: null,
    setup: false,
    remoteDebugPort: null,
    startDate: null,
    disableBrowserSandbox: false,
    acknowledgeLocalDevtoolsRisk: false,
  };
  for (const arg of argv) {
    if (arg.startsWith('--company=')) out.company = arg.slice('--company='.length);
    else if (arg.startsWith('--start-date=')) out.startDate = arg.slice('--start-date='.length);
    else if (arg === '--setup') out.setup = true;
    else if (arg === '--disable-browser-sandbox') out.disableBrowserSandbox = true;
    else if (arg === '--acknowledge-local-devtools-risk') out.acknowledgeLocalDevtoolsRisk = true;
    else if (arg.startsWith('--remote-debug-port=')) {
      const v = Number(arg.slice('--remote-debug-port='.length));
      if (!Number.isInteger(v) || v < 1 || v > 65535) {
        console.error('Invalid --remote-debug-port; expected an integer 1..65535.');
        process.exit(2);
      }
      out.remoteDebugPort = v;
    } else if (arg === '--help' || arg === '-h') {
      console.error(usage());
      process.exit(0);
    } else {
      console.error(`Unknown argument.\n${usage()}`);
      process.exit(2);
    }
  }
  return out;
}

function browserSandboxArgs(disabled) {
  if (!disabled) return [];
  console.error(
    'WARNING: Chromium sandbox disabled explicitly. Use this only inside a trusted, isolated container.',
  );
  return ['--no-sandbox', '--disable-setuid-sandbox'];
}

// Where to land the browser when seeding the profile in setup modes.
const LOGIN_URLS = {
  hapoalim: 'https://login.bankhapoalim.co.il/cgi-bin/poalwwwc?reqName=getLogonPage',
  visaCal:  'https://www.cal-online.co.il/',
};

async function waitForSetupDone(browser, userDataDir) {
  console.error('');
  console.error('========================================================================');
  console.error('Setup browser is open. Profile: ~/.cache/findash/chromium-profile/<company>/');
  console.error('');
  console.error('In the browser:');
  console.error('  1. Log in normally.');
  console.error('  2. Complete any SMS OTP / CAPTCHA.');
  console.error('  3. Approve or trust this device if the site offers it.');
  console.error('  4. Wait until the account/dashboard page has loaded.');
  console.error('');
  console.error('Come back here and press Enter to close Chromium and flush the profile.');
  console.error('========================================================================');
  console.error('');

  if (input.isTTY) {
    const rl = createInterface({ input, output });
    try {
      await rl.question('Press Enter after setup is complete...');
    } finally {
      rl.close();
    }
  } else {
    console.error('stdin is not interactive; waiting for Ctrl+C / SIGTERM.');
    await new Promise((resolveWait) => {
      const onSig = () => {
        process.off('SIGINT', onSig);
        process.off('SIGTERM', onSig);
        resolveWait();
      };
      process.on('SIGINT', onSig);
      process.on('SIGTERM', onSig);
    });
  }

  console.error('Closing browser to flush profile...');
  await browser.close().catch(() => {});
  console.error('Done. Trusted-device cookie is stored in the private findash browser profile.');
  console.error('Test it with:  node scripts/fetch_bank.js --company=<same-company>');
}

async function runVisibleSetup(company, userDataDir, disableBrowserSandbox) {
  let browser;
  try {
    browser = await puppeteer.launch({
      userDataDir,
      headless: false,
      args: browserSandboxArgs(disableBrowserSandbox),
    });
  } catch (e) {
    console.error('Puppeteer launch failed (browser unavailable or profile unsafe).');
    process.exit(2);
  }

  const pages = await browser.pages();
  const page = pages[0] ?? await browser.newPage();
  await page.setViewport({ width: 1280, height: 900 });
  const loginUrl = LOGIN_URLS[company] ?? 'about:blank';
  try {
    await page.goto(loginUrl, { waitUntil: 'domcontentloaded', timeout: 60000 });
  } catch (e) {
    console.error('(navigation warning: login page did not load; navigate manually in the browser)');
  }

  await waitForSetupDone(browser, userDataDir);
  process.exit(0);
}

async function runRemoteDebugSetup(company, userDataDir, port, disableBrowserSandbox) {
  console.error(
    'SECURITY WARNING: DevTools is unauthenticated. Every process on this host can ' +
    'control the bank browser while this mode is active. Continue only on a trusted, single-user host.',
  );
  let browser;
  try {
    browser = await puppeteer.launch({
      userDataDir,
      headless: true,
      args: [
        ...browserSandboxArgs(disableBrowserSandbox),
        `--remote-debugging-port=${port}`,
        '--remote-debugging-address=127.0.0.1',
        // Chrome 111+ rejects WebSocket connections to /devtools/* unless the
        // Origin header is in an explicit allow-list. The DevTools page we
        // hand the user is served from http://localhost:<port> itself, so its
        // Origin would otherwise be blocked → "WebSocket disconnected".
        // Loopback blocks direct network clients, but other local processes can
        // still reach the unauthenticated DevTools endpoint.
        '--remote-allow-origins=*',
      ],
    });
  } catch (e) {
    console.error('Puppeteer launch failed (browser unavailable or profile unsafe).');
    process.exit(2);
  }

  const page = await browser.newPage();
  await page.setViewport({ width: 1280, height: 900 });
  const loginUrl = LOGIN_URLS[company] ?? 'about:blank';
  try {
    await page.goto(loginUrl, { waitUntil: 'domcontentloaded', timeout: 60000 });
  } catch (e) {
    console.error('(navigation warning: login page did not load; navigate manually from DevTools)');
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
  console.error('Profile: ~/.cache/findash/chromium-profile/<company>/');
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
  console.error('Done. Trusted-device cookie is stored in the private findash browser profile.');
  console.error('Test it with:  node scripts/fetch_bank.js --company=' + company);
  process.exit(0);
}

function buildCredentials(company) {
  if (company === 'hapoalim') {
    const userCode = process.env.HAPOALIM_USER_CODE;
    const password = process.env.HAPOALIM_PASSWORD;
    if (!isUsableSecret(userCode) || !isUsableSecret(password)) {
      console.error(
        'Missing Hapoalim credentials.\n' +
        '  Provide via .secrets/findash ([hapoalim] section: user_code=… / password=…)\n' +
        '  or via env: HAPOALIM_USER_CODE, HAPOALIM_PASSWORD',
      );
      process.exit(2);
    }
    return { userCode, password };
  }
  if (company === 'visaCal') {
    const username = process.env.CAL_USERNAME;
    const password = process.env.CAL_PASSWORD;
    if (!isUsableSecret(username) || !isUsableSecret(password)) {
      console.error(
        'Missing Cal credentials.\n' +
        '  Provide via .secrets/findash ([cal] section: username=… / password=…)\n' +
        '  or via env: CAL_USERNAME, CAL_PASSWORD',
      );
      process.exit(2);
    }
    return { username, password };
  }
  console.error('Unsupported --company value. Use hapoalim or visaCal.');
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
    const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(val);
    if (!match) {
      console.error('Invalid start date; expected YYYY-MM-DD.');
      process.exit(2);
    }
    const [, yearText, monthText, dayText] = match;
    const year = Number(yearText);
    const month = Number(monthText);
    const day = Number(dayText);
    const d = new Date(year, month - 1, day);
    if (d.getFullYear() !== year || d.getMonth() !== month - 1 || d.getDate() !== day) {
      console.error('Invalid start date; expected a real calendar date.');
      process.exit(2);
    }
    return d;
  }
  const d = new Date();
  d.setHours(0, 0, 0, 0);
  d.setDate(d.getDate() - 60);
  return d;
}

function profileDir(company) {
  const cacheRoot = join(homedir(), '.cache');
  const findashCache = join(cacheRoot, 'findash');
  const profiles = join(findashCache, 'chromium-profile');
  const dir = join(profiles, company);
  try {
    mkdirSync(cacheRoot, { recursive: true, mode: 0o700 });
    if (lstatSync(cacheRoot).isSymbolicLink()) {
      throw new Error('profile path may not contain symbolic links');
    }
    for (const path of [findashCache, profiles, dir]) {
      mkdirSync(path, { recursive: true, mode: 0o700 });
      if (lstatSync(path).isSymbolicLink()) {
        throw new Error('profile path may not contain symbolic links');
      }
      chmodSync(path, 0o700);
    }
    return dir;
  } catch {
    console.error('Could not secure the private Chromium profile directory.');
    process.exit(2);
  }
}

// Chrome records the open-tab list in per-profile session-restore files. A
// scrape run that dies mid-flight leaves its tabs recorded there, so the next
// visible --setup launch would restore every accumulated dead tab. Cookies and
// trusted-device state live elsewhere in the profile and are untouched.
function pruneSessionRestoreState(userDataDir) {
  const defaultProfile = join(userDataDir, 'Default');
  const targets = [
    join(defaultProfile, 'Sessions'),
    join(defaultProfile, 'Current Session'),
    join(defaultProfile, 'Current Tabs'),
    join(defaultProfile, 'Last Session'),
    join(defaultProfile, 'Last Tabs'),
  ];
  for (const target of targets) {
    try {
      rmSync(target, { recursive: true, force: true });
    } catch {
      // Best-effort: a locked or unreadable session file never blocks a fetch.
    }
  }
}

function writePrivateCapture(company, result) {
  try {
    return writePrivateJsonCapture(PROJECT_ROOT, company, result);
  } catch {
    console.error('Could not write the private bank capture safely.');
    process.exit(2);
  }
}

async function main() {
  checkNodeVersion();
  const {
    company,
    setup,
    remoteDebugPort,
    startDate: startDateArg,
    disableBrowserSandbox,
    acknowledgeLocalDevtoolsRisk,
  } = parseArgs(process.argv.slice(2));
  if (!company) {
    console.error('Missing --company=<hapoalim|visaCal>.');
    process.exit(2);
  }
  if (!Object.hasOwn(SECRETS_MAP, company)) {
    console.error('Unsupported --company value. Use hapoalim or visaCal.');
    process.exit(2);
  }
  if (setup && remoteDebugPort !== null) {
    console.error('--setup and --remote-debug-port are mutually exclusive.');
    process.exit(2);
  }
  if (acknowledgeLocalDevtoolsRisk && remoteDebugPort === null) {
    console.error('--acknowledge-local-devtools-risk requires --remote-debug-port.');
    process.exit(2);
  }
  if (remoteDebugPort !== null && !acknowledgeLocalDevtoolsRisk) {
    console.error(
      '--remote-debug-port exposes unauthenticated browser control to every local process. ' +
      'Use only on a trusted, single-user host and pass --acknowledge-local-devtools-risk explicitly.',
    );
    process.exit(2);
  }

  await loadDependencies();

  // Visible local setup path: no creds, no scrape. Let the user seed or refresh
  // trusted-device cookies without racing the scraper's login timeouts.
  if (setup) {
    const userDataDir = profileDir(company);
    pruneSessionRestoreState(userDataDir);
    await runVisibleSetup(company, userDataDir, disableBrowserSandbox);
    return;
  }

  // Remote-debug seeding path: no creds, no scrape — just open the login page
  // headlessly and wait for the user to drive it via DevTools over SSH tunnel.
  if (remoteDebugPort !== null) {
    const userDataDir = profileDir(company);
    pruneSessionRestoreState(userDataDir);
    await runRemoteDebugSetup(company, userDataDir, remoteDebugPort, disableBrowserSandbox);
    return;
  }

  const startDate = resolveStartDate(startDateArg);
  loadSecretsIntoEnv(company);
  const credentials = buildCredentials(company);
  const companyId = resolveCompanyId(company);
  const userDataDir = profileDir(company);
  pruneSessionRestoreState(userDataDir);

  let browser;
  try {
    browser = await puppeteer.launch({
      userDataDir,
      headless: true,
      args: browserSandboxArgs(disableBrowserSandbox),
    });
  } catch (e) {
    console.error('Puppeteer launch failed (browser unavailable or profile unsafe).');
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
    console.error('Scrape failed (provider unavailable or re-authentication required).');
    process.exit(1);
  }

  await browser.close().catch(() => {});

  if (!result || result.success !== true) {
    console.error('Scrape failed (provider unavailable or re-authentication required).');
    process.exit(1);
  }

  writePrivateCapture(company, result);
  const accounts = Array.isArray(result.accounts) ? result.accounts : [];
  const transactionCount = accounts.reduce(
    (total, account) => total + (Array.isArray(account?.txns) ? account.txns.length : 0),
    0,
  );
  console.log(
    `Bank capture: ${company} ok (${accounts.length} account(s), ` +
    `${transactionCount} transaction(s)); private JSON staged`,
  );
}

main().catch(() => {
  console.error('Bank fetch failed unexpectedly.');
  process.exitCode = 1;
});
