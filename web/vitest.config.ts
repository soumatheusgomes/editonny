import { fileURLToPath } from 'node:url';
import { defineConfig } from 'vitest/config';

// Vite matches aliases against forward-slash ids, so Windows paths are normalized.
const dir = (path: string) => fileURLToPath(new URL(path, import.meta.url)).replaceAll('\\', '/');

export default defineConfig({
  resolve: {
    alias: {
      '@/': dir('./src/'),
      '@content/': dir('./content/'),
      // `server-only` throws outside React Server Components; unit tests run the same modules in Node.
      'server-only': dir('./src/test/server-only.ts'),
    },
  },
  test: {
    name: 'web',
    environment: 'jsdom',
    include: ['src/**/*.test.{ts,tsx}'],
    setupFiles: ['../vitest.setup.ts'],
    // next-intl imports `next/*` without file extensions, which only Vite's resolver handles.
    server: { deps: { inline: ['next-intl'] } },
  },
});
