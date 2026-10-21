import type { RequestPhrases } from './types';

export const enUS: RequestPhrases = {
  folderLabel: 'Edit the videos in',
  folderPlaceholder: '[enter the video folder]',
  goalLabel: 'Goal',
  defaultGoal: 'turn the footage into a clear, engaging video',
  formatLabel: 'Format',
  paceLabel: 'Pace',
  captionsLabel: 'Captions',
  visualsLabel: 'Visuals',
  sceneReference: reference => `Catalog reference: ${reference}. Adapt the text to the actual footage.`,
  cleanupLabel: 'Standard editing cleanup',
  cleanup: 'Remove unnecessary pauses, audible breaths, mistakes, false starts, and redundant repetitions. Preserve complete words, natural pacing, intentional pauses, and the meaning of the speech. Measure cuts against the actual recording and review transitions so speech is not clipped. For targeted revisions, apply cleanup only within the requested editing scope; preserve unrelated content and approved timing.',
  closing: 'Before editing, inspect only the selected files locally, present a complete summary of requested and proposed settings, and wait for my confirmation even when this prompt is detailed. Include image and filters, audio treatment and music, captions, conversation/speech/caption languages, cuts and protected pauses, format/resolution/FPS/framing, graphics, sample review, and delivery. Do not add music, sound effects, or background ambience unless I explicitly request them. Visual scenes, transitions, and dynamic pacing do not authorize additional audio. Preserve audio already present in the source within the requested scope. Preserve the original image appearance: no unrequested brightness, tone, contrast, saturation, filter, or automatic HDR/SDR conversion. Explain necessary conversions before proceeding. Keep the recorded language unless I request translation. Save each project in an organized Downloads/editonny folder with numbered exports; if running remotely or access is unavailable, explain and confirm an accessible destination. First check the environment and files. Preserve the originals, spoken words, and meaning. Do not invent prices, results, or testimonials. Prepare an 8–15 second sample for me to review before finishing the full video. Do not use paid services or upload media to third parties without my authorization. When finished, report the output file and the checks performed.',
  formats: { source: 'preserve source proportions, resolution and frame rate', vertical: '9:16 (vertical)', horizontal: '16:9 (horizontal)', square: '1:1 (square)', portrait: '4:5 (portrait feed)' },
  paces: { natural: 'natural, preserving intentional pauses', dynamic: 'dynamic, with precise cuts that preserve complete words', calm: 'calm, with room for expression and comprehension' },
  captions: { readable: 'readable, with subtle emphasis on important words', subtle: 'subtle, without animation', none: 'no captions' },
  effects: { subtle: 'subtle, only when they help explain the speech', educational: 'educational, with steps, pointers, and titles', expressive: 'expressive, with balanced transitions and emphasis', none: 'no visual scenes or additional effects' },
};
