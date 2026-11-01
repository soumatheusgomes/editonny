// Registry of README banners (name -> (messages, locale) => SVG) and the section banners built from guide text.
import { hero } from './hero.mjs';
import { C, COUNTS, esc, fill, fit, heading, icon, measure, paragraph, svg } from './lib.mjs';
import { captions, framing, install, lab, motion } from './scenes.mjs';

const STAR = 'M0-11C2-2 2-2 11 0C2 2 2 2 0 11C-2 2-2 2-11 0C-2-2-2-2 0-11Z';

/** Two crossing tapes scrolling in opposite directions, like the guide's marquee. */
function marquee(t) {
  const tape = (items, y, angle, fillColor, tone, speed, reverse) => {
    const size = 30;
    const seq = items.map(item => ({ item, w: measure(item.toUpperCase(), { size, spacing: -0.5 }) }));
    const seqWidth = seq.reduce((sum, { w }) => sum + w + 64, 0);
    const copies = Math.ceil(2600 / seqWidth) + 2;
    // One copy starts left of the card so the rotated tape never shows an empty end.
    let x = -seqWidth;
    const parts = [];
    for (let c = 0; c < copies; c++) for (const { item, w } of seq) {
      parts.push(`<text x="${x}" y="${y + 11}" font-size="${size}" font-weight="800" letter-spacing="-0.5" style="fill:${tone}">${esc(item.toUpperCase())}</text><path d="${STAR}" transform="translate(${x + w + 32} ${y})" fill="${tone}"/>`);
      x += w + 64;
    }
    return `<g transform="rotate(${angle} 640 ${y})"><rect x="-80" y="${y - 34}" width="1440" height="68" fill="${fillColor}"/>
<g class="${reverse ? 'back' : 'tape'}" style="--w:-${seqWidth.toFixed(1)}px;animation-duration:${speed}s">${parts.join('')}</g></g>`;
  };
  const items = fill(t.Home.marquee).split('|');
  const meta = [t.Home.hero.metaFree, t.Home.hero.metaOpen, t.Home.hero.metaAgents, ...t.Home.hero.words.split('|')];
  return svg({
    width: 1280, height: 190, title: items.join(' · '), description: meta.join(' · '),
    css: '@keyframes tape{from{transform:translateX(0)}to{transform:translateX(var(--w))}}.tape{animation:tape linear infinite}@keyframes back{from{transform:translateX(var(--w))}to{transform:translateX(0)}}.back{animation:back linear infinite}',
    body: `${tape(meta, 80, 3, C['volt-400'], C['ink-950'], 46, true)}${tape(items, 104, -2.5, C['orange-500'], C['ink-950'], 38, false)}`,
  });
}

