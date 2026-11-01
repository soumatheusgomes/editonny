// README banners that replay the guide's lab, caption styles, framing, motion scenes, and the install request.
import { C, esc, fill, heading, icon, measure, paragraph, svg, wrap } from './lib.mjs';

const KINDS = ['word', 'pause', 'word', 'word', 'mistake', 'word', 'pause', 'word', 'mistake', 'word', 'word']; // as in LabStages.tsx

/** Footage stand-in from the guide: orange stage light and a dark silhouette, clipped to a rounded screen. */
function footage(id, x, y, w, h, r = 22) {
  const cx = x + w / 2;
  return `<clipPath id="${id}"><rect x="${x}" y="${y}" width="${w}" height="${h}" rx="${r}"/></clipPath>
<g clip-path="url(#${id})"><rect x="${x}" y="${y}" width="${w}" height="${h}" fill="url(#stage)"/><circle cx="${cx}" cy="${y + h * 0.36}" r="${Math.min(w, h) * 0.17}" class="ink"/><ellipse cx="${cx}" cy="${y + h * 0.86}" rx="${Math.min(w * 0.62, h * 0.5)}" ry="${h * 0.3}" class="ink"/></g>`;
}
const STAGE = `<radialGradient id="stage" cx="50%" cy="34%" r="72%"><stop offset="0" stop-color="${C['orange-500']}"/><stop offset=".42" stop-color="${C['orange-700']}"/><stop offset=".95" stop-color="${C['ink-950']}"/></radialGradient>`;

