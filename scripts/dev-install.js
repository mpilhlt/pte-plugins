/**
 * Install all plugins of this repository into the environment of a PDF-TEI-Editor checkout
 * ("core") as editable packages, together with their Python dependencies.
 *
 * The editor finds them through their `pte.plugins` entry points and imports them from
 * plugins/<name>/src, so changes take effect without reinstalling or publishing. The checkout
 * is taken from the PTE_ROOT environment variable, by default `../pdf-tei-editor`.
 *
 * `uv sync` in the editor removes the editable installs again; run this script afterwards.
 *
 * Usage:
 *   npm run dev:install
 */

import { execFileSync } from 'node:child_process';
import { existsSync, readdirSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const pteRoot = resolve(process.env.PTE_ROOT ?? join(repoRoot, '..', 'pdf-tei-editor'));
const python = join(pteRoot, '.venv', 'bin', 'python');

if (!existsSync(python)) {
  console.error(`No editor environment at ${python}; run "uv sync" in ${pteRoot} first, or set PTE_ROOT`);
  process.exit(1);
}

const pluginsDir = join(repoRoot, 'plugins');
const pluginDirs = readdirSync(pluginsDir, { withFileTypes: true })
  .filter(entry => entry.isDirectory() && existsSync(join(pluginsDir, entry.name, 'pyproject.toml')))
  .map(entry => join(pluginsDir, entry.name));

console.log(`Installing ${pluginDirs.length} plugins into ${python}`);
execFileSync('uv', ['pip', 'install', '--python', python, ...pluginDirs.flatMap(dir => ['-e', dir])], {
  stdio: 'inherit',
});
