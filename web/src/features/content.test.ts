// @vitest-environment node
import { describe, expect, it } from 'vitest';
import { routing } from '@/i18n/routing';
import arExamples from '@content/examples/ar.json';
import enExamples from '@content/examples/en-US.json';
import esExamples from '@content/examples/es.json';
import ptExamples from '@content/examples/pt-BR.json';
import arScenes from '@content/scenes/ar.json';
import esScenes from '@content/scenes/es.json';
import ptScenes from '@content/scenes/pt-BR.json';
import source from '../../../skills/editonny/visuals/catalog.json';
import { getSceneCategories, getScenes } from './catalog/scenes';
import { getExamples } from './examples/examples';
import { EXAMPLE_CATEGORIES, EXAMPLES } from './examples/records';

const LOCALES = routing.locales;

describe('scene catalog', () => {
  it.each(LOCALES)('%s returns the 145 scenes in source order with public fields only', locale => {
    const scenes = getScenes(locale);
    expect(scenes.map(scene => scene.id)).toEqual(source.scenes.map(scene => scene.id));
    expect(scenes).toHaveLength(145);
    for (const scene of scenes) expect(Object.keys(scene).sort()).toEqual(['category', 'categoryLabel', 'duration', 'format', 'hidesCaptions', 'id', 'layout', 'name', 'whenToUse']);
  });

  it.each([['pt-BR', ptScenes], ['es', esScenes], ['ar', arScenes]] as const)('%s translates every scene and category, with no unknown ids', (_, translation) => {
    const ids = source.scenes.map(scene => scene.id);
    expect(Object.keys(translation.scenes).sort()).toEqual([...ids].sort());
    for (const [id, text] of Object.entries(translation.scenes)) {
      expect(text.name.trim(), id).not.toBe('');
      expect(text.whenToUse.trim(), id).not.toBe('');
    }
    expect(Object.keys(translation.categories).sort()).toEqual([...new Set(source.scenes.map(scene => scene.category))].sort());
  });

  it('uses the translated text and keeps English keys for filtering', () => {
    const [scene] = getScenes('pt-BR');
    expect(scene).toMatchObject({ id: 'revenue-counter', name: 'Dinheiro subindo', category: 'General', categoryLabel: 'Universais' });
    expect(getScenes('en-US')[0]).toMatchObject({ name: 'Revenue counter', categoryLabel: 'General' });
  });

  it('sorts categories by their localized label', () => {
    const labels = getSceneCategories('es').map(category => category.label);
    expect(labels).toEqual([...labels].sort((a, b) => a.localeCompare(b, 'es')));
    expect(getSceneCategories('es')).toHaveLength(26);
  });
});

describe('examples', () => {
  it.each(LOCALES)('%s returns the 54 examples in workflow order with complete text', locale => {
    const examples = getExamples(locale);
    expect(examples.map(example => example.id)).toEqual(EXAMPLES.map(example => example.id));
    expect(examples).toHaveLength(54);
    for (const example of examples) {
      for (const field of ['title', 'summary', 'when', 'goal', 'categoryLabel'] as const) expect(example[field].trim(), `${example.id}.${field}`).not.toBe('');
      expect(example.tips.length, example.id).toBeGreaterThan(0);
      expect(EXAMPLE_CATEGORIES).toContain(example.category);
    }
  });

  // Checks the files themselves: getExamples falls back to English, which would hide a missing translation.
  it.each([['en-US', enExamples], ['pt-BR', ptExamples], ['es', esExamples], ['ar', arExamples]] as const)('%s text file covers every example, with no unknown ids', (_, file) => {
    const text: Record<string, { title: string; summary: string; when: string; goal: string; tips: string[] }> = file;
    expect(Object.keys(text).sort()).toEqual(EXAMPLES.map(example => example.id).sort());
    for (const [id, entry] of Object.entries(text)) {
      for (const field of ['title', 'summary', 'when', 'goal'] as const) expect(entry[field]?.trim(), `${id}.${field}`).toBeTruthy();
      expect(entry.tips?.length, id).toBeGreaterThan(0);
    }
  });

  it('keeps the ids the guide links to', () => {
    for (const id of ['vsl', 'lesson', 'reels', 'polish', 'hook-versions', 'interview', 'explain', 'introduce', 'demonstrate']) expect(EXAMPLES.some(example => example.id === id), id).toBe(true);
  });

  it('carries preset format and effects keys', () => {
    expect(getExamples('es').find(example => example.id === 'ad')).toMatchObject({ format: 'vertical', effects: 'expressive' });
  });
});