/** Raw × edited: pauses and mistakes are marked, struck, and dropped while the request and result read alongside. */
export function lab(t) {
  const l = t.Home.lab;
  const head = heading({ x: 64, y: 64, index: '03', eyebrow: l.eyebrow, titleA: l.titleA, highlight: l.highlight, size: 56, width: 700, tone: C['volt-400'] });
  const lead = paragraph(l.lead, 860, 112, 356, { size: 16 });
  const top = Math.max(head.bottom, lead.bottom) + 40, x = 64, w = 568;
  const parts = [];
  const size = 27, left = x + 40, width = w - 80;
  let cx = 0, cy = top + 120;
  l.tokens.split('|').forEach((token, i) => {
    const kind = KINDS[i];
    const tw = kind === 'pause' ? token.length * 7.2 + 24 : measure(token, { size, weight: 600 });
    if (cx > 0 && cx + tw > width) { cx = 0; cy += 54; }
    const tx = left + cx;
    if (kind === 'pause') parts.push(`<g class="gone"><rect x="${tx}" y="${cy - 21}" width="${tw}" height="28" rx="8" fill="url(#hatch)"/><text x="${tx + tw / 2}" y="${cy - 2}" text-anchor="middle" class="mono" font-size="11.5" style="letter-spacing:0">${esc(token)}</text></g>`);
    else if (kind === 'mistake') parts.push(`<g class="gone"><text x="${tx}" y="${cy}" font-size="${size}" font-weight="600" style="fill:${C['rec-400']}">${esc(token)}</text><rect x="${tx - 2}" y="${cy - 11}" width="${tw + 4}" height="3" class="strike" style="fill:${C['rec-400']}"/></g>`);
    else parts.push(`<text x="${tx}" y="${cy}" font-size="${size}" font-weight="600">${esc(token)}</text>`);
    cx += tw + 12;
  });
  const wy = cy + 56, h = Math.max(450, wy - top + 120);
  parts.unshift(`<rect x="${x}" y="${top}" width="${w}" height="${h}" rx="30" fill="${C['ink-900']}" stroke="${C['ink-700']}"/>
<text x="${x + 28}" y="${top + 40}" class="mono dim" font-size="11">${esc(l.schematic)}</text>`);
  const bars = Array.from({ length: 62 }, (_, i) => {
    const silent = (i > 13 && i < 22) || (i > 40 && i < 47);
    const bh = silent ? 2 : 6 + Math.round(Math.abs(Math.sin(i * 1.3) * Math.cos(i * 0.4)) * 30);
    return `<rect x="${left + i * 8}" y="${wy - bh / 2}" width="${silent ? 5 : 4}" height="${bh}" rx="2"${silent ? ` class="gone" style="fill:${C['ink-500']}"` : ''}/>`;
  }).join('');
  parts.push(`<g class="or">${bars}</g>`);
  const ty = top + h - 66;
  parts.push(`<rect x="${left}" y="${ty}" width="164" height="42" rx="21" fill="${C['ink-850']}" stroke="${C['ink-700']}"/><rect x="${left + 4}" y="${ty + 4}" width="76" height="34" rx="17" fill="${C['ink-600']}" class="knob" style="--k:80px"/>
<text x="${left + 42}" y="${ty + 26}" text-anchor="middle" font-size="14" font-weight="700">${esc(l.cutRaw)}</text><text x="${left + 122}" y="${ty + 26}" text-anchor="middle" font-size="14" font-weight="700">${esc(l.cutEdited)}</text>
<text x="${x + w - 28}" y="${ty + 26}" text-anchor="end" class="mono dim info0" font-size="11" style="letter-spacing:.04em">${esc(l.cutInfoRaw)}</text><text x="${x + w - 28}" y="${ty + 26}" text-anchor="end" class="mono info1" font-size="11" style="letter-spacing:.04em;fill:${C['volt-400']}">${esc(l.cutInfoEdited)}</text>`);
  const rx = 656, rw = 560;
  parts.push(`<rect x="${rx}" y="${top}" width="${rw}" height="${h}" rx="30" fill="${C['ink-900']}" stroke="${C['ink-700']}"/>`);
  const input = paragraph(l.cut.input, rx + 150, top + 52, 370, { size: 15 });
  const request = paragraph(`“${l.cut.request}”`, rx + 150, input.bottom + 70, 370, { size: 21, weight: 600, lead: 1.4, cls: '' });
  const by = request.bottom + 26, copyW = measure(l.copy, { size: 13, weight: 700 }) + 54;
  const output = paragraph(l.cut.output, rx + 150, by + 100, 370, { size: 15, cls: '' });
  parts.push(`<text x="${rx + 40}" y="${top + 52}" class="mono dim" font-size="11">${esc(l.input)}</text>${input.markup}
<line x1="${rx + 40}" x2="${rx + rw - 40}" y1="${input.bottom + 26}" y2="${input.bottom + 26}" stroke="${C['ink-700']}"/>
<text x="${rx + 40}" y="${input.bottom + 70}" class="mono or" font-size="11">${esc(l.request)}</text>${request.markup}
<rect x="${rx + 150}" y="${by}" width="${copyW}" height="36" rx="18" fill="${C['ink-950']}" stroke="${C['ink-600']}"/>${icon('copy', rx + 164, by + 10, 16, C['fog-50'])}<text x="${rx + 188}" y="${by + 23}" font-size="13" font-weight="700">${esc(l.copy)}</text>
<line x1="${rx + 40}" x2="${rx + rw - 40}" y1="${by + 60}" y2="${by + 60}" stroke="${C['ink-700']}"/>
<g class="info1"><rect x="${rx + 24}" y="${by + 70}" width="${rw - 48}" height="${output.bottom - by - 40}" rx="16" fill="rgba(212,255,58,.07)"/></g>
<text x="${rx + 40}" y="${by + 100}" class="mono vo" font-size="11">${esc(l.output)}</text>${output.markup}
${paragraph(l.footnote, rx + 40, top + h - 30, rw - 80, { size: 11, cls: 'faint' }).markup}`);
  return svg({
    width: 1280, height: top + h + 40, title: `${l.titleA}${l.highlight}`, description: `${l.cut.input} ${l.cut.request} ${l.cut.output}`,
    css: `@keyframes gone{0%,46%{opacity:1}56%,94%{opacity:.12}100%{opacity:1}}.gone{opacity:.12;animation:gone 8s infinite}
@keyframes strike{0%,32%{transform:scaleX(0)}42%,94%{transform:scaleX(1)}100%{transform:scaleX(0)}}.strike{transform-box:fill-box;transform-origin:left;animation:strike 8s infinite}
@keyframes knob{0%,46%{transform:translateX(0)}52%,94%{transform:translateX(var(--k))}100%{transform:translateX(0)}}.knob{transform:translateX(var(--k));animation:knob 8s cubic-bezier(.65,0,.35,1) infinite}
@keyframes info0{0%,46%{opacity:1}50%,96%{opacity:0}100%{opacity:1}}.info0{opacity:0;animation:info0 8s infinite}
@keyframes info1{0%,48%{opacity:0}54%,96%{opacity:1}100%{opacity:0}}.info1{animation:info1 8s infinite}`,
    body: `${head.markup}${lead.markup}${parts.join('\n')}`,
  });
}

