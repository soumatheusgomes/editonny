import type { Metadata } from 'next';
import { getTranslations } from 'next-intl/server';
import { getPathname } from '@/i18n/navigation';
import { routing, type AppLocale, type AppPathname } from '@/i18n/routing';
import { AUTHOR, REPOSITORY_URL } from './links';

export { AUTHOR, REPOSITORY_URL };

const vercelUrl = process.env.VERCEL_PROJECT_PRODUCTION_URL;
/** Canonical origin: NEXT_PUBLIC_SITE_URL, else the Vercel production domain, else local development. */
export const SITE_URL = new URL(process.env.NEXT_PUBLIC_SITE_URL ?? (vercelUrl ? `https://${vercelUrl}` : 'http://localhost:3000'));

/**
 * DESIGN.md colors for surfaces that cannot read CSS variables (web manifest, generated Open Graph images).
 * `site.test.ts` fails when they drift from DESIGN.md.
 */
export const BRAND = {
  ink950: '#050506', ink900: '#0b0b0d', ink800: '#17171b', ink600: '#2c2c33',
  fog50: '#fafafa', fog400: '#a1a1aa', orange500: '#ff4f12', orange700: '#b82f00', orange900: '#2b0b00', volt400: '#d4ff3a', sun400: '#ffd60a',
} as const;

export type PageKey = 'home' | 'create' | 'catalog' | 'examples' | 'install';
export const PAGE_PATHS: Record<PageKey, AppPathname> = { home: '/', create: '/create', catalog: '/catalog', examples: '/examples', install: '/install' };
export const OG_LOCALES: Record<AppLocale, string> = { 'en-US': 'en_US', 'pt-BR': 'pt_BR', es: 'es_ES', ar: 'ar_EG' };

/** Localized path of a page, with the locale prefix (for example `/pt-br/criar`). */
export const localizedPath = (locale: AppLocale, page: PageKey) => getPathname({ locale, href: PAGE_PATHS[page] });

/** Title, description, canonical URL, hreflang alternates, and social cards for one page in one locale. */
export async function pageMetadata(locale: AppLocale, page: PageKey): Promise<Metadata> {
  const t = await getTranslations({ locale, namespace: 'Metadata' });
  const title = t(`${page}.title`);
  const description = t(`${page}.description`);
  const url = localizedPath(locale, page);
  // Child pages define openGraph, which replaces the inherited file-based image, so every page names it explicitly.
  const images = [{ url: `/${locale}/opengraph-image`, width: 1200, height: 630, alt: t('ogAlt') }];
  return {
    metadataBase: SITE_URL,
    title,
    description,
    applicationName: t('siteName'),
    authors: [{ name: AUTHOR.name, url: AUTHOR.instagram }],
    alternates: {
      canonical: url,
      languages: { ...Object.fromEntries(routing.locales.map(code => [code, localizedPath(code, page)])), 'x-default': PAGE_PATHS[page] },
    },
    openGraph: {
      type: 'website', siteName: t('siteName'), title, description, url, images,
      locale: OG_LOCALES[locale], alternateLocale: routing.locales.filter(code => code !== locale).map(code => OG_LOCALES[code]),
    },
    twitter: { card: 'summary_large_image', title, description, images },
    robots: { index: true, follow: true, googleBot: { index: true, follow: true, 'max-image-preview': 'large', 'max-snippet': -1, 'max-video-preview': -1 } },
  };
}

/** schema.org description of the site and the skill for the home page, serialized safely for a `<script>` tag. */
export async function homeJsonLd(locale: AppLocale) {
  const t = await getTranslations({ locale, namespace: 'Metadata' });
  const absolute = (path: string) => new URL(path, SITE_URL).href;
  const url = absolute(localizedPath(locale, 'home'));
  const author = { '@type': 'Person', name: AUTHOR.name, url: AUTHOR.instagram };
  const graph = {
    '@context': 'https://schema.org',
    '@graph': [
      { '@type': 'WebSite', '@id': `${url}#website`, url, name: t('siteName'), description: t('home.description'), inLanguage: locale, publisher: author },
      {
        '@type': 'SoftwareApplication', '@id': `${url}#software`, name: t('siteName'), url, description: t('home.description'), inLanguage: locale,
        image: absolute(`/${locale}/opengraph-image`), applicationCategory: 'MultimediaApplication', applicationSubCategory: 'Video editing',
        operatingSystem: 'macOS', isAccessibleForFree: true, license: 'https://opensource.org/licenses/MIT', downloadUrl: REPOSITORY_URL,
        softwareHelp: absolute(localizedPath(locale, 'install')), sameAs: [REPOSITORY_URL], author,
        offers: { '@type': 'Offer', price: '0', priceCurrency: 'USD' },
      },
    ],
  };
  return JSON.stringify(graph).replace(/</g, '\\u003c');
}
