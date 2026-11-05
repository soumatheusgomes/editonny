// Runs Python 3.10+ with the given arguments. npm scripts use it because Windows has no
// working `python3` (only a Microsoft Store stub) while macOS and Linux may lack `python`.
import { spawnSync } from 'node:child_process';
import { pathToFileURL } from 'node:url';

const CANDIDATES = process.platform === 'win32'
  ? [['python'], ['py', '-3'], ['python3']]
  : [['python3'], ['python']];

export function pythonCommand() {
  for (const [command, ...prefix] of CANDIDATES) {
    const probe = spawnSync(command, [...prefix, '-c', 'import sys; print(sys.version_info >= (3, 10))'], { encoding: 'utf8' });
    if (probe.status === 0 && probe.stdout.trim() === 'True') return [command, ...prefix];
  }
  throw new Error('Python 3.10 or newer was not found on PATH (tried ' + CANDIDATES.map(c => c.join(' ')).join(', ') + ').');
}

if (import.meta.url === pathToFileURL(process.argv[1] ?? '').href) {
  const [command, ...prefix] = pythonCommand();
  const run = spawnSync(command, [...prefix, ...process.argv.slice(2)], { stdio: 'inherit' });
  if (run.error) throw run.error;
  process.exit(run.status ?? 1);
}