/** One sample caption in the four lettering styles, with a karaoke highlight sweeping word by word. */
export function captions(t) {
  const l = t.Home.lab;
  const title = paragraph(l.captions.request, 64, 124, 760, { size: 40, weight: 800, lead: 1.1, cls: '' });
  const sub = paragraph(l.captions.output, 64, title.bottom + 40, 760, { size: 17 });
  const top = sub.bottom + 44, tw = 272, th = 380, gap = (1152 - tw * 4) / 3;
  const words = l.captionSample.split(' ');
  // Three lines balanced by characters, so longer languages do not overflow one line.
  const target = l.captionSample.length / 3;
  const lines = [[]];
  for (const word of words) {
    const line = lines.at(-1);
    if (line.length && lines.length < 3 && [...line, word].join(' ').length > target + 2) lines.push([word]);
    else line.push(word);
  }
  const longest = Math.max(...lines.map(line => line.join(' ').length));
  const step = 0.42, loop = words.length * step + 2.2;
  const styles = [
    // `em` approximates the average advance per character, in ems, for faces without bundled TrueType metrics.
    { label: l.fontImpact, family: 'Anton,Impact,sans-serif', weight: 400, size: 31, upper: true, accent: C['sun-400'], em: 0.48 },
    { label: l.fontClean, family: 'Montserrat,sans-serif', weight: 900, size: 22, upper: true, accent: C['volt-400'], em: 0.72 },
    { label: l.fontHand, family: 'Caveat,cursive', weight: 700, size: 38, upper: false, accent: C['sun-400'], em: 0.42 },
    { label: l.fontMarker, family: "'Permanent Marker',cursive", weight: 400, size: 25, upper: false, accent: C['volt-400'], em: 0.58 },
  ].map(style => ({ ...style, size: Math.min(style.size, Math.floor((tw - 32) / (longest * style.em))) }));
  const tiles = styles.map((style, i) => {
    const x = 64 + i * (tw + gap), cx = x + tw / 2;
    let index = 0;
    const text = lines.map((line, row) => `<text x="${cx}" y="${top + th - 116 + row * style.size * 1.12}" text-anchor="middle" style="font-family:${style.family};font-weight:${style.weight};--a:${style.accent}" font-size="${style.size}">${line.map(word => `<tspan class="say" style="animation-delay:${(index++ * step).toFixed(2)}s">${esc(style.upper ? word.toUpperCase() : word)}</tspan>`).join(' ')}</text>`).join('');
    const lw = measure(style.label, { size: 12, weight: 700 }) + 24;
    return `<rect x="${x - 1}" y="${top - 1}" width="${tw + 2}" height="${th + 2}" rx="27" fill="${C['ink-700']}"/>${footage(`cap${i}`, x, top, tw, th, 26)}
<rect x="${x + 16}" y="${top + 16}" width="${lw}" height="26" rx="13" fill="${C['ink-950']}" opacity=".8"/><text x="${x + 28}" y="${top + 33}" font-size="12" font-weight="700">${esc(style.label)}</text>${text}`;
  }).join('\n');
  return svg({
    width: 1280, height: top + th + 56, fonts: ['figtree', 'anton', 'montserrat', 'caveat', 'marker'],
    title: l.captions.request, description: `${l.captions.output} ${styles.map(style => style.label).join(', ')}.`,
    css: `@keyframes say{0%{fill:${C['fog-50']}}2%,${((step / loop) * 100 * 1.6).toFixed(1)}%{fill:var(--a)}${((step / loop) * 100 * 2).toFixed(1)}%,100%{fill:${C['fog-50']}}}.say{animation:say ${loop.toFixed(2)}s infinite}`,
    body: `<defs>${STAGE}</defs><text x="64" y="76" class="mono dim" font-size="12">${esc(`${l.eyebrow} / ${l.captionsLabel}`.toUpperCase())}</text>${title.markup}${sub.markup}${tiles}`,
  });
}

