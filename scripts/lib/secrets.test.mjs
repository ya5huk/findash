// Unit tests for the findash secret reader (single consolidated .secrets/findash).
// Run: node --test scripts/lib/secrets.test.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { parseIni, resolveCompanyCreds } from './secrets.mjs';

const HAPOALIM = {
  section: 'hapoalim',
  keys: { user_code: 'HAPOALIM_USER_CODE', password: 'HAPOALIM_PASSWORD' },
};

test('parseIni reads keys under a [section]', () => {
  const ini = parseIni('[hapoalim]\nuser_code=abc\npassword=secret\n');
  assert.deepEqual(ini.hapoalim, { user_code: 'abc', password: 'secret' });
});

test('parseIni ignores blank lines and # / ; comments', () => {
  const ini = parseIni('# comment\n\n[telegram]\n; another\nbot_token=t\n');
  assert.deepEqual(ini.telegram, { bot_token: 't' });
});

test('parseIni splits on the first = only (values may contain =)', () => {
  const ini = parseIni('[pdf-passwords]\nmonthly-payslip=p=ss=word\n');
  assert.equal(ini['pdf-passwords']['monthly-payslip'], 'p=ss=word');
});

test('resolveCompanyCreds maps a [section] to env-var names', () => {
  const creds = resolveCompanyCreds('[hapoalim]\nuser_code=u\npassword=p\n', HAPOALIM);
  assert.deepEqual(creds, { HAPOALIM_USER_CODE: 'u', HAPOALIM_PASSWORD: 'p' });
});

test('resolveCompanyCreds returns nothing for an empty/absent file', () => {
  assert.deepEqual(resolveCompanyCreds('', HAPOALIM), {});
});

test('resolveCompanyCreds ignores other sections and empty values', () => {
  assert.deepEqual(resolveCompanyCreds('[cal]\nusername=x\n', HAPOALIM), {});
  assert.deepEqual(resolveCompanyCreds('[hapoalim]\nuser_code=\npassword=p\n', HAPOALIM), {
    HAPOALIM_PASSWORD: 'p',
  });
});