/** Exploded isometric stack of the four layers of an edit beside their explanations. */
function anatomy(t) {
  const a = t.Home.anatomy;
  const head = heading({ x: 680, y: 70, index: '01', eyebrow: a.eyebrow, titleA: a.titleA, highlight: a.highlight, size: 56, width: 540 });
  const lead = paragraph(a.lead, 680, head.bottom + 40, 520, { size: 17 });
  const rows = [1, 2, 3, 4].map((n, i) => {
    const y = lead.bottom + 44 + i * 92;
    return `<line x1="680" x2="1216" y1="${y}" y2="${y}" stroke="${C['ink-700']}"/>
<text x="680" y="${y + 52}" class="k num" font-size="40" style="animation-delay:${i * 2}s">${`0${n}`}</text>
<text x="744" y="${y + 34}" font-size="19" font-weight="700">${esc(a[`plate${n}Title`])}</text>
${paragraph(a[`plate${n}Text`], 744, y + 60, 470, { size: 14 }).markup}`;
  }).join('\n');
  const iso = 'matrix(0.866 0.5 -0.866 0.5 0 0)';
  const plate = (n, inner, fillColor, stroke) => `<g class="plate" style="--lift:${-n * 104}px;--low:${-n * 18}px"><g transform="translate(330 520) ${iso} scale(.9)">
<g class="glow" style="animation-delay:${n * 2}s"><rect x="-190" y="-130" width="380" height="260" rx="22" fill="none" stroke="${C['orange-500']}" stroke-width="4"/></g>
<rect x="-180" y="-120" width="360" height="240" rx="18" fill="${fillColor}" stroke="${stroke}" stroke-width="1.5"${n > 1 ? ' stroke-dasharray="6 6"' : ''}/>${inner}</g></g>`;
  const wave = Array.from({ length: 40 }, (_, i) => { const h = 6 + Math.round(Math.abs(Math.sin(i * 1.7) * Math.cos(i * 0.6)) * 26); return `<rect x="${-150 + i * 7.5}" y="${80 - h / 2}" width="4" height="${h}" rx="2"/>`; }).join('');
  const raw = `${[[-150, 70], [-70, 54], [0, 90], [104, 46]].map(([x, w]) => `<rect x="${x}" y="-70" width="${w}" height="58" rx="8" fill="${C['ink-600']}"/>`).join('')}
<rect x="-78" y="-70" width="6" height="58" fill="url(#hatch)"/><rect x="92" y="-70" width="10" height="58" fill="url(#hatch)"/><g class="or">${wave}</g>
<text x="-160" y="-92" class="mono dim" font-size="13">${esc(a.tagRaw)}</text>`;
  const cut = `${[-78, -14, 94].map(x => `<line x1="${x}" x2="${x}" y1="-100" y2="110" stroke="${C['volt-400']}" stroke-width="3"/>`).join('')}
<rect x="40" y="58" width="96" height="30" rx="15" class="vo"/><text x="54" y="79" class="mono ink" font-size="13" style="letter-spacing:.06em">✂ ${esc(a.tagCut.split('·').pop().trim())}</text>
<text x="-160" y="-92" class="mono" font-size="13" style="fill:${C['volt-400']}">${esc(a.tagCut)}</text>`;
  const capWidth = measure(`${a.captionA}${a.captionB}${a.captionC}`, { family: 'Anton', size: 30 });
  const bWidth = measure(a.captionB, { family: 'Anton', size: 30 });
  const aWidth = measure(a.captionA, { family: 'Anton', size: 30 });
  const caption = `<rect x="${-capWidth / 2 - 18}" y="-28" width="${capWidth + 36}" height="56" rx="10" class="ink"/>
<rect x="${-capWidth / 2 + aWidth - 4}" y="-20" width="${bWidth + 8}" height="40" rx="6" fill="${C['sun-400']}"/>
<text x="${-capWidth / 2}" y="12" class="k" font-size="30">${esc(a.captionA)}<tspan class="ink">${esc(a.captionB)}</tspan>${esc(a.captionC)}</text>
<text x="-160" y="-92" class="mono" font-size="13" style="fill:${C['volt-400']}">${esc(a.tagCaption)}</text>`;
  const steps = [t.Home.bench.step1, t.Home.bench.step2, t.Home.bench.step3];
  const tags = steps.map((step, i) => `<rect x="${-120 + i * 26}" y="${-74 + i * 48}" width="230" height="40" rx="20" fill="${[C['orange-500'], C['fog-50'], C['volt-400']][i]}"/><text x="${-100 + i * 26}" y="${-46 + i * 48}" class="k ink" font-size="22">${`0${i + 1}`} ${esc(step.toUpperCase())}</text>`).join('');
  const body = `${plate(0, raw, C['ink-800'], C['ink-600'])}${plate(1, cut, 'rgba(23,23,27,.78)', C['volt-400'])}${plate(2, caption, 'rgba(212,255,58,.06)', C['volt-400'])}${plate(3, `<g>${tags}</g>`, 'rgba(255,79,18,.05)', C['orange-500'])}
${head.markup}${lead.markup}${rows}`;
  return svg({
    width: 1280, height: 860, fonts: ['figtree', 'anton'], title: `${a.titleA}${a.highlight}`, description: a.lead,
    css: `@keyframes stack{0%,12%{transform:translateY(var(--low))}38%,74%{transform:translateY(var(--lift))}96%,100%{transform:translateY(var(--low))}}.plate{transform:translateY(var(--lift));animation:stack 8s cubic-bezier(.65,0,.35,1) infinite}
@keyframes glow{0%,2%{opacity:0}6%,22%{opacity:1}26%,100%{opacity:0}}.glow{opacity:0;animation:glow 8s infinite}
@keyframes num{0%,2%{fill:${C['fog-600']}}6%,22%{fill:${C['orange-500']}}26%,100%{fill:${C['fog-600']}}}.num{fill:${C['fog-600']};animation:num 8s infinite}`,
    body,
  });
}