/** The same footage reframed through the guide's four formats while the subject stays centered. */
export function framing(t) {
  const l = t.Home.lab;
  const frames = [['9:16', 190], ['16:9', 440], ['1:1', 300], ['4:5', 270]].map(([ratio, width]) => {
    const [a, b] = ratio.split(':').map(Number);
    const scale = Math.min(1, 330 / (width * b / a));
    return { ratio, w: width * scale, h: width * scale * b / a };
  });
  const title = paragraph(fill(l.format.request.replace('{format}', '16:9')), 64, 130, 500, { size: 38, weight: 800, lead: 1.12, cls: '' });
  const input = paragraph(l.format.input, 64, title.bottom + 64, 480, { size: 15 });
  const output = paragraph(l.format.output, 64, input.bottom + 64, 480, { size: 15, cls: '' });
  const cx = 900, cy = 236;
  const views = frames.map((frame, i) => {
    const x = cx - frame.w / 2, y = cy - frame.h / 2;
    return `<g class="frame" style="animation-delay:${i * 2}s${i ? ';opacity:0' : ''}">${footage(`f${i}`, x, y, frame.w, frame.h, 18)}
<rect x="${x + 14}" y="${y + 14}" width="${frame.w - 28}" height="${frame.h - 28}" rx="10" fill="none" stroke="${C['fog-50']}" stroke-opacity=".5" stroke-dasharray="5 6"/>
<rect x="${x + 14}" y="${y + frame.h - 44}" width="54" height="28" rx="14" class="vo"/><text x="${x + 41}" y="${y + frame.h - 25}" text-anchor="middle" class="mono ink" font-size="12" style="letter-spacing:0">${frame.ratio}</text></g>`;
  }).join('\n');
  const tabs = frames.map((frame, i) => `<rect x="${cx - 162 + i * 81}" y="420" width="77" height="34" rx="17" fill="${C['ink-800']}"/><g class="tab" style="animation-delay:${i * 2}s${i ? ';opacity:0' : ''}"><rect x="${cx - 162 + i * 81}" y="420" width="77" height="34" rx="17" class="or"/></g>
<text x="${cx - 123.5 + i * 81}" y="442" text-anchor="middle" class="mono" font-size="12" style="letter-spacing:0">${frame.ratio}</text>`).join('');
  return svg({
    width: 1280, height: Math.max(output.bottom + 60, 520), title: l.format.output, description: `${l.format.input} ${frames.map(frame => frame.ratio).join(', ')}.`,
    css: `@keyframes frame{0%{opacity:0;transform:scale(.94)}4%,23%{opacity:1;transform:none}26%,100%{opacity:0;transform:scale(1.03)}}.frame{transform-box:fill-box;transform-origin:center;animation:frame 8s cubic-bezier(.2,.7,.2,1) infinite backwards}
@keyframes tab{0%,1%{opacity:0}3%,24%{opacity:1}26%,100%{opacity:0}}.tab{animation:tab 8s infinite backwards}`,
    body: `<defs>${STAGE}</defs><text x="64" y="76" class="mono dim" font-size="12">${esc(`${l.eyebrow} / ${l.tabFormat}`.toUpperCase())}</text>${title.markup}
<text x="64" y="${title.bottom + 64 - 26}" class="mono dim" font-size="11">${esc(l.input)}</text>${input.markup}
<text x="64" y="${input.bottom + 64 - 26}" class="mono vo" font-size="11">${esc(l.output)}</text>${output.markup}
<rect x="640" y="40" width="576" height="${Math.max(output.bottom + 20, 480) - 40}" rx="28" fill="${C['ink-900']}" stroke="${C['ink-700']}"/>${views}${tabs}`,
  });
}

