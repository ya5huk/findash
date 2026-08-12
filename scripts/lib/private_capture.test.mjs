import assert from 'node:assert/strict';
import {
  chmodSync,
  lstatSync,
  mkdirSync,
  readFileSync,
  readdirSync,
  symlinkSync,
  writeFileSync,
} from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { afterEach, test } from 'node:test';
import { mkdtemp, rm } from 'node:fs/promises';

import { writePrivateJsonCapture } from './private_capture.mjs';


const roots = [];

async function temporaryRoot() {
  const root = await mkdtemp(join(tmpdir(), 'findash-capture-'));
  roots.push(root);
  return root;
}

afterEach(async () => {
  await Promise.all(roots.splice(0).map((root) => rm(root, { recursive: true, force: true })));
});

test('writes and replaces one mode-600 JSON capture atomically', async () => {
  const root = await temporaryRoot();
  const destination = writePrivateJsonCapture(root, 'hapoalim', { value: 1 });
  chmodSync(destination, 0o644);
  writePrivateJsonCapture(root, 'hapoalim', { value: 2 });

  assert.deepEqual(JSON.parse(readFileSync(destination, 'utf8')), { value: 2 });
  assert.equal(lstatSync(destination).mode & 0o777, 0o600);
  assert.equal(lstatSync(join(root, 'inbox', 'staging', 'captures')).mode & 0o777, 0o700);
  assert.deepEqual(readdirSync(join(root, 'inbox', 'staging', 'captures')), [
    'hapoalim-latest.json',
  ]);
});

test('refuses a symlink capture without touching its target', async () => {
  const root = await temporaryRoot();
  const directory = join(root, 'inbox', 'staging', 'captures');
  mkdirSync(directory, { recursive: true });
  const target = join(root, 'target');
  writeFileSync(target, 'keep');
  symlinkSync(target, join(directory, 'visaCal-latest.json'));

  assert.throws(
    () => writePrivateJsonCapture(root, 'visaCal', { private: true }),
    /unsafe/,
  );
  assert.equal(readFileSync(target, 'utf8'), 'keep');
});

test('refuses symlinked capture ancestors and unsafe names', async () => {
  const root = await temporaryRoot();
  const outside = join(root, 'outside');
  mkdirSync(outside);
  symlinkSync(outside, join(root, 'inbox'));
  assert.throws(
    () => writePrivateJsonCapture(root, 'hapoalim', {}),
    /unsafe/,
  );
  assert.throws(
    () => writePrivateJsonCapture(root, '../escape', {}),
    /invalid private capture name/,
  );
});
