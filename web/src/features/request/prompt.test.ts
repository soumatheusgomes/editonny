import { describe, expect, it } from 'vitest';
import { createPrompt, scenePrompt } from './prompt';
import { DEFAULT_DRAFT } from './draft';

// Complete generated requests for default, explicit, and catalog-scene inputs.
const GOLDEN_EMPTY = "$editonny\n\nEdit the videos in: [enter the video folder]\nGoal: turn the footage into a clear, engaging video.\nFormat: preserve source proportions, resolution and frame rate.\nPace: natural, preserving intentional pauses.\nCaptions: readable, with subtle emphasis on important words.\nVisuals: no visual scenes or additional effects.\n\nStandard editing cleanup: Remove unnecessary pauses, audible breaths, mistakes, false starts, and redundant repetitions. Preserve complete words, natural pacing, intentional pauses, and the meaning of the speech. Measure cuts against the actual recording and review transitions so speech is not clipped. For targeted revisions, apply cleanup only within the requested editing scope; preserve unrelated content and approved timing.\n\nBefore editing, inspect only the selected files locally, present a complete summary of requested and proposed settings, and wait for my confirmation even when this prompt is detailed. Include image and filters, audio treatment and music, captions, conversation/speech/caption languages, cuts and protected pauses, format/resolution/FPS/framing, graphics, sample review, and delivery. Do not add music, sound effects, or background ambience unless I explicitly request them. Visual scenes, transitions, and dynamic pacing do not authorize additional audio. Preserve audio already present in the source within the requested scope. Preserve the original image appearance: no unrequested brightness, tone, contrast, saturation, filter, or automatic HDR/SDR conversion. Explain necessary conversions before proceeding. Keep the recorded language unless I request translation. Save each project in an organized Downloads/editonny folder with numbered exports; if running remotely or access is unavailable, explain and confirm an accessible destination. First check the environment and files. Preserve the originals, spoken words, and meaning. Do not invent prices, results, or testimonials. Prepare an 8–15 second sample for me to review before finishing the full video. Do not use paid services or upload media to third parties without my authorization. When finished, report the output file and the checks performed.";
const GOLDEN_FULL = "/editonny\n\nEdit the videos in: /Users/Ana/Raw videos\nGoal: teach photography.\nFormat: 16:9 (horizontal).\nPace: dynamic, with precise cuts that preserve complete words.\nCaptions: no captions.\nVisuals: educational, with steps, pointers, and titles.\nCatalog reference: Revenue growth (revenue-growth). Adapt the text to the actual footage.\n\nStandard editing cleanup: Remove unnecessary pauses, audible breaths, mistakes, false starts, and redundant repetitions. Preserve complete words, natural pacing, intentional pauses, and the meaning of the speech. Measure cuts against the actual recording and review transitions so speech is not clipped. For targeted revisions, apply cleanup only within the requested editing scope; preserve unrelated content and approved timing.\n\nBefore editing, inspect only the selected files locally, present a complete summary of requested and proposed settings, and wait for my confirmation even when this prompt is detailed. Include image and filters, audio treatment and music, captions, conversation/speech/caption languages, cuts and protected pauses, format/resolution/FPS/framing, graphics, sample review, and delivery. Do not add music, sound effects, or background ambience unless I explicitly request them. Visual scenes, transitions, and dynamic pacing do not authorize additional audio. Preserve audio already present in the source within the requested scope. Preserve the original image appearance: no unrequested brightness, tone, contrast, saturation, filter, or automatic HDR/SDR conversion. Explain necessary conversions before proceeding. Keep the recorded language unless I request translation. Save each project in an organized Downloads/editonny folder with numbered exports; if running remotely or access is unavailable, explain and confirm an accessible destination. First check the environment and files. Preserve the originals, spoken words, and meaning. Do not invent prices, results, or testimonials. Prepare an 8–15 second sample for me to review before finishing the full video. Do not use paid services or upload media to third parties without my authorization. When finished, report the output file and the checks performed.";
const GOLDEN_SCENE = "$editonny\n\nEdit the videos in: [enter the video folder]\nGoal: edit my video using this visual reference.\nFormat: preserve source proportions, resolution and frame rate.\nPace: natural, preserving intentional pauses.\nCaptions: readable, with subtle emphasis on important words.\nVisuals: subtle, only when they help explain the speech.\nCatalog reference: Revenue counter (revenue-counter). Adapt the text to the actual footage.\n\nStandard editing cleanup: Remove unnecessary pauses, audible breaths, mistakes, false starts, and redundant repetitions. Preserve complete words, natural pacing, intentional pauses, and the meaning of the speech. Measure cuts against the actual recording and review transitions so speech is not clipped. For targeted revisions, apply cleanup only within the requested editing scope; preserve unrelated content and approved timing.\n\nBefore editing, inspect only the selected files locally, present a complete summary of requested and proposed settings, and wait for my confirmation even when this prompt is detailed. Include image and filters, audio treatment and music, captions, conversation/speech/caption languages, cuts and protected pauses, format/resolution/FPS/framing, graphics, sample review, and delivery. Do not add music, sound effects, or background ambience unless I explicitly request them. Visual scenes, transitions, and dynamic pacing do not authorize additional audio. Preserve audio already present in the source within the requested scope. Preserve the original image appearance: no unrequested brightness, tone, contrast, saturation, filter, or automatic HDR/SDR conversion. Explain necessary conversions before proceeding. Keep the recorded language unless I request translation. Save each project in an organized Downloads/editonny folder with numbered exports; if running remotely or access is unavailable, explain and confirm an accessible destination. First check the environment and files. Preserve the originals, spoken words, and meaning. Do not invent prices, results, or testimonials. Prepare an 8–15 second sample for me to review before finishing the full video. Do not use paid services or upload media to third parties without my authorization. When finished, report the output file and the checks performed.";
const CLEANUP = 'Remove unnecessary pauses, audible breaths, mistakes, false starts, and redundant repetitions.';
const FULL = { agent: 'claude', folder: '/Users/Ana/Raw videos', goal: 'teach photography', format: 'horizontal', pace: 'dynamic', captions: 'none', effects: 'educational' } as const;
const SCENE = { id: 'revenue-growth', name: 'Revenue growth' };

