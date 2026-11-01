#!/usr/bin/env node
// Writes the README's animated SVG banners for every guide locale; `--check` fails when a committed file is stale.
import { mkdirSync, readdirSync, readFileSync, writeFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';
import { BANNERS } from './banners.mjs';
import { messages, ROOT } from './lib.mjs';

export const LOCALES = ['en-US', 'pt-BR', 'es', 'ar'];
export const OUT = 'docs/assets/readme';

/** Every banner as { path, content }, rendered from the guide's interface text. */
export function render() {
  return LOCALES.flatMap(locale => {
    const text = messages(locale);
    return Object.entries(BANNERS).map(([name, draw]) => ({ path: `${OUT}/${locale}/${name}.svg`, content: draw(text, locale) }));
  });
}

// Runs only as a script; importing render() (tests, `node -e`) leaves argv[1] unrelated or undefined.
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const check = process.argv.includes('--check');
  const stale = [];
  for (const { path, content } of render()) {
    const file = new URL(path, ROOT);
    let current = null;
    try { current = readFileSync(file, 'utf8'); } catch { /* missing counts as stale */ }
    if (current === content) continue;
    if (check) stale.push(path);
    else { mkdirSync(new URL('.', file), { recursive: true }); writeFileSync(file, content); console.log(`wrote ${path}`); }
  }
  if (check) {
    const expected = new Set(render().map(item => item.path));
    for (const locale of LOCALES) {
      for (const name of readdirSync(new URL(`${OUT}/${locale}/`, ROOT))) if (!expected.has(`${OUT}/${locale}/${name}`)) stale.push(`${OUT}/${locale}/${name} (unexpected)`);
    }
    if (stale.length) {
      console.error(`README art is stale; run npm run readme:art\n${stale.join('\n')}`);
      process.exit(1);
    }
    console.log('README art is current.');
  }
}
