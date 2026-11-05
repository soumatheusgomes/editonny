// Resolves the agent-browser CLI for execFile. On Windows npm installs only `.cmd`/`.ps1` shims, which
// execFile cannot start without a shell (and a shell would reparse the JSON and script arguments),
// so the shim's JavaScript entry point runs under the current Node instead.
import { existsSync } from 'node:fs';
import { delimiter, join } from 'node:path';

export function agentBrowserCommand() {
  if (process.platform !== 'win32') return ['agent-browser'];
  for (const dir of (process.env.PATH ?? '').split(delimiter).filter(Boolean)) {
    if (existsSync(join(dir, 'agent-browser.exe'))) return [join(dir, 'agent-browser.exe')];
    if (!existsSync(join(dir, 'agent-browser.cmd'))) continue;
    // Local installs keep shims in node_modules/.bin; global installs keep node_modules beside them.
    for (const entry of [join(dir, '..', 'agent-browser', 'bin', 'agent-browser.js'),
      join(dir, 'node_modules', 'agent-browser', 'bin', 'agent-browser.js')]) {
      if (existsSync(entry)) return [process.execPath, entry];
    }
  }
  return ['agent-browser'];
}
