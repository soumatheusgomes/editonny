import { readFileSync } from 'node:fs';
import { hasLocale } from 'next-intl';
import { getTranslations } from 'next-intl/server';
import { notFound } from 'next/navigation';
import { ImageResponse } from 'next/og';
import { routing, RTL_LOCALES, type AppLocale } from '@/i18n/routing';
import { AUTHOR, BRAND } from '@/lib/site';

export const size = { width: 1200, height: 630 };
export const contentType = 'image/png';
export const alt = 'editonny';

export function generateStaticParams() {
  return routing.locales.map(locale => ({ locale }));
}

// Synchronous reads keep the route prerendered (async file access would defer it to request time). Literal URLs keep
// only these files in the deployment trace. Satori reads static TrueType only, hence the instanced subsets.
const asset = (path: string) => readFileSync(new URL(path, import.meta.url));
const fonts = () => [
  { name: 'Figtree', data: asset('../fonts/figtree-800-latin.ttf'), weight: 800 as const, style: 'normal' as const },
  { name: 'Figtree', data: asset('../fonts/figtree-600-latin.ttf'), weight: 600 as const, style: 'normal' as const },
  { name: 'Mono', data: asset('../fonts/jetbrains-mono-700-latin.ttf'), weight: 700 as const, style: 'normal' as const },
  { name: 'Anton', data: asset('../../../../skills/editonny/engine/assets/fonts/anton/Anton-Regular.ttf'), weight: 400 as const, style: 'normal' as const },
];
const photo = () => `data:image/jpeg;base64,${asset('../../../../design-system/assets/imagery/matheus-gomes.jpeg').toString('base64')}`;

// Satori takes numeric lengths in pixels.
const pad = (y: number, x: number) => ({ paddingTop: y, paddingBottom: y, paddingLeft: x, paddingRight: x });
const border = (width: number, color: string) => ({ borderWidth: width, borderStyle: 'solid' as const, borderColor: color });
const pill = (background: string, color: string) => ({ display: 'flex', alignItems: 'center', gap: 8, ...pad(6, 16), borderRadius: 999, background, color });

/**
 * Localized social card, after design-system/assets/brand/og-image.png: wordmark, free/open badge, the hero headline
 * with its first keyword highlighted, the creator, and the workbench phone with the request and timeline beside it.
 */