/** Four catalog scene motifs replaying in phone frames: steps, before/after, number, question and answer. */
export function motion(t) {
  const l = t.Home.lab, f = t.Home.features, bench = t.Home.bench;
  const head = heading({ x: 64, y: 64, index: '04', eyebrow: f.motionTag, titleA: '', highlight: f.motionTitle, size: 56, width: 1150 });
  const lead = paragraph(l.motionOutput, 64, head.bottom + 40, 760, { size: 17 });
  const top = lead.bottom + 44, fw = 262, fh = 440, gap = (1152 - fw * 4) / 3;
  const frame = (i, inner) => {
    const x = 64 + i * (fw + gap);
    return `<rect x="${x}" y="${top}" width="${fw}" height="${fh}" rx="32" fill="${C['ink-900']}" stroke="${C['ink-700']}"/><rect x="${x + 10}" y="${top + 10}" width="${fw - 20}" height="${fh - 20}" rx="24" fill="url(#scene)"/>${inner(x + 10, top + 10, fw - 20, fh - 20)}`;
  };
  const steps = (x, y, w) => `<text x="${x + w / 2}" y="${y + 120}" text-anchor="middle" class="mono" font-size="10">${esc(bench.method)}</text>${[bench.step1, bench.step2, bench.step3].map((step, i) => `<g class="pop" style="animation-delay:${i * 0.3}s"><rect x="${x + 20}" y="${y + 140 + i * 64}" width="${w - 40}" height="52" rx="14" fill="${[C['orange-500'], C['fog-50'], C['volt-400']][i]}"/><text x="${x + 38}" y="${y + 175 + i * 64}" class="k ink" font-size="24">${`0${i + 1}`} ${esc(step.toUpperCase())}</text></g>`).join('')}`;
  const compare = (x, y, w, h) => `<rect x="${x}" y="${y}" width="${w}" height="${h}" rx="24" fill="${C['ink-800']}"/><path d="M${x + w / 2} ${y}H${x + w - 24}A24 24 0 0 1 ${x + w} ${y + 24}V${y + h - 24}A24 24 0 0 1 ${x + w - 24} ${y + h}H${x + w / 2}Z" class="or"/>
<text x="${x + w / 4}" y="${y + 70}" text-anchor="middle" class="k dim" font-size="26">${esc(l.before)}</text><text x="${x + w * 0.75}" y="${y + 70}" text-anchor="middle" class="k ink" font-size="26">${esc(l.after)}</text>
<g class="grow"><rect x="${x + w / 4 - 22}" y="${y + h - 150}" width="44" height="110" rx="10" fill="${C['ink-600']}"/></g><g class="grow" style="animation-delay:.5s"><rect x="${x + w * 0.75 - 22}" y="${y + h - 300}" width="44" height="260" rx="10" class="ink"/></g>
<line x1="${x + w / 2}" x2="${x + w / 2}" y1="${y}" y2="${y + h}" stroke="${C['fog-50']}" stroke-width="3"/><circle cx="${x + w / 2}" cy="${y + h / 2}" r="14" fill="${C['fog-50']}"/>${icon('columns-2', x + w / 2 - 9, y + h / 2 - 9, 18, C['ink-950'])}`;
  const number = (x, y, w, h) => `<g class="pop"><text x="${x + w / 2}" y="${y + 130}" text-anchor="middle" class="k vo" font-size="64">↑ 240%</text></g>${[30, 55, 78, 100].map((pct, i) => `<g class="grow" style="animation-delay:${0.2 + i * 0.18}s"><rect x="${x + 30 + i * ((w - 60) / 4)}" y="${y + h - 30 - pct * 2}" width="${(w - 60) / 4 - 12}" height="${pct * 2}" rx="8" fill="${i === 3 ? C['volt-400'] : C['orange-500']}"/></g>`).join('')}`;
  const question = (x, y, w) => {
    const q = wrap([{ text: l.question }], w - 64, { size: 18, weight: 700 });
    const answer = wrap([{ text: l.answer }], w - 40, { size: 30, family: 'Anton' }).map(line => line.map(word => word.text).join(' '));
    return `<g class="pop"><rect x="${x + 18}" y="${y + 80}" width="${w - 36}" height="${q.length * 26 + 30}" rx="18" fill="${C['fog-50']}"/>${q.map((line, i) => `<text x="${x + 34}" y="${y + 112 + i * 26}" font-size="18" font-weight="700" class="ink">${esc(line.map(word => word.text).join(' '))}</text>`).join('')}</g>
<g class="pop" style="animation-delay:.8s">${answer.map((line, i) => `<text x="${x + 20}" y="${y + 150 + q.length * 26 + 40 + i * 34}" class="k vo" font-size="30">${esc(line)}</text>`).join('')}</g>`;
  };
  const picks = ['steps', 'beforeAfter', 'number', 'question'];
  const labels = picks.map((pick, i) => `<text x="${64 + i * (fw + gap)}" y="${top + fh + 36}" font-size="18" font-weight="800">${esc(l.picks[pick].label)}</text><text x="${64 + i * (fw + gap)}" y="${top + fh + 58}" class="mono faint" font-size="10.5">${esc(`${l.catalogScene.replace(/:$/, '')} 0${i + 1}`.toUpperCase())}</text>`).join('');
  return svg({
    width: 1280, height: top + fh + 130, fonts: ['figtree', 'anton'], title: f.motionTitle, description: `${l.motionOutput} ${picks.map(pick => l.picks[pick].label).join(', ')}.`,
    css: `@keyframes pop{0%{opacity:0;transform:translateY(24px)}8%,82%{opacity:1;transform:none}92%,100%{opacity:0;transform:none}}.pop{animation:pop 6s cubic-bezier(.2,.7,.2,1) infinite both}
@keyframes grow{0%{transform:scaleY(0)}12%,82%{transform:scaleY(1)}92%,100%{transform:scaleY(0)}}.grow{transform-box:fill-box;transform-origin:bottom;animation:grow 6s cubic-bezier(.2,.7,.2,1) infinite both}`,
    body: `<defs><radialGradient id="scene" cx="50%" cy="100%" r="90%"><stop offset="0" stop-color="${C['orange-900']}"/><stop offset=".8" stop-color="${C['ink-950']}"/></radialGradient></defs>
${head.markup}${lead.markup}${frame(0, steps)}${frame(1, compare)}${frame(2, number)}${frame(3, question)}${labels}
<text x="64" y="${top + fh + 104}" font-size="16" font-weight="800" class="or">${esc(fill(f.motionLink))} ↗</text>`,
  });
}