/** The two services as the guide's paired cards: Editor (dark) and Motion (orange), with Lucide icons. */
function features(t) {
  const f = t.Home.features;
  const head = heading({ x: 64, y: 64, index: '02', eyebrow: f.eyebrow, titleA: f.titleA, highlight: f.highlight, size: 56, width: 760 });
  const lead = paragraph(f.lead, 860, 108, 356, { size: 16 });
  const top = Math.max(head.bottom, lead.bottom) + 40;
  const card = (x, tag, title, link, mark, items, dark, offset) => {
    const tone = dark ? C['orange-500'] : C['ink-950'];
    const rows = items.map(([name, item], i) => {
      const y = top + 140 + i * 72;
      return `<g class="row" style="animation-delay:${(offset + i) * 0.12}s"><line x1="${x + 40}" x2="${x + 532}" y1="${y}" y2="${y}" stroke="${dark ? C['ink-700'] : 'rgba(5,5,6,.18)'}"/>
<g class="scan" style="animation-delay:${(offset + i) * 1}s"><rect x="${x + 28}" y="${y + 6}" width="516" height="60" rx="14" fill="${dark ? 'rgba(255,79,18,.12)' : 'rgba(5,5,6,.12)'}"/></g>
<rect x="${x + 40}" y="${y + 14}" width="44" height="44" rx="12" fill="${dark ? C['ink-700'] : C['ink-950']}"/>${icon(name, x + 51, y + 25, 22, C['orange-500'])}
<text x="${x + 102}" y="${y + 33}" font-size="${fit([fill(item.title)], 430, { weight: 700 }, 16.5)}" font-weight="700"${dark ? '' : ' class="ink"'}>${esc(fill(item.title))}</text>
<text x="${x + 102}" y="${y + 54}" font-size="${fit([item.text], 430, { weight: 500 }, 13.5)}" font-weight="500"${dark ? ' class="dim"' : ' class="ink" opacity=".72"'}>${esc(item.text)}</text></g>`;
    }).join('\n');
    return `<rect x="${x}" y="${top}" width="572" height="580" rx="30" fill="${dark ? C['ink-850'] : C['orange-500']}" stroke="${dark ? C['ink-700'] : C['orange-600']}"/>
<text x="${x + 532}" y="${top + 86}" text-anchor="end" class="k" font-size="66" fill="none" stroke="${dark ? C['ink-600'] : C['orange-700']}" stroke-width="1.5" style="fill:none">${mark}</text>
<text x="${x + 40}" y="${top + 58}" class="mono" font-size="12" style="fill:${tone}">${esc(tag)}</text>
<text x="${x + 40}" y="${top + 104}" font-size="32" font-weight="800" letter-spacing="-0.04em"${dark ? '' : ' class="ink"'}>${esc(title)}</text>${rows}
<text x="${x + 40}" y="${top + 540}" font-size="15" font-weight="800" style="fill:${tone}">${esc(fill(link))} ↗</text>`;
  };
  const editor = [['cuts', 'scissors'], ['story', 'clapperboard'], ['captions', 'type'], ['audio', 'volume-2'], ['reframe', 'crop']].map(([key, name]) => [name, f.editor[key]]);
  const motionItems = [['lists', 'list-ordered'], ['comparisons', 'columns-2'], ['numbers', 'trending-up'], ['diagrams', 'git-fork'], ['catalog', 'sparkles']].map(([key, name]) => [name, f.motion[key]]);
  return svg({
    width: 1280, height: top + 640, fonts: ['figtree', 'anton'], title: `${f.titleA}${f.highlight}`, description: f.lead,
    css: `@keyframes row{from{opacity:0;transform:translateX(-18px)}to{opacity:1;transform:none}}.row{animation:row .6s cubic-bezier(.2,.7,.2,1) both}
@keyframes scan{0%,1%{opacity:0}3%,9%{opacity:1}11%,100%{opacity:0}}.scan{opacity:0;animation:scan 10s infinite}`,
    body: `${head.markup}${lead.markup}${card(64, f.editorTag, f.editorTitle, f.editorLink, 'ED', editor, true, 0)}${card(644, f.motionTag, f.motionTitle, f.motionLink, 'MO', motionItems, false, 5)}`,
  });
}

