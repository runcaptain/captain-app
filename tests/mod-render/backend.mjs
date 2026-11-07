// The backend the live render tests run: this repo's backend/core, never an older `cst` on PATH (whose routes may
// differ). Set up once with uv (backend/core/.venv) when it isn't there yet.
import fs from 'node:fs';
import path from 'node:path';
import { spawnSync } from 'node:child_process';

const here = path.dirname(new URL(import.meta.url).pathname);
const core = path.resolve(here, '../../backend/core');

/** The path of backend/core's cst, or null (and why) when it can't be set up here. */
export function cstBin() {
  const bin = path.join(core, '.venv/bin/cst');
  if (fs.existsSync(bin)) return { bin };
  const uv = spawnSync('sh', ['-c', 'command -v uv'], { encoding: 'utf8' }).stdout.trim();
  if (!uv) return { bin: null, why: 'backend/core has no .venv and uv is not installed (see backend/README.md)' };
  console.log('setting up backend/core (uv venv, uv pip install -e .[dev]) once ...');
  const a = spawnSync(uv, ['venv', '-q'], { cwd: core, stdio: 'inherit' });
  const b = a.status === 0 ? spawnSync(uv, ['pip', 'install', '-q', '-e', '.[dev]'], { cwd: core, stdio: 'inherit' }) : a;
  return fs.existsSync(bin) && b.status === 0 ? { bin } : { bin: null, why: 'setting up backend/core failed' };
}