const AGENT = {
  'en-US': ['Cloning github.com/soumatheusgomes/editonny into a temporary folder', 'Reading skills/editonny/SKILL.md and its complete setup procedure', 'Installing for Codex and Claude Code; your settings are preserved', 'Engine, renderer, transcription model, and local OCR ready', 'Diagnostics and synthetic sample passed. Restart your agent.'],
  'pt-BR': ['Clonando github.com/soumatheusgomes/editonny em uma pasta temporária', 'Lendo skills/editonny/SKILL.md e o procedimento de setup completo', 'Instalando para Codex e Claude Code; suas configurações ficam preservadas', 'Engine, renderizador, modelo de transcrição e OCR local prontos', 'Diagnósticos e amostra sintética aprovados. Reinicie o agente.'],
  es: ['Clonando github.com/soumatheusgomes/editonny en una carpeta temporal', 'Leyendo skills/editonny/SKILL.md y su preparación completa', 'Instalando para Codex y Claude Code; tu configuración se conserva', 'Motor, renderizador, modelo de transcripción y OCR local listos', 'Diagnósticos y muestra sintética aprobados. Reinicia el agente.'],
  ar: ['استنساخ github.com/soumatheusgomes/editonny في مجلد مؤقت', 'قراءة skills/editonny/SKILL.md وإجراء الإعداد الكامل', 'التثبيت لـ Codex وClaude Code؛ إعداداتك محفوظة', 'المحرك ومحرك التصيير ونموذج التفريغ وOCR المحلي جاهزة', 'نجح التشخيص والعينة الاصطناعية. أعد تشغيل الوكيل.'],
};

