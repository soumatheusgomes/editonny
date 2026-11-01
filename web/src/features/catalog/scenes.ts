import 'server-only';
import ar from '@content/scenes/ar.json';
import es from '@content/scenes/es.json';
import pt from '@content/scenes/pt-BR.json';
import type { AppLocale } from '@/i18n/routing';
import catalog from '../../../../skills/editonny/visuals/catalog.json';

export type SceneLayout = 'overlay' | 'fullscreen' | 'frame';
/** Public scene fields; `category` is the English key used for filtering, `categoryLabel` its localized label. */
export type Scene = {
  id: string;
  name: string;
  category: string;
  categoryLabel: string;
  whenToUse: string;
  duration: number | null;
  format: string | null;
  layout: SceneLayout | null;
  hidesCaptions: boolean;
};

type Translation = { categories: Record<string, string>; scenes: Record<string, { name: string; whenToUse: string }> };
const TRANSLATIONS: Partial<Record<AppLocale, Translation>> = { 'pt-BR': pt, es, ar };
const LAYOUTS: readonly string[] = ['overlay', 'fullscreen', 'frame'];

/** The skill's scene catalog with only its public fields, translated for `locale` with an English fallback per field. */
export function getScenes(locale: AppLocale): Scene[] {
  const translation = TRANSLATIONS[locale];
  return catalog.scenes.map(scene => {
    const text = translation?.scenes[scene.id];
    return {
      id: scene.id,
      name: text?.name || scene.name,
      category: scene.category,
      categoryLabel: translation?.categories[scene.category] || scene.category,
      whenToUse: text?.whenToUse || scene.whenToUse,
      duration: typeof scene.duration === 'number' ? scene.duration : null,
      format: scene.format || null,
      layout: LAYOUTS.includes(scene.layout) ? scene.layout as SceneLayout : null,
      hidesCaptions: Boolean(scene.hidesCaptions),
    };
  });
}

/** Scene categories sorted by localized label. */
export function getSceneCategories(locale: AppLocale): Array<{ key: string; label: string }> {
  const seen = new Map(getScenes(locale).map(scene => [scene.category, scene.categoryLabel]));
  return [...seen].map(([key, label]) => ({ key, label })).sort((a, b) => a.label.localeCompare(b.label, locale));
}