/** How it works: three numbered steps joined by a filling progress line. */
function workflow(t) {
  const h = t.Home.how;
  const head = heading({ x: 64, y: 64, index: '05', eyebrow: h.eyebrow, titleA: h.titleA, highlight: h.highlight, size: 52, width: 1150 });
  const top = head.bottom + 56;
  const icons = ['copy', 'terminal', 'wand-sparkles'];
  const steps = [1, 2, 3].map((n, i) => {
    const x = 64 + i * 392;
    return `<g class="step" style="animation-delay:${i * 2}s"><text x="${x}" y="${top + 92}" class="k" font-size="84" fill="none" stroke="${C['orange-500']}" stroke-width="2" style="fill:none">${`0${n}`}</text></g>
<g class="lit" style="animation-delay:${i * 2}s"><text x="${x}" y="${top + 92}" class="k or" font-size="84">${`0${n}`}</text></g>
<rect x="${x + 270}" y="${top + 28}" width="56" height="56" rx="16" fill="${C['ink-800']}" stroke="${C['ink-700']}"/>${icon(icons[i], x + 284, top + 42, 28, C['orange-500'])}
<text x="${x}" y="${top + 150}" font-size="24" font-weight="800" letter-spacing="-0.03em">${esc(h[`step${n}Title`])}</text>
${paragraph(h[`step${n}Text`], x, top + 182, 340, { size: 15 }).markup}`;
  }).join('\n');
  return svg({
    width: 1280, height: top + 300, fonts: ['figtree', 'anton'], title: `${h.titleA}${h.highlight}`, description: h.lead,
    css: `@keyframes fillbar{0%{transform:scaleX(0)}70%,92%{transform:scaleX(1)}100%{transform:scaleX(1);opacity:0}}.bar{transform-box:fill-box;transform-origin:left;animation:fillbar 6s cubic-bezier(.65,0,.35,1) infinite}
@keyframes lit{0%,4%{opacity:0}8%,92%{opacity:1}100%{opacity:0}}.lit{animation:lit 6s infinite both}`,
    body: `${head.markup}
<rect x="64" y="${top}" width="1152" height="4" rx="2" fill="${C['ink-700']}"/><rect x="64" y="${top}" width="1152" height="4" rx="2" class="vo bar"/>
${steps}`,
  });
}

const STATS = {
  'en-US': 'Bundled videos', 'pt-BR': 'Vídeos incluídos', es: 'Videos incluidos', ar: 'فيديوهات مرفقة',
};

/** Orange closing card with the library counters rolling into place. */
function stats(t, locale) {
  const o = t.Home.offer;
  const counter = (value, x, y, label, i) => {
    const digits = String(value).split('');
    const size = 120, step = size * 1.05;
    let cx = x;
    const cols = digits.map((digit, d) => {
      const w = measure(digit, { family: 'Anton', size });
      const col = `<g class="roll" style="--to:${-Number(digit) * step}px;animation-delay:${i * 0.25 + d * 0.12}s">${Array.from({ length: 10 }, (_, n) => `<text x="${cx}" y="${y + n * step}" class="k ink" font-size="${size}">${n}</text>`).join('')}</g>`;
      cx += w + 2;
      return col;
    }).join('');
    return `<clipPath id="c${i}"><rect x="${x - 4}" y="${y - size * 0.92}" width="${cx - x + 8}" height="${size * 1.02}"/></clipPath><g clip-path="url(#c${i})">${cols}</g>
<text x="${x}" y="${y + 40}" class="mono ink" font-size="13">${esc(label.toUpperCase())}</text>`;
  };
  const meta = [t.Home.hero.metaFree, t.Home.hero.metaOpen, t.Home.hero.metaAgents].join('  ·  ');
  const body = `<rect x="24" y="24" width="1232" height="372" rx="28" class="or"/>
<text x="72" y="106" font-size="54" font-weight="800" letter-spacing="-0.05em" class="ink">${esc(o.title)}</text>
<text x="72" y="140" class="mono ink" font-size="12" opacity=".75">${esc(meta.toUpperCase())}</text>
${counter(COUNTS.scenes, 72, 300, t.Footer.scenes, 0)}${counter(COUNTS.requests, 480, 300, t.Footer.requests, 1)}${counter(COUNTS.videos, 860, 300, STATS[locale], 2)}`;
  return svg({
    width: 1280, height: 420, fonts: ['figtree', 'anton'], title: o.title,
    description: `${COUNTS.scenes} ${t.Footer.scenes} · ${COUNTS.requests} ${t.Footer.requests} · ${COUNTS.videos} ${STATS[locale]}. ${o.creatorNote}`,
    css: '@keyframes roll{0%{transform:translateY(0)}30%,100%{transform:translateY(var(--to))}}.roll{transform:translateY(var(--to));animation:roll 7s cubic-bezier(.2,.8,.2,1) infinite both}',
    body,
  });
}

export const BANNERS = {
  hero: text => hero(text.Home, text.Nav),
  marquee,
  install,
  workflow,
  anatomy,
  features,
  lab,
  captions,
  framing,
  motion,
  stats,
};
