'use client';
import { LanguageSelect, LANGUAGES } from '@editonny/design-system';
import br from '@editonny/design-system/assets/flags/br.svg';
import eg from '@editonny/design-system/assets/flags/eg.svg';
import es from '@editonny/design-system/assets/flags/es.svg';
import us from '@editonny/design-system/assets/flags/us.svg';
import { useLocale, useTranslations } from 'next-intl';
import { useRouter as useNextRouter } from 'next/navigation';
import { getPathname, usePathname, useRouter } from '@/i18n/navigation';
import { routing, type AppLocale } from '@/i18n/routing';

const FLAGS: Record<string, string> = { us: us.src, br: br.src, es: es.src, eg: eg.src };
const OPTIONS = LANGUAGES.map(language => ({ ...language, flagUrl: FLAGS[language.flag ?? ''] }));

/** Language picker: switches to the same page in another locale, keeps the query and home section, and stores the choice in a cookie. */
export function LanguageMenu() {
  const t = useTranslations('Language');
  const locale = useLocale();
  const pathname = usePathname();
  const router = useRouter();
  const nextRouter = useNextRouter();
  const change = (code: string) => {
    if (code === locale || !routing.locales.includes(code as AppLocale)) return;
    const href = { pathname, query: Object.fromEntries(new URLSearchParams(window.location.search)) };
    const { hash } = window.location;
    if (!hash) return router.replace(href, { locale: code as AppLocale, scroll: false });
    // next-intl drops the fragment of a home section (/en#library). Its prefetch still stores the language cookie;
    // Next's router then navigates with the fragment and scrolls back to the section.
    router.prefetch(href, { locale: code as AppLocale });
    nextRouter.replace(getPathname({ locale: code as AppLocale, href }) + hash);
  };
  return <LanguageSelect value={locale} onChange={change} options={OPTIONS} label={t('label')} searchPlaceholder={t('search')} emptyText={t('empty')} />;
}