/** Agent terminal receiving the canonical install request, then reporting each setup step. */
export function install(t, locale) {
  const g = t.Home.guide, inst = t.Install;
  const size = 14, char = size * 0.6, x = 64, w = 1152, top = 190;
  const prompt = wrap([{ text: inst.askRequest }], w - 120, { size, family: 'mono' }).map(line => line.map(word => word.text).join(' '));
  const lines = AGENT[locale];
  const py = top + 96, ay = py + prompt.length * 23 + 46;
  const height = ay + lines.length * 40 + 30 - top;
  const typed = prompt.map((line, i) => {
    const y = py + i * 23, lw = line.length * char + 8;
    return `<text x="${x + 64}" y="${y}" font-size="${size}" class="code">${esc(line)}</text><rect x="${x + 60}" y="${y - 16}" width="${lw}" height="22" fill="${C['ink-950']}" class="type" style="--w:${lw.toFixed(1)}px;animation-delay:${(i * 0.32).toFixed(2)}s"/>`;
  }).join('');
  const start = prompt.length * 0.32 + 0.6;
  const report = lines.map((line, i) => {
    const y = ay + i * 40, delay = (start + i * 0.8).toFixed(2);
    return `<g class="say" style="animation-delay:${delay}s"><circle cx="${x + 72}" cy="${y - 5}" r="10" class="vo"/><path d="M${x + 67} ${y - 5}l3.5 3.5 6.5-7" fill="none" stroke="${C['ink-950']}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
<text x="${x + 94}" y="${y}" font-size="15.5" font-weight="500">${esc(line)}</text></g>`;
  }).join('\n');
  const loop = 14;
  return svg({
    width: 1280, height: top + height + 80, title: inst.askTitle, description: `${g.askLead} ${inst.askRequest}`,
    css: `.code{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;fill:${C['fog-200']}}
@keyframes type{0%{transform:translateX(0)}${((0.3 / loop) * 100).toFixed(1)}%,96%{transform:translateX(var(--w))}100%{transform:translateX(0)}}.type{transform:translateX(var(--w));animation:type ${loop}s steps(24) infinite backwards}
@keyframes say{0%{opacity:0;transform:translateY(8px)}3%,96%{opacity:1;transform:none}100%{opacity:0}}.say{animation:say ${loop}s cubic-bezier(.2,.7,.2,1) infinite backwards}
@keyframes blink{50%{opacity:0}}.caret{animation:blink 1s steps(1) infinite}`,
    body: `<text x="${x}" y="76" class="mono or" font-size="12">${esc(g.askKicker)}</text>
<text x="${x}" y="134" font-size="44" font-weight="800" letter-spacing="-0.04em">${esc(inst.askTitle)}</text>
<clipPath id="term"><rect x="${x}" y="${top}" width="${w}" height="${height}" rx="24"/></clipPath>
<rect x="${x}" y="${top}" width="${w}" height="${height}" rx="24" fill="${C['ink-950']}" stroke="${C['ink-700']}"/>
<g clip-path="url(#term)"><rect x="${x}" y="${top}" width="${w}" height="52" fill="${C['ink-900']}"/>
<circle cx="${x + 26}" cy="${top + 26}" r="6" fill="${C['rec-400']}"/><circle cx="${x + 46}" cy="${top + 26}" r="6" fill="${C['sun-400']}"/><circle cx="${x + 66}" cy="${top + 26}" r="6" fill="${C['volt-600']}"/>
<text x="${x + w / 2}" y="${top + 31}" text-anchor="middle" class="mono dim" font-size="11">CLAUDE CODE  ·  CODEX</text>
<text x="${x + 40}" y="${py}" class="mono or" font-size="16" style="letter-spacing:0">›</text>${typed}
<line x1="${x + 40}" x2="${x + w - 40}" y1="${ay - 34}" y2="${ay - 34}" stroke="${C['ink-800']}"/>${report}
<rect x="${x + 64}" y="${ay + lines.length * 40 - 22}" width="10" height="18" class="vo caret"/></g>
${icon('check', x, top + height + 30, 18, C['volt-400'])}<text x="${x + 28}" y="${top + height + 44}" font-size="15" class="dim">${esc(g.installNote)}</text>
<text x="${x + w}" y="${top + height + 44}" text-anchor="end" class="mono faint" font-size="11">${esc(t.Home.bench.badge.toUpperCase())}</text>`,
  });
}
