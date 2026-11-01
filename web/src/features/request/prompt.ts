import type { AppLocale } from '@/i18n/routing';
import { DEFAULT_DRAFT, type RequestDraft } from './draft';
import { ENTRIES, type Agent } from './options';
import { phrasesFor } from './phrases';

export type SceneRef = { id: string; name: string };

// Ends the goal with a period unless the visitor already ended the sentence.
const sentence = (text: string) => (/[.!?…]$/.test(text) ? text : `${text}.`);

/** The editing request a visitor copies into their agent, in the interface language. User text is kept verbatim. */
export function createPrompt(draft: Partial<RequestDraft>, locale: AppLocale, scene?: SceneRef | null): string {
  const p = phrasesFor(locale);
  const value = { ...DEFAULT_DRAFT, ...draft };
  const folder = value.folder.trim() || p.folderPlaceholder;
  const goal = value.goal.trim() || p.defaultGoal;
  const reference = scene && value.effects !== 'none' ? `\n${p.sceneReference(`${scene.name} (${scene.id})`)}` : '';
  return `${ENTRIES[value.agent]}

${p.folderLabel}: ${folder}
${p.goalLabel}: ${sentence(goal)}
${p.formatLabel}: ${p.formats[value.format]}.
${p.paceLabel}: ${p.paces[value.pace]}.
${p.captionsLabel}: ${p.captions[value.captions]}.
${p.visualsLabel}: ${p.effects[value.effects]}.${reference}

${p.cleanupLabel}: ${p.cleanup}

${p.closing}`;
}

/** A ready request that edits footage around one catalog scene. */
export function scenePrompt(scene: SceneRef, agent: Agent, locale: AppLocale): string {
  const goal = { 'en-US': 'edit my video using this visual reference', 'pt-BR': 'editar meu vídeo usando esta referência visual', es: 'editar mi video usando esta referencia visual', ar: 'تعديل الفيديو الخاص بي باستخدام هذا المرجع البصري' }[locale];
  return createPrompt({ agent, goal, effects: 'subtle' }, locale, scene);
}
