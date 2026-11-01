import type { Metadata, Viewport } from 'next';
import { hasLocale } from 'next-intl';
import { notFound } from 'next/navigation';
import type { ReactNode } from 'react';
import '@editonny/design-system/styles.css';
import { ToastProvider } from '@/features/feedback/ToastProvider';
import { ThemeScript } from '@/features/theme/ThemeScript';
import { SiteShell } from '@/components/shell/SiteShell';
import { ClientMessages, SHELL_MESSAGES } from '@/i18n/ClientMessages';
import { routing, RTL_LOCALES } from '@/i18n/routing';
import { BRAND, SITE_URL } from '@/lib/site';
import { fontVariables } from '../fonts';
import './globals.css';

export const metadata: Metadata = { metadataBase: SITE_URL };

export const viewport: Viewport = { themeColor: BRAND.ink900, colorScheme: 'dark light' };

export function generateStaticParams() {
  return routing.locales.map(locale => ({ locale }));
}

export default async function LocaleLayout({ children, params }: LayoutProps<'/[locale]'>) {
  const { locale } = await params;
  if (!hasLocale(routing.locales, locale)) notFound();
  return (
    <html lang={locale} dir={RTL_LOCALES.includes(locale) ? 'rtl' : 'ltr'} data-theme="dark" className={fontVariables} suppressHydrationWarning>
      <head><ThemeScript /></head>
      <body>
        <ClientMessages groups={SHELL_MESSAGES}>
          <ToastProvider>
            <SiteShell locale={locale}>{children as ReactNode}</SiteShell>
          </ToastProvider>
        </ClientMessages>
      </body>
    </html>
  );
}