export default async function OpenGraphImage({ params }: { params: Promise<{ locale: string }> }) {
  const requested = (await params).locale;
  // The proxy skips this route, so unknown prefixes reach it directly.
  if (!hasLocale(routing.locales, requested)) notFound();
  const locale: AppLocale = requested;
  // Satori has no right-to-left shaping and fails on Arabic fallback fonts, so right-to-left locales use the English card.
  const t = await getTranslations({ locale: RTL_LOCALES.includes(locale) ? routing.defaultLocale : locale, namespace: 'Home' });
  const keyword = t('hero.words').split('|')[0];
  const caption = [t('anatomy.captionA'), t('anatomy.captionB'), t('anatomy.captionC')].map(part => part.trim());
  const request = [
    { text: t('bench.req1'), chip: BRAND.orange500 }, { text: t('bench.req2') }, { text: t('bench.req3'), chip: BRAND.sun400 },
    { text: t('bench.req4') }, { text: t('bench.req5'), chip: BRAND.volt400 }, { text: t('bench.req6') },
  ].filter(part => part.text.trim());
  // Punctuation joins the chip before it, as in running text.
  const phrases = request.reduce<{ text: string; chip?: string; tail?: string }[]>((all, part) => {
    const previous = all.at(-1);
    if (!part.chip && previous && /^[,.;:!?]+$/.test(part.text.trim())) previous.tail = part.text.trim();
    else all.push({ ...part, text: part.text.trim() });
    return all;
  }, []);
  return new ImageResponse(
    <div style={{ width: '100%', height: '100%', display: 'flex', position: 'relative', ...pad(64, 72), color: BRAND.fog50, fontFamily: 'Figtree', background: `radial-gradient(circle at 82% 55%, ${BRAND.orange900}, ${BRAND.ink950} 62%)` }}>
      <div style={{ display: 'flex', flexDirection: 'column', justifyContent: 'space-between', width: 620 }}>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 36 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 14, fontSize: 34, fontWeight: 800, letterSpacing: -1 }}>
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', width: 52, height: 52, borderRadius: 14, background: BRAND.orange500, color: BRAND.ink950, fontSize: 36, paddingBottom: 4 }}>e</div>
            <div style={{ display: 'flex', alignItems: 'flex-end' }}>editonny<div style={{ width: 10, height: 10, marginBottom: 9, marginLeft: 3, borderRadius: 999, background: BRAND.volt400 }} /></div>
          </div>
          <div style={{ display: 'flex' }}>
            <div style={{ ...pill(BRAND.volt400, BRAND.ink950), fontFamily: 'Mono', fontSize: 19, letterSpacing: 2, textTransform: 'uppercase' }}>{`${t('hero.metaFree')} · ${t('hero.metaOpen')}`}</div>
          </div>
          <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-start', fontSize: 72, fontWeight: 800, lineHeight: 1.02, letterSpacing: -3 }}>
            <span>{t('hero.line1')}</span>
            <span>{t('hero.line2')}</span>
            <span style={{ marginTop: 10, ...pad(0, 18), paddingBottom: 8, borderRadius: 18, background: BRAND.orange500, color: BRAND.ink950 }}>{keyword}</span>
          </div>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 18 }}>
          {/* eslint-disable-next-line @next/next/no-img-element -- Satori renders plain img elements only. */}
          <img src={photo()} width={64} height={64} alt="" style={{ borderRadius: 999, ...border(3, BRAND.orange500) }} />
          <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
            <span style={{ fontSize: 24, fontWeight: 800 }}>{AUTHOR.name}</span>
            <span style={{ fontSize: 18, fontWeight: 600, color: BRAND.fog400 }}>{`${t('hero.creatorRole')} · github.com/soumatheusgomes/editonny`}</span>
          </div>
        </div>
      </div>

      {/* Phone with the finished caption */}
      <div style={{ position: 'absolute', top: 44, right: 56, width: 320, height: 542, display: 'flex', padding: 12, borderRadius: 46, background: BRAND.ink900, ...border(2, BRAND.ink600) }}>
        <div style={{ position: 'relative', flex: 1, display: 'flex', flexDirection: 'column', justifyContent: 'space-between', padding: 18, overflow: 'hidden', borderRadius: 36, background: `radial-gradient(circle at 50% 38%, ${BRAND.orange500}, ${BRAND.orange700} 45%, ${BRAND.ink950} 92%)` }}>
          <div style={{ position: 'absolute', top: 118, left: 98, width: 100, height: 100, borderRadius: 999, background: BRAND.ink950 }} />
          <div style={{ position: 'absolute', top: 236, left: -34, width: 364, height: 360, borderRadius: 999, background: BRAND.ink950 }} />
          <div style={{ display: 'flex', justifyContent: 'space-between', fontFamily: 'Mono', fontSize: 15 }}>
            <div style={pill(BRAND.volt400, BRAND.ink950)}>
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke={BRAND.ink950} strokeWidth="3.5" strokeLinecap="round" strokeLinejoin="round"><path d="M20 6 9 17l-5-5" /></svg>
              {t('bench.badgeFinal')}
            </div>
            <div style={pill(BRAND.ink800, BRAND.fog50)}>0:31</div>
          </div>
          <div style={{ display: 'flex', flexWrap: 'wrap', justifyContent: 'center', gap: 8, paddingBottom: 26, fontFamily: 'Anton', fontSize: caption.join(' ').length > 22 ? 36 : 42, lineHeight: 1.08, textTransform: 'uppercase' }}>
            {caption[0].split(' ').map((word, i) => <span key={i}>{word}</span>)}
            <span style={{ ...pad(0, 8), borderRadius: 8, background: BRAND.sun400, color: BRAND.ink950 }}>{caption[1]}</span>
            {caption[2].split(' ').map((word, i) => <span key={`c${i}`}>{word}</span>)}
          </div>
        </div>
      </div>

      {/* Request bubble and timeline overlapping the phone */}
      <div style={{ position: 'absolute', top: 196, left: 612, width: 312, display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 6, padding: 18, borderRadius: 20, background: BRAND.ink800, ...border(2, BRAND.ink600), fontSize: 20, fontWeight: 600, lineHeight: 1.3 }}>
        {phrases.map((part, i) => (
          <div key={i} style={{ display: 'flex', alignItems: 'flex-end' }}>
            <span style={part.chip ? { ...pad(2, 8), borderRadius: 7, background: part.chip, color: BRAND.ink950 } : {}}>{part.text}</span>
            {part.tail && <span>{part.tail}</span>}
          </div>
        ))}
      </div>
      <div style={{ position: 'absolute', top: 432, left: 612, width: 218, display: 'flex', flexDirection: 'column', gap: 10 }}>
        <div style={{ display: 'flex', gap: 8 }}>{[64, 46, 92].map((width, i) => <div key={i} style={{ width, height: 18, borderRadius: 6, background: BRAND.ink600 }} />)}</div>
        <div style={{ display: 'flex', gap: 6 }}>{[0, 1, 2, 3, 4, 5, 6, 7].map(i => <div key={i} style={{ width: 21, height: 18, borderRadius: 5, background: BRAND.sun400 }} />)}</div>
        <div style={{ display: 'flex', paddingLeft: 98 }}><div style={{ width: 92, height: 18, borderRadius: 6, background: BRAND.orange500 }} /></div>
        <div style={{ position: 'absolute', top: -14, left: 154, width: 3, height: 102, background: BRAND.volt400 }} />
      </div>
    </div>,
    { ...size, fonts: fonts() },
  );
}
