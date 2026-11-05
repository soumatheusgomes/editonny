import test from 'node:test';
import assert from 'node:assert/strict';
import { execFile, spawn } from 'node:child_process';
import { promisify } from 'node:util';
import { createInterface } from 'node:readline';
import { EventEmitter, once } from 'node:events';
import { readFile, writeFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { runInNewContext } from 'node:vm';
import { agentBrowserCommand } from '../scripts/agent-browser.mjs';

for (const [file, expected] of [
  ['tests/preview-browser.test.mjs', ['fixture', 'lines']],
  ['tests/template-scenes.test.mjs', ['server']],
  ['scripts/smoke_three.mjs', ['server', 'project']],
]) {
  test(`${file} cleans up when browser close fails`, async () => {
    const source = await readFile(new URL(`../${file}`, import.meta.url), 'utf8');
    // Run the actual outer finalizer without starting the browser or rendering scenes.
    const finalizer = source.slice(source.search(/^ {0,2}} finally \{/m)).replace(/\n\}\);\s*$/, '');
    const cleaned = [];
    const failure = new Error('Browser close failed');
    const fixture = new EventEmitter();
    fixture.exitCode = null;
    fixture.stdin = { end() { cleaned.push('fixture');fixture.exitCode = 0;fixture.emit('exit', 0); } };
    await assert.rejects(runInNewContext(`(async () => { try { ${finalizer} })()`, {
      browser: async () => { throw failure; },
      clearTimeout, deadline: undefined, once, fixture,
      lines: { close() { cleaned.push('lines'); } },
      server: { close(done) { cleaned.push('server');done(); } },
      rm: async () => { cleaned.push('project'); }, project: 'temporary project',
    }), error => error === failure);
    assert.deepEqual(cleaned, expected);
  });
}

