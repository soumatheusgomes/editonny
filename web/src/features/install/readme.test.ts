// @vitest-environment node
import { existsSync, readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';
import type { AppLocale } from '@/i18n/routing';
import { REPOSITORY_URL } from '@/lib/links';
import ar from '../../../messages/ar.json';
import en from '../../../messages/en-US.json';
import es from '../../../messages/es.json';
import pt from '../../../messages/pt-BR.json';

const root = new URL('../../../../', import.meta.url);
const READMES: Record<AppLocale, [string, typeof en]> = { 'en-US': ['README.md', en], 'pt-BR': ['README.pt-BR.md', pt], es: ['README.es.md', es], ar: ['README.ar.md', ar] };

describe.each(Object.entries(READMES))('%s README', (locale, [file, messages]) => {
  const readme = readFileSync(new URL(file, root), 'utf8');

  it('carries the guide install request verbatim, pointing at the repository', () => {
    expect(messages.Install.askRequest).toContain(REPOSITORY_URL);
    expect(readme).toContain(`\`\`\`text\n${messages.Install.askRequest}\n\`\`\``);
  });

  it('shows only existing banners of its own locale', () => {
    const images = [...readme.matchAll(/src="(docs\/assets\/readme\/[^"]+)"/g)].map(match => match[1]);
    expect(images.length).toBeGreaterThan(5);
    for (const image of images) {
      expect(image, image).toMatch(new RegExp(`^docs/assets/readme/${locale}/[a-z]+\\.svg$`));
      expect(existsSync(new URL(image, root)), image).toBe(true);
    }
  });
});
