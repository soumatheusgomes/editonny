import { defineRouting } from 'next-intl/routing';

export const routing = defineRouting({
  locales: ['en-US', 'pt-BR', 'es', 'ar'],
  defaultLocale: 'en-US',
  // Short, lowercase URL prefixes; the [locale] segment still receives the full locale code.
  localePrefix: { mode: 'always', prefixes: { 'en-US': '/en', 'pt-BR': '/pt-br', es: '/es', ar: '/ar' } },
  localeDetection: true,
  // next-intl defaults to a session cookie; a manual language choice should survive closing the browser.
  localeCookie: { maxAge: 60 * 60 * 24 * 365 },
  pathnames: {
    '/': '/',
    '/create': { 'en-US': '/create', 'pt-BR': '/criar', es: '/crear', ar: '/create' },
    '/catalog': { 'en-US': '/catalog', 'pt-BR': '/catalogo', es: '/catalogo', ar: '/catalog' },
    '/examples': { 'en-US': '/examples', 'pt-BR': '/exemplos', es: '/ejemplos', ar: '/examples' },
    '/install': { 'en-US': '/install', 'pt-BR': '/instalacao', es: '/instalacion', ar: '/install' },
  },
});

export type AppLocale = (typeof routing.locales)[number];
export type AppPathname = keyof typeof routing.pathnames;

/** Locales written right to left; the layout sets <html dir> from this. */
export const RTL_LOCALES: readonly AppLocale[] = ['ar'];
