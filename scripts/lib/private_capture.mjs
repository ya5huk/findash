/** Atomic private JSON capture used by the bank scraper wrapper. */

import {
  chmodSync,
  existsSync,
  lstatSync,
  mkdirSync,
  renameSync,
  unlinkSync,
  writeFileSync,
} from 'node:fs';
import { join, resolve } from 'node:path';


export function writePrivateJsonCapture(projectRoot, captureName, value) {
  if (!/^[A-Za-z0-9_-]+$/.test(captureName)) {
    throw new Error('invalid private capture name');
  }
  const root = resolve(projectRoot);
  const paths = [
    join(root, 'inbox'),
    join(root, 'inbox', 'staging'),
    join(root, 'inbox', 'staging', 'captures'),
  ];
  for (const path of paths) {
    mkdirSync(path, { recursive: true, mode: 0o700 });
    const metadata = lstatSync(path);
    if (metadata.isSymbolicLink() || !metadata.isDirectory()) {
      throw new Error('private capture path is unsafe');
    }
    chmodSync(path, 0o700);
  }

  const directory = paths.at(-1);
  const destination = join(directory, `${captureName}-latest.json`);
  const temporary = join(
    directory,
    `.${captureName}-${process.pid}-${Date.now()}.tmp`,
  );
  try {
    if (existsSync(destination)) {
      const metadata = lstatSync(destination);
      if (metadata.isSymbolicLink() || !metadata.isFile()) {
        throw new Error('private capture destination is unsafe');
      }
    }
    writeFileSync(temporary, `${JSON.stringify(value, null, 2)}\n`, {
      encoding: 'utf8',
      flag: 'wx',
      mode: 0o600,
    });
    chmodSync(temporary, 0o600);
    renameSync(temporary, destination);
    chmodSync(destination, 0o600);
    return destination;
  } catch (error) {
    try { unlinkSync(temporary); } catch {}
    throw error;
  }
}