describe('confirmed image-preserving intake', () => {
  it.each([
    ['en-US', 'Do not add music, sound effects, or background ambience unless I explicitly request them.'],
    ['pt-BR', 'Não adicione música, efeitos sonoros ou ambiência de fundo, salvo se eu pedir explicitamente.'],
    ['es', 'No agregues música, efectos de sonido ni ambiente de fondo salvo que lo pida explícitamente.'],
  ] as const)('%s requires explicit permission for additional audio in default and visual requests', (locale, policy) => {
    expect(createPrompt({}, locale)).toContain(policy);
    expect(createPrompt(FULL, locale, SCENE)).toContain(policy);
    expect(scenePrompt(SCENE, 'codex', locale)).toContain(policy);
    const goal = 'Add the music I provided';
    expect(createPrompt({ goal }, locale)).toContain(goal);
    expect(createPrompt({ goal }, locale)).toContain(policy);
  });

  it('preserves source format and does not enable decorative effects by default', () => {
    expect(DEFAULT_DRAFT.format).toBe('source');
    expect(DEFAULT_DRAFT.effects).toBe('none');
  });

  it.each([
    ['en-US', 'wait for my confirmation', 'Preserve the original image appearance'],
    ['pt-BR', 'aguarde minha confirmação', 'Preserve a aparência original'],
    ['es', 'espera mi confirmación', 'Conserva la apariencia original'],
  ] as const)('%s confirms every job and forbids hidden color treatment', (locale, confirm, image) => {
    const request = createPrompt(FULL, locale, SCENE);
    expect(request).toContain(confirm);
    expect(request).toContain(image);
  });
});

describe('createPrompt in English', () => {
  it('keeps the request structure with preservation defaults and mandatory intake', () => {
    expect(createPrompt({}, 'en-US')).toBe(GOLDEN_EMPTY);
  });

  it('keeps explicit choices and adds mandatory intake for a complete draft', () => {
    expect(createPrompt(FULL, 'en-US', SCENE)).toBe(GOLDEN_FULL);
  });

  it('keeps the selected scene while preserving unspecified source format', () => {
    expect(scenePrompt({ id: 'revenue-counter', name: 'Revenue counter' }, 'codex', 'en-US')).toBe(GOLDEN_SCENE);
  });

  it('does not double the final period of a goal that already ends a sentence', () => {
    expect(createPrompt({ goal: 'Teach photography.' }, 'en-US')).toContain('Goal: Teach photography.\n');
    expect(createPrompt({ goal: 'Can you tighten the intro?' }, 'en-US')).toContain('Goal: Can you tighten the intro?\n');
  });
});

describe('createPrompt in every locale', () => {
  it.each(['en-US', 'pt-BR', 'es'] as const)('%s starts with the agent entry and keeps user data verbatim', locale => {
    const goal = 'Explique a preparação do café — '.repeat(60).trim();
    const prompt = createPrompt({ agent: 'claude', folder: '  /Videos/Málaga/東京  ', goal }, locale);
    expect(prompt.startsWith('/editonny\n')).toBe(true);
    expect(prompt).toContain('/Videos/Málaga/東京');
    expect(prompt).toContain(goal);
    expect(createPrompt({}, locale).startsWith('$editonny\n')).toBe(true);
  });

  it.each(['en-US', 'pt-BR', 'es'] as const)('%s states the cleanup policy exactly once', locale => {
    const prompt = createPrompt(FULL, locale, SCENE);
    const cleanup = createPrompt({}, locale).split('\n\n')[2]!;
    expect(prompt.split(cleanup).length - 1).toBe(1);
  });

  it('keeps the canonical English cleanup sentence', () => {
    expect(createPrompt({}, 'en-US')).toContain(CLEANUP);
  });

  it.each(['en-US', 'pt-BR', 'es'] as const)('%s drops the scene reference when visuals are off', locale => {
    const withScene = createPrompt({ effects: 'subtle' }, locale, SCENE);
    const withoutVisuals = createPrompt({ effects: 'none' }, locale, SCENE);
    expect(withScene).toContain('revenue-growth');
    expect(withoutVisuals).not.toContain('revenue-growth');
  });

  it('localizes the template outside the user data', () => {
    expect(createPrompt({}, 'pt-BR')).not.toContain('Edit the videos in');
    expect(createPrompt({}, 'es')).not.toContain('Edit the videos in');
    expect(createPrompt({ format: 'portrait' }, 'pt-BR')).toContain('4:5');
  });
});
