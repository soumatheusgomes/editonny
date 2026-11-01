import 'server-only';
import ar from '@content/examples/ar.json';
import en from '@content/examples/en-US.json';
import es from '@content/examples/es.json';
import pt from '@content/examples/pt-BR.json';
import type { AppLocale } from '@/i18n/routing';
import type { ExamplePreset } from '../request/draft';
import { EXAMPLES, type ExampleCategory } from './records';

type ExampleText = { title: string; summary: string; when: string; goal: string; tips: string[] };
export type Example = ExamplePreset & ExampleText & { category: ExampleCategory; categoryLabel: string };

const TEXT: Record<AppLocale, Record<string, ExampleText>> = { 'en-US': en, 'pt-BR': pt, es, ar };
const CATEGORY_LABELS: Record<AppLocale, Record<ExampleCategory, string>> = {
  'en-US': { Start: 'Start', Edit: 'Edit', Teach: 'Teach', Compose: 'Compose', Visuals: 'Visuals', Style: 'Style', Refine: 'Refine', Audio: 'Audio', Continue: 'Continue', Mobile: 'Mobile', Review: 'Review' },
  'pt-BR': { Start: 'Começar', Edit: 'Editar', Teach: 'Ensinar', Compose: 'Compor', Visuals: 'Motion', Style: 'Estilo', Refine: 'Ajustar', Audio: 'Som', Continue: 'Repetir e retomar', Mobile: 'Celular', Review: 'Conferir' },
  es: { Start: 'Empezar', Edit: 'Editar', Teach: 'Enseñar', Compose: 'Componer', Visuals: 'Motion', Style: 'Estilo', Refine: 'Ajustar', Audio: 'Sonido', Continue: 'Repetir y retomar', Mobile: 'Celular', Review: 'Revisar' },
  ar: { Start: 'البداية', Edit: 'المونتاج', Teach: 'الشرح', Compose: 'التركيب', Visuals: 'موشن', Style: 'الأسلوب', Refine: 'الضبط', Audio: 'الصوت', Continue: 'التكرار والاستكمال', Mobile: 'الهاتف', Review: 'المراجعة' },
};

/** Ready-made requests with their text in `locale`, falling back to English per record. */
export function getExamples(locale: AppLocale): Example[] {
  return EXAMPLES.map(record => ({ ...record, ...(TEXT[locale][record.id] ?? en[record.id as keyof typeof en]), categoryLabel: CATEGORY_LABELS[locale][record.category] }));
}

/** Example categories present in the library, in workflow order, with localized labels. */
export function getExampleCategories(locale: AppLocale): Array<{ key: ExampleCategory; label: string }> {
  return [...new Set(EXAMPLES.map(record => record.category))].map(key => ({ key, label: CATEGORY_LABELS[locale][key] }));
}