test('preview preserves marks, trims, drafts, and approval confirmation', async () => {
  const root = fileURLToPath(new URL('../', import.meta.url));
  const fixture = spawn('uv', ['run', '--locked', '--directory', `${root}skills/editonny/engine`,
    'python', `${root}tests/integration/preview_fixture.py`], { stdio: ['pipe', 'pipe', 'pipe'] });
  let errors = '';
  fixture.stderr.on('data', chunk => { errors += chunk; });
  const lines = createInterface({ input: fixture.stdout });
  const session = `editonny-preview-${process.pid}`;
  const exec = promisify(execFile);
  const [agentBrowser, ...agentBrowserPrefix] = agentBrowserCommand();
  const browser = async (...args) => {
    const { stdout } = await exec(agentBrowser, [...agentBrowserPrefix, '--session', session, '--json', ...args], { timeout: 30000 });
    const response = JSON.parse(stdout);
    assert.equal(response.success, true, JSON.stringify(response.error));
    return response.data;
  };
  const evaluate = async expression => (await browser('eval', expression)).result;
  let deadline;
  try {
    const line = await Promise.race([
      once(lines, 'line').then(([line]) => line),
      once(fixture, 'exit').then(() => { throw new Error(`Fixture exited: ${errors}`); }),
      new Promise((_, reject) => { deadline = setTimeout(() => reject(new Error(`Fixture unavailable: ${errors}`)), 30000); }),
    ]);
    clearTimeout(deadline);
    const { url, edit } = JSON.parse(line);
    const statePath = `${edit}/state.json`;
    const poll = async message => {
      const state = JSON.parse(await readFile(statePath, 'utf8'));
      await writeFile(statePath, JSON.stringify({ ...state, awaiting: null, message }));
      await browser('wait', '--fn', `document.querySelector('#status-text').textContent === ${JSON.stringify(message)}`);
    };
    const timelinePath = `${edit}/cut_timeline.json`;
    const timeline = JSON.parse(await readFile(timelinePath, 'utf8'));
    timeline.segments[0].beat = '';
    await writeFile(timelinePath, JSON.stringify(timeline));
    await browser('open', url);
    await browser('wait', '--fn', "document.querySelector('#view-cut video').readyState >= 2");
    assert.equal(await evaluate("document.querySelector('#view-cut .seg-beat').textContent"), 'Segment 1');
    await browser('press', 'i');
    await browser('press', 'o');
    await browser('fill', '#view-cut [data-note-input]', 'Check this word');
    await browser('press', 'Enter');
    assert.equal(await evaluate("document.querySelectorAll('#view-cut .mark-item').length"), 1);
    await poll('Polling preserves the draft');
    assert.equal(await evaluate("document.querySelector('#view-cut .mark-note').value"), 'Check this word');
    await browser('focus', '#view-cut [data-approve]');
    await browser('press', 'Enter');
    await browser('wait', '#confirm-dialog[open]');
    assert.match(await evaluate("document.querySelector('[data-confirm-title]').textContent"), /marks/);
    await browser('press', 'Escape');
    await browser('focus', '#view-cut .seg-handle.end');
    await browser('press', 'ArrowRight');
    assert.match(await evaluate("document.querySelector('#view-cut [data-trims-list]').textContent"), /0.01/);
    await browser('focus', '#view-cut [data-approve]');
    await browser('press', 'Enter');
    await browser('wait', '#confirm-dialog[open]');
    assert.match(await evaluate("document.querySelector('[data-confirm-title]').textContent"), /adjustments/);
    await browser('press', 'Escape');
    await browser('click', '#view-cut [data-save-trims]');
    await browser('wait', '#view-cut [data-trims-state].is-saved');
    const trims = JSON.parse(await readFile(`${edit}/preview_trims.json`, 'utf8'));
    assert.equal(trims.trims[0].end_delta, 0.01);
    await browser('click', '#view-cut [data-save-marks]');
    await browser('wait', '#view-cut [data-marks-state].is-saved');
    assert.equal(JSON.parse(await readFile(`${edit}/preview_marks.json`, 'utf8')).marks[0].note, 'Check this word');
    await browser('click', '[data-tab="style"]');
    assert.ok(await evaluate("document.querySelectorAll('#view-style .badge').length > 0"));
    assert.equal(await evaluate("[...document.querySelectorAll('#view-style .badge')].every(el => el.textContent === 'Animated')"), true);
    await browser('fill', '[data-hook-text]', 'My hook');
    await browser('open', `${url}/#style`);
    await browser('wait', '--fn', "document.querySelector('[data-hook-text]').value === 'My hook'");
    await poll('Style preserved after polling');
    assert.equal(await evaluate("document.querySelector('[data-hook-text]').value"), 'My hook');
    await browser('focus', '[data-save-style]');
    await browser('press', 'Enter');
    await browser('wait', '[data-style-saved].is-saved');
    assert.equal(JSON.parse(await readFile(`${edit}/preview_style.json`, 'utf8')).hook_text, 'My hook');
    await writeFile(`${edit}/edit_data.json`, JSON.stringify({ inserts: ['split_top', 'split_bottom', 'full', 'pip'].map(type => ({ type, start: 0, end: 1 })) }));
    await browser('click', '[data-tab="final"]');
    await browser('wait', '--fn', "document.querySelector('#view-final video').readyState >= 2");
    assert.equal(await evaluate("document.querySelectorAll('#view-final .mark-item').length"), 0);
    assert.equal(await evaluate("document.querySelector('#view-final [data-download]').getAttribute('download')"), 'final.mp4');
    await browser('wait', '--fn', "document.querySelectorAll('#view-final .layer-pill').length === 4");
    assert.deepEqual(await evaluate("[...document.querySelectorAll('#view-final .layer-pill')].map(el => el.textContent)"), ['Image above', 'Image below', 'Fullscreen', 'Picture in picture']);
    await writeFile(`${edit}/edit_data.json`, JSON.stringify({ edit_type: 'greenScreen', inserts: [{ type: 'split_top', start: 0, end: 1 }] }));
    await browser('wait', '--fn', "document.querySelector('#view-final .layer-pill')?.textContent === 'Background'");
    const browserErrors = await browser('errors');
    assert.deepEqual(browserErrors.errors, []);
    assert.equal(await evaluate('performance.getEntriesByType("resource").filter(r => r.responseStatus >= 400).length'), 0);
    await writeFile(`${edit}/downloaded.html`, '<!doctype html><p>Untrusted project document</p><script>window.untrustedScriptRan=true;fetch("/api/state").then(()=>window.readPreviewState=true)</script>');
    await browser('open', `${url}/media/downloaded.html`);
    assert.equal(await evaluate('window.untrustedScriptRan === undefined && window.readPreviewState === undefined'), true);
  } finally {
    clearTimeout(deadline);
    try {
      await browser('close');
    } finally {
      const exited = once(fixture, 'exit');
      fixture.stdin.end();
      if (fixture.exitCode === null) await exited;
      lines.close();
    }
  }
});
