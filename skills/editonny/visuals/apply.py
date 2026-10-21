#!/usr/bin/env python3


import os, sys, json
from pathlib import Path
from resources import copy_assets, copy_scenes
HERE = os.path.dirname(os.path.abspath(__file__))
def project(p):
    p = os.path.abspath(os.path.expanduser(p))
    if not os.path.exists(os.path.join(p, 'project.json')): sys.exit(f'Error: {p} is not an editonny project (project.json is missing)')
    origin = origin_dir()
    text = '\n'.join(f.read_text(encoding='utf-8') for f in (Path(origin) / 'scenes').glob('*.html'))
    b = copy_assets(Path(origin), Path(p), text)
    a = copy_scenes(Path(origin), Path(p))
    print(f'Copied {a} scenes and {b} new assets to {p}')
    print('  Add scene entries to project.json: {"file": "compositions/scenes/visuals/<scene>.html", "start": ..., "end": ..., "hidesCaptions": true}')
def transition(p, name, cuts):
    """Copy one independently registered transition for each cut."""
    p = os.path.abspath(os.path.expanduser(p))
    scene_id = 'transition-' + name
    c = catalog().get(scene_id)
    if not c:
        raise ValueError(f'Unknown transition: {name}')
    content = Path(origin_dir(), 'scenes', scene_id + '.html').read_text()
    copy_assets(Path(origin_dir()), Path(p), content)
    folder = Path(p, 'compositions', 'scenes')
    folder.mkdir(parents=True, exist_ok=True)
    rows, i = [], 1
    for cut in cuts:
        while os.path.lexists(folder / f'transition-{i:02d}-{name}.html'):
            i += 1
        new = f'transition-{i:02d}-{name}'
        with (folder / (new + '.html')).open('x') as stream:
            stream.write(replace_id(content, scene_id, new))
        rows.append({'file': f'compositions/scenes/{new}.html',
                     'start': round(cut - c['duration'] / 2, 3),
                     'end': round(cut + c['duration'] / 2, 3),
                     'name': f'Transition · {name} {i}', 'sceneId': scene_id})
    print(f'Copied {len(rows)} transitions to {folder}. Add these entries to project.json scenes:')
    print(json.dumps(rows, ensure_ascii=False, indent=2))


def pointer(p, name, count, kind='pointer', pre=None):
    """Copy independently registered pointers, effects, or text styles."""
    p = os.path.abspath(os.path.expanduser(p))
    prefix = pre or kind
    scene_id = prefix + '-' + name
    c = catalog().get(scene_id)
    if not c:
        raise ValueError(f'Unknown {kind}: {name}')
    content = Path(origin_dir(), 'scenes', scene_id + '.html').read_text()
    copy_assets(Path(origin_dir()), Path(p), content)
    folder = Path(p, 'compositions', 'scenes')
    folder.mkdir(parents=True, exist_ok=True)
    rows, i = [], 1
    for _ in range(count):
        while os.path.lexists(folder / f'{prefix}-{i:02d}-{name}.html'):
            i += 1
        new = f'{prefix}-{i:02d}-{name}'
        with (folder / (new + '.html')).open('x') as stream:
            stream.write(replace_id(content, scene_id, new))
        row = {'file': f'compositions/scenes/{new}.html', 'start': 0.0,
               'end': c['duration'], 'name': f'{kind.capitalize()} · {name} {i}', 'sceneId': scene_id}
        if c.get('hidesCaptions'):
            row['hidesCaptions'] = True
        rows.append(row)
    print(f'Copied {len(rows)} {kind} scenes to {folder}. Customize their parameters and add these entries to project.json scenes:')
    print(json.dumps(rows, ensure_ascii=False, indent=2))


def origin_dir(): return HERE
def catalog(): return {c['id']: c for c in json.load(open(os.path.join(origin_dir(), 'catalog.json')))['scenes']}
BUILT_ID = r'(["\'])#?([A-Za-z][\w-]*)\1\s*\+'   # "#c3-p" + n: an ID prefix completed at runtime
def made_at_runtime(scr, prefix):
    """The script creates IDs with this prefix itself (e.id = "n-c" + j), so no page element carries it."""
    import re
    return bool(re.search(r'(\.id\s*=\s*|setAttribute\(\s*["\']id["\']\s*,\s*)["\']%s' % re.escape(prefix), scr))
def replace_id(content, old, new):
    """Rename registration and DOM IDs independently for each copy. Ignore SVG IDs embedded in script strings when discovering page IDs; dynamic ID prefixes must be renamed together."""
    import re
    content = content.replace(old, new); suf = '_' + re.sub(r'[^a-z0-9]', '', new)
    outside = re.sub(r'<script\b.*?</script>', '', content, flags=re.S)
    ids = [i for i in set(re.findall(r'(?<![\w-])id="([A-Za-z][\w-]*)"', outside)) if i != 'root' and not i.endswith('-template') and i != new]
    pre = {i.split('-')[0] + '-' for i in ids if '-' in i}
    if len(pre) == 1 and all('-' in i for i in ids) and len(next(iter(pre))) > 4:


        p0 = next(iter(pre)); p1 = p0[:-1] + ('_' if p0[-2].isdigit() else '') + suf[1:] + '-'
        return content.replace(p0, p1)
    # A runtime-built selector ("#c3-p" + n) cannot take a trailing suffix, so its family takes it after the root token.
    scr = ''.join(re.findall(r'<script\b.*?</script>', content, flags=re.S))
    ren = {}
    for p in sorted({m.group(2) for m in re.finditer(BUILT_ID, scr)}, key=len, reverse=True):
        members = [i for i in ids if i.startswith(p) and i not in ren]
        if '-' not in p or not members or made_at_runtime(scr, p): continue
        q = p.replace('-', suf + '-', 1)
        ren.update({i: q + i[len(p):] for i in members})
        content = re.sub(r'(["\'])(#?)%s(?=\1\s*\+)' % re.escape(p), lambda m, q=q: m.group(1) + m.group(2) + q, content)
    for i in sorted(ids, key=len, reverse=True):
        j = ren.get(i, i + suf)
        content = re.sub(r'(?<![\w-])id="%s"' % re.escape(i), 'id="%s"' % j, content)
        content = re.sub(r'#%s(?![\w-])' % re.escape(i), '#%s' % j, content)
        content = re.sub(r'getElementById\((["\'])%s\1\)' % re.escape(i), 'getElementById("%s")' % j, content)
    return content
def check_ids(content, name, suf=''):
    """Return missing literal DOM targets and invalid dynamic prefixes before publishing a copied scene."""
    import re
    outside = re.sub(r'<script\b.*?</script>', '', content, flags=re.S)
    ids = set(re.findall(r'(?<![\w-])id="([^"]+)"', outside))
    scr = ''.join(re.findall(r'<script\b.*?</script>', content, flags=re.S))
    missing = []
    for m in re.finditer(r'getElementById\(\s*(["\'])([\w-]+)\1\s*(\+)?', scr):
        target, dynamic = m.group(2), m.group(3)
        if dynamic:
            cand = [i for i in ids if i.startswith(target)]

            if not cand or (suf and all(i.endswith(suf) for i in cand) and ('"%s"' % suf) not in scr): missing.append(target + '...')
        elif target not in ids and ('"%s"' % target) not in scr.replace(m.group(0), '') and ('id = "%s"' % target) not in scr:
            missing.append(target)
    for m in re.finditer(r'querySelector(?:All)?\(\s*(["\'])#([\w-]+)\1\s*\)', scr):
        if m.group(2) not in ids: missing.append('#' + m.group(2))
    for p in {m.group(2) for m in re.finditer(BUILT_ID, scr)}:
        cand = [i for i in ids if i.startswith(p) and i != p]
        # Only renamed copies of the family remain, so the runtime-built selector reaches nothing.
        if cand and suf and all(suf in i[len(p):] for i in cand) and not made_at_runtime(scr, p) and ('"%s"' % suf) not in scr: missing.append(p + '...')
    return sorted(set(missing))
def shorten(content, id_, old_duration, new_duration):
    """Run an entire fixed-duration animation faster so it fits the shorter scene interval."""
    import re
    content = re.sub(r'data-duration="%s"' % re.escape('%g' % old_duration), 'data-duration="%g"' % new_duration, content)
    target = 'window.__timelines["%s"] = tl;' % id_
    if target not in content: sys.exit('Error: timeline registration not found while shortening scene %s' % id_)
    content = set_timing(content, old_duration, new_duration, False)
    return content.replace(target, 'var tlc = gsap.timeline({ paused: true }); tlc.add(tl.tweenFromTo(0, %s, { duration: %s, ease: "none", immediateRender: false }), 0);\n  window.__timelines["%s"] = tlc;' % (old_duration, new_duration, id_))
def hold_at_end(content, id_, end):
    """Extend internal duration by 0.8 seconds when a scene reaches the final frame, moving its exit beyond the cut. Moving the scene earlier disables the hold."""
    import re
    m = re.search(r'const C = \(window\.__scenes \|\| \{\}\)\["%s"\] \|\| \{[^;]*\};' % re.escape(id_), content)
    if not m: return content
    row = m.group(0).replace('const C', 'var C', 1)
    return content.replace(m.group(0), row + ('\n  /* Keep the exit beyond the final video frame. */'
                                           '\n  if (C && C.duration && typeof C.end === "number" && C.end >= %s - .3) C = Object.assign({}, C, { duration: C.duration + .8 });' % round(end, 3)), 1)
def hold_exit(content, id_, end, exit_at):
    """Drop the fade-outs that start from exit_at (source seconds) when a fixed-duration scene reaches the final frame. These scenes time their exit from a hard-coded D, not C.duration. Moving the scene earlier restores the exit."""
    target = 'window.__timelines["%s"] = tl;' % id_
    if target not in content: sys.exit('Error: timeline registration not found while holding scene %s' % id_)
    return content.replace(target, ('/* Keep the final video frame visible: skip the exit fades. Late entrances still play. */\n'
        '  if (typeof C.end === "number" && C.end >= %s - .3) tl.getChildren(false, true, false).forEach(function (t) { if (t.startTime() >= %s && t.vars.opacity === 0) t.kill(); });\n  ') % (round(end, 3), exit_at) + target, 1)
def set_timing(content, old_duration, new_duration, frame):
    """Expose original and fitted durations and the frame-wait flag to word-synchronized panels."""
    target = 'const tl = gsap.timeline({ paused: true });'
    if target not in content or 'var SCENE_TIMING' in content: return content
    return content.replace(target, target + '\n  var SCENE_TIMING = { originalDuration: %s, duration: %s, frame: %s };   /* Timing supplied by apply.py. */' % (old_duration, new_duration, 'true' if frame else 'false'), 1)
def wait_for_frame(content, id_, old_duration, new_duration):
    """Wait until the frame closes, then fit the complete animation into the remaining scene interval."""
    import re
    target = 'window.__timelines["%s"] = tl;' % id_
    if target not in content: sys.exit('Error: timeline registration not found while fitting scene %s' % id_)
    if abs(new_duration - old_duration) > 1e-6: content = re.sub(r'data-duration="%s"' % re.escape('%g' % old_duration), 'data-duration="%g"' % new_duration, content)
    content = set_timing(content, old_duration, new_duration, True)
    return content.replace(target, ('/* Wait for frame closure and fit the remaining interval. */\n'
        '  var CLOSE_AT = Math.max(0, Math.min(%s * .3, Number((C.frame || {}).closedAt) || 0));\n'
        '  var tlc = gsap.timeline({ paused: true }); tlc.add(tl.tweenFromTo(0, %s, { duration: %s - CLOSE_AT, ease: "none", immediateRender: false }), CLOSE_AT);\n'
        '  window.__timelines["%s"] = tlc;') % (new_duration, old_duration, new_duration, id_))
def read_parameters(content):
    """Read the editable var P parameter block as a dictionary."""
    import re
    m = re.search(r'var P = \{(.*?)\n\s*\};', content, re.S); out = {}
    if not m: return out
    for k, v in re.findall(r'\n\s*(\w+):\s*(.*?),\s*//', m.group(1)):
        try: out[k] = json.loads(v)
        except ValueError: out[k] = v.strip()
    return out
def set_parameter(content, k, v):
    import re
    return re.sub(r'(\n\s*%s:\s*)(.*?),(\s*//)' % re.escape(k), lambda m: m.group(1) + json.dumps(v, ensure_ascii=False) + ',' + m.group(3), content, count=1)
def cut_duration(p, proj):
    edit = os.path.normpath(os.path.join(p, proj.get('edit', '../edit')))
    try: return float(json.load(open(os.path.join(edit, 'cut_timeline.json')))['duration'])
    except (OSError, ValueError, KeyError, TypeError): return None
def frames(proj):
    """Merge adjacent numeric frame blocks so contiguous panels share one frame."""
    bl = sorted([(float(b['start']), float(b['end'])) for b in proj.get('blocks', []) if b.get('scene') == 'frame'
                 and isinstance(b.get('start'), (int, float)) and isinstance(b.get('end'), (int, float))])
    out = []
    for a, z in bl:
        if out and a <= out[-1][1] + 0.05: out[-1][1] = max(out[-1][1], z)
        else: out.append([a, z])
    return out
def _row_text(p, l):
    try: return open(os.path.join(p, l.get('file', '')), encoding='utf-8').read()
    except (OSError, TypeError): return ''
def is_caption(p, l, cat):
    """Identify a caption scene from catalog metadata or its HIDDEN_RANGES declaration."""
    c = cat.get(l.get('sceneId'))
    if c: return c.get('type') == 'captions'
    return 'var HIDDEN_RANGES = ' in _row_text(p, l)
def row_layer(p, l, cat):
    if l.get('aboveFace'): return 3
    c = cat.get(l.get('sceneId'))
    if c: return layer(c)
    if is_caption(p, l, cat): return 1
    import re
    return 0 if re.match(r'(fx|tr)\d\d_', os.path.basename(str(l.get('file', '')))) else 2
def layer(c):
    """Layer order: video effects and transitions below captions, followed by titles and overlays."""
    if not c: return 2
    if c.get('category') in ('Video effects', 'Transitions'): return 0
    if c.get('type') == 'captions': return 1
    return 2
def sort_layers(proj, cat, p=''):
    items = proj.get('scenes', [])
    new = sorted(items, key=lambda l: row_layer(p, l, cat))
    proj['scenes'] = new
    return new != items
def _norm(s):
    import unicodedata, re
    return re.sub(r'[^a-z0-9]', '', unicodedata.normalize('NFD', str(s)).encode('ascii', 'ignore').decode().lower())
def hidden_caption_ranges(p, proj, cat):
    """Calculate caption exclusions from frame blocks, comparisons, green screen settings, explicit exclusions, lettering, and overlay scenes."""
    edit = os.path.normpath(os.path.join(p, proj.get('edit', '../edit')))
    try: ws = [w for w in json.load(open(os.path.join(edit, 'transcripts', 'cut.json')))['words'] if str(w.get('text', '')).strip()]
    except (OSError, ValueError, KeyError, TypeError): ws = []
    try: vdur = float(json.load(open(os.path.join(edit, 'cut_timeline.json')))['duration'])
    except (OSError, ValueError, KeyError, TypeError): vdur = 1e6
    warnings = []
    def time(d, k, after=0.0):
        v = d.get(k)
        if v is None: return None
        if isinstance(v, (int, float)): return float(v)
        try: return float(str(v).replace(',', '.'))
        except ValueError: pass
        target = [_norm(x) for x in str(v).split()]; base = d.get(k + 'After', d.get('after', after))
        try: base = float(base)
        except (TypeError, ValueError): base = after
        for i, w in enumerate(ws):
            if float(w['start']) < base - 1e-6 or i + len(target) > len(ws): continue
            if all(_norm(ws[i + j]['text']).startswith(a) for j, a in enumerate(target)): return float(w['start'])
        warnings.append(str(v)); return None
    lg = proj.get('captions') or {}; out = []
    bl = []
    for b in proj.get('blocks', []):
        start = time(b, 'start') if 'start' in b else 0.0
        if start is None: continue
        end = time(b, 'end', start) if 'end' in b else vdur + 60
        if end is not None: bl.append(dict(b, start=start, end=end))
    bl.sort(key=lambda b: b['start']); t = 0.0
    for b in bl:
        b['start'] = max(b['start'], t); t = max(t, b['end'])
        if b['end'] <= b['start'] + 1e-3: continue
        if 'captions' in b: leaves = not b['captions']
        elif b.get('scene') == 'frame': leaves = not lg.get('inFrame', False)
        elif b.get('scene') == 'greenScreen': leaves = not lg.get('inGreenScreen', True)
        else: leaves = b.get('scene') == 'compare'
        if leaves: out.append((b['start'] - 0.3, b['end']))
        elif b.get('scene') == 'frame' and not any(o.get('scene') == 'frame' and abs(o['end'] - b['start']) <= 0.05 for o in bl):
            out.append((b['start'] - 0.3, b['start'] + 0.7))
    for x in lg.get('hide', []) or []:
        if isinstance(x, (list, tuple)) and len(x) == 2:
            a, z = time({'start': x[0]}, 'start'), time({'end': x[1]}, 'end')
            if a is not None and z is not None: out.append((a, z))
    for i, a in enumerate(proj.get('animations', []) or []):
        if a.get('hidesCaptions', a.get('type') == 'lettering') and not a.get('keepCaptions'):
            at = time(a, 'at') or 0.0; sai_ = time(a, 'exit', at)
            out.append((at - 0.1, sai_ if sai_ is not None else at + 1.5))
    for l in proj.get('scenes', []):
        if not l.get('hidesCaptions') or is_caption(p, l, cat): continue

        if lg.get('inFrame') and (cat.get(l.get('sceneId')) or {}).get('layout') == 'frame': continue
        start = time(l, 'start')
        end = time(l, 'end', start or 0.0) if start is not None else None
        if start is not None and end is not None: out.append((start - 0.1, end))
    out.sort(); together = []
    for a, z in out:
        if together and a <= together[-1][1] + 0.05: together[-1][1] = max(together[-1][1], z)
        else: together.append([a, z])
    return [[round(max(0.0, a), 3), round(z, 3)] for a, z in together], sorted(set(warnings))
def update_captions(p, proj=None, cat=None, speak=True):
    """Write caption exclusion intervals into each copied scene. Re-run after moving blocks or overlays; shorter overlapping captions take priority."""
    import re
    p = os.path.abspath(os.path.expanduser(p)); cat = cat or catalog()
    proj = proj or json.load(open(os.path.join(p, 'project.json')))
    legs = [l for l in proj.get('scenes', []) if is_caption(p, l, cat) or (cat.get(l.get('sceneId')) or {}).get('type') == 'captions']
    if not legs: return
    tracks, warnings = hidden_caption_ranges(p, proj, cat)
    for l in legs:
        f = os.path.join(p, l['file'])
        if not os.path.exists(f): continue
        content = open(f).read()
        if 'var HIDDEN_RANGES = ' not in content:
            print(f'  Warning: {l["file"]} has no HIDDEN_RANGES declaration; reapply the scene before updating caption exclusions'); continue
        try: start = float(l['start']); end = float(l['end'])
        except (TypeError, ValueError, KeyError): start, end = 0.0, 1e6
        own = [x for x in tracks if x[1] > start and x[0] < end]


        for o in legs:
            try: od, oa = float(o['start']), float(o['end'])
            except (TypeError, ValueError, KeyError): continue
            if o is not l and oa - od < end - start - 1e-3 and oa > start and od < end: own.append([round(od, 3), round(oa, 3)])
        own.sort(); jm = []
        for a_, z_ in own:
            if jm and a_ <= jm[-1][1] + 0.05: jm[-1][1] = max(jm[-1][1], z_)
            else: jm.append([a_, z_])
        own = jm
        new = re.sub(r'var HIDDEN_RANGES = \[.*?\];', 'var HIDDEN_RANGES = ' + json.dumps(own) + ';', content, count=1)
        if new != content: open(f, 'w').write(new)
        if speak:
            print(f'  Captions {os.path.basename(f)}: hidden in {len(own)} intervals' + (': ' + ', '.join(f'{a:.2f}-{z:.2f}' for a, z in own[:8]) + (' ...' if len(own) > 8 else '') if own else ''))
    for v in warnings: print(f'  Warning: word "{v}" was not found in the cut transcript; its caption exclusion was skipped')
def use_scene(p, id_, start, end=None, cut=None):
    import re
    p = os.path.abspath(os.path.expanduser(p)); pj = os.path.join(p, 'project.json')
    if not os.path.exists(pj): sys.exit(f'Error: {p} is not an editonny project (project.json is missing)')
    cat = catalog()
    if id_ not in cat: sys.exit(f'Error: scene {id_} is not in catalog.json')
    c = cat[id_]; d = float(c['duration'])
    if cut is not None: start = round(cut - c.get('cutAt', d / 2), 3); end = round(start + d, 3)
    if start < 0: sys.exit(f'Error: {id_} would start at {start:g} s, before the video; use --start or a later --cut')
    if end is None: end = round(start + d, 3)
    dur = round(end - start, 3)
    if dur <= 0: sys.exit('Error: end must be greater than start')
    if c.get('category') in ('Pointers', 'Video effects') and dur < 1.2:
        end = round(start + 1.2, 3); dur = 1.2; print('  Warning: pointers and effects require at least 1.2 seconds; adjusted to %.2f–%.2f' % (start, end))
    folder = os.path.join(p, 'compositions', 'scenes')
    i = 1
    while os.path.lexists(os.path.join(folder, f'{id_}-{i:02d}.html')): i += 1
    new = f'{id_}-{i:02d}'
    content = replace_id(open(os.path.join(origin_dir(), 'scenes', id_ + '.html')).read(), id_, new)
    proj0 = json.load(open(pj)); vdur = cut_duration(p, proj0)
    in_frame = False
    stays = vdur is not None and c.get('category') != 'Transitions' and c.get('type') != 'captions' and end >= vdur - 0.25
    fixed = c['layout'] in ('fullscreen', 'frame') and not c.get('adaptation')
    floor = float(c.get('minDuration', 0.55))
    # Fixed layouts fade out from 0.46 to 0.2 s before their hard-coded D.
    if fixed and stays: content = hold_exit(content, new, vdur, round(d - 0.5, 3))
    if fixed and abs(dur - d) > 0.05:
        if dur < d * floor - 1e-6: sys.exit(f'Error: {id_} lasts {d:g} seconds and can shorten to {d * floor:.1f} seconds (requested: {dur:g}). Choose another scene or a longer interval.')
        if dur > d: sys.exit(f'Error: {id_} lasts at most {d:g} seconds')
        if c['layout'] != 'frame': content = shorten(content, new, d, dur)
    if c['layout'] == 'frame': content = wait_for_frame(content, new, d, dur if abs(dur - d) > 0.05 else d)
    if stays: content = hold_at_end(content, new, vdur)


    if c.get('type') == 'captions' and (proj0.get('captions') or {}).get('inFrame'):
        inside = sum(max(0.0, min(z, end) - max(a_, start)) for a_, z in frames(proj0))
        if inside >= 0.8 * (end - start):
            box = (proj0.get('frame') or {}).get('box') or [520, 980, 400, 540]
            try: size = float(read_parameters(content).get('size', 84) or 84)
            except (TypeError, ValueError): size = 84.0
            size = min(size, 72.0); ym = int(round(float(box[1]) - 22 - size * 0.55))
            content = set_parameter(set_parameter(content, 'size', int(size)), 'y', ym); in_frame = True
            print('  Frame captions: y=%d, font size=%d, above the lower frame' % (ym, size))
    missing = check_ids(content, new, '_' + re.sub(r'[^a-z0-9]', '', new))
    if missing: sys.exit(f'Error: copied scene {new}.html references missing IDs: {", ".join(missing)}')
    nb = copy_assets(Path(origin_dir()), Path(p), content)
    if nb: print(f'  Copied {nb} visual assets to assets/visuals')
    os.makedirs(folder, exist_ok=True)
    open(os.path.join(folder, new + '.html'), 'x').write(content)
    row = {'file': f'compositions/scenes/{new}.html', 'start': round(start, 3), 'end': round(end, 3), 'name': f"{c['name']} {i}", 'sceneId': id_}
    if c.get('hidesCaptions'): row['hidesCaptions'] = True
    if in_frame: row['aboveFace'] = True

    if c.get('category') in ('Pointers', 'Proof cards', 'Interfaces', 'Scene objects', 'Comedy', 'Image entrances', 'Neon') or c.get('category', '').endswith(' demonstrations') or c['layout'] == 'fullscreen': row['overPerson'] = True
    if c.get('category') == 'Comedy':
        if dur < 2.0 or dur > 4.0: print('  Warning: comedy scenes work best at 2–4 seconds (requested: %.2f)' % dur)
    elif c.get('category') in ('Proof cards', 'Interfaces', 'Scene objects', 'Image entrances', 'Neon') and dur < 2.5:
        print('  Warning: %s work best at 2.5 seconds or longer (requested: %.2f)' % (c['category'], dur))
    proj = json.load(open(pj)); proj.setdefault('scenes', []).append(row)
    if sort_layers(proj, cat, p): print('  Layers sorted: effects and transitions, then captions, then overlays')
    json.dump(proj, open(pj, 'w'), ensure_ascii=False, indent=1)
    print(f'Applied {new}.html from {start:.2f} to {end:.2f} seconds in project.json' + (f' (shortened from {d:g} to {dur:g} seconds)' if fixed and abs(dur - d) > 0.05 else ''))
    if stays: print('  Holding through the final frame so the call to action remains visible')
    if c['layout'] == 'frame':
        covers = any(a_ <= start + 0.05 and z >= end - 0.05 for a_, z in frames(proj))
        if not covers: print('  Add a frame block: {"scene": "frame", "start": %.2f, "end": %.2f}. Use one continuous block for adjacent panels.' % (start, end))
    if c.get('parameters'): print('  Customize parameters near the start of compositions/scenes/%s.html' % new)
    if c.get('texts'): print('  Customize text and countNumber values in compositions/scenes/%s.html' % new)
    if row.get('overPerson'): print('  Run camera_motion.py after adding scenes; camera motion pauses during this interval')
    if c['layout'] == 'frame' and not any(b.get('scene') == 'frame' and isinstance(b.get('start'), (int, float)) and b['start'] < start - 0.4 and (not isinstance(b.get('end'), (int, float)) or b['end'] > start) for b in proj.get('blocks', [])):
        print('  This panel waits for the frame to close; its closedAt delay is handled automatically')
    update_captions(p, proj, cat)

def _ai_chat(D, P):
    import unicodedata
    u = (D - .32) - .05; n = len(unicodedata.normalize('NFC', str(P.get('request') or 'Edit this video for me')))
    tb = .35 + max(.5, min(1.2, .22 * u, n * .06))
    return tb + .14 + .12 + .1
def _search(D, P):
    import unicodedata
    u = (D - .32) - .05; n = len(unicodedata.normalize('NFC', str(P.get('term') or 'editonny')))
    return .35 + max(.45, min(1.1, .22 * u, n * .07)) + .14
def _receipt(D, P):
    import re
    k = min(1.0, max(.45, (D - .4) / 3.6))
    if re.search(r'\d', str(P.get('value', ''))): return .05 + .12 + .85 * k
    return .05 + .3
SOUND_TIMING = {'ai-chat-interface': _ai_chat, 'search-interface': _search, 'receipt': _receipt}


TARGET_RMS = {'standard': -20.0, 'comedy': -15.0, 'transition': -24.0}
PEAK_CEILING = {'standard': -6.0, 'comedy': -4.0, 'transition': -8.0}
_MEASUREMENTS = {}
def measure_wav(f):
    if f in _MEASUREMENTS: return _MEASUREMENTS[f]
    import wave, array, math
    r = None
    try:
        w = wave.open(f); ch, sw, sr, n = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
        if sw == 2:
            a = array.array('h', w.readframes(n))
            if sys.byteorder == 'big': a.byteswap()
            if len(a):
                peak = max(abs(min(a)), abs(max(a))) / 32768.0
                cs = [0]; acc = 0
                for v in a: acc += v * v; cs.append(acc)
                window, step = max(1, int(.1 * sr) * ch), max(1, int(.01 * sr) * ch)
                best = max(cs[min(len(a), i + window)] - cs[i] for i in range(0, max(1, len(a) - window + 1), step)) / min(window, len(a)) / 32768.0 ** 2
                if peak > 0 and best > 0: r = (20 * math.log10(peak), 10 * math.log10(best))
        w.close()
    except Exception: r = None
    _MEASUREMENTS[f] = r; return r
def level_for_volume(f, role):
    m = measure_wav(f)
    if not m: return None
    peak, rms = m
    return round(min(PEAK_CEILING[role], TARGET_RMS[role] + (peak - rms)), 1)

def sounds(p):
    """Write events.json with scene sounds aligned to their visible animation and normalized by measured loudness."""
    p = os.path.abspath(os.path.expanduser(p)); proj = json.load(open(os.path.join(p, 'project.json'))); cat = catalog()
    ef = os.path.join(origin_dir(), 'library', 'effects')
    def wav(category, n=1):
        folder = os.path.join(ef, category)
        if not os.path.isdir(folder): return None
        fs = sorted(f for f in os.listdir(folder) if f.endswith('.wav'))
        return os.path.join(folder, fs[(n - 1) % len(fs)]) if fs else None
    ev_path = os.path.join(p, 'events.json')
    old_items = json.load(open(ev_path)) if os.path.exists(ev_path) else []
    if isinstance(old_items, dict): old_items = old_items.get('events', [])
    events = [e for e in old_items if not e.get('sceneId')]; counts = {}


    rows = [l for l in proj.get('scenes', []) if l.get('sceneId') in cat]
    seq = set()
    for a in rows:
        for b in rows:
            if a is not b and a['sceneId'] == b['sceneId'] and cat[a['sceneId']].get('category') == 'Pointers' and abs(float(a['start']) - float(b['start'])) <= 2.0:
                seq.add(id(a))
    for l in proj.get('scenes', []):
        id_ = l.get('sceneId')
        if not id_ or id_ not in cat: continue
        c = cat[id_]; start = float(l['start']); g = c.get('category', '')
        if g == 'Transitions': category, t = c.get('sound'), start + c.get('cutAt', c['duration'] / 2)
        elif g in ('Pointers', 'Video effects'): category, t = c.get('sound'), start + (c.get('soundAt') or 0.1)
        elif g == 'Image entrances':


            s = c.get('soundAt') or 0.1; dur = float(l['end']) - start
            category, t = c.get('sound'), start + (0.05 + (s - 0.05) * min(1.0, 0.55 * dur / 1.3) if id_ == 'entrance-click-open' else s)
        elif id_ in SOUND_TIMING: category, t = c.get('sound'), start + SOUND_TIMING[id_](float(l['end']) - start, read_parameters(_row_text(p, l)))
        elif g in ('Proof cards', 'Interfaces', 'Scene objects', 'Comedy') or c.get('category', '').endswith(' demonstrations'): category, t = c.get('sound'), start + (c.get('soundAt') or 0.1) * min(1.0, (float(l['end']) - start) / c['duration'])
        elif g == 'Captions and titles': category, t = ('title-swoosh', start + 0.1) if c.get('type') == 'title' else (None, 0)
        else: category, t = 'whoosh', start + 0.12
        if id(l) in seq: category = 'bubble-pop'
        if c.get('layout') == 'frame' and any(b.get('scene') == 'frame' and isinstance(b.get('start'), (int, float)) and abs(b['start'] - start) <= 0.4 for b in proj.get('blocks', [])):
            continue
        if not category or category == 'none': continue
        counts[category] = counts.get(category, 0) + 1; w = wav(category, 1 if id(l) in seq else counts[category])
        if not w: print(f'  Warning: no sound found for category {category}'); continue
        ev = {'t': round(t, 3), 'sound': w, 'note': l.get('name', id_), 'sceneId': id_}
        role = 'comedy' if g == 'Comedy' else 'transition' if g == 'Transitions' else 'standard'
        nv = level_for_volume(w, role) if role != 'transition' else None
        if nv is not None: ev['level'] = nv


        if g == 'Comedy': ev['force'] = True; ev.setdefault('level', 'accent')
        elif g == 'Transitions': ev.setdefault('level', 'medium')
        elif id(l) in seq: ev['force'] = True
        events.append(ev)
        output = {'text-spotlight-off': 'photo', 'text-exhale': 'swish'}.get(id_)
        if output and wav(output): events.append(dict({'t': round(float(l['end']) - 0.15, 3), 'sound': wav(output), 'note': 'exit · ' + l.get('name', id_), 'sceneId': id_}, **({'level': level_for_volume(wav(output), 'standard')} if level_for_volume(wav(output), 'standard') is not None else {})))
        if g not in ('Transitions', 'Pointers', 'Video effects', 'Captions and titles', 'Proof cards', 'Interfaces', 'Scene objects', 'Comedy', 'Image entrances', 'Neon') and '"scene-' in open(os.path.join(p, l['file'])).read() and 'countNumber(' in open(os.path.join(p, l['file'])).read():
            dur = float(l['end']) - start; esc = dur / c['duration']; w2 = wav('pop-ui', counts.setdefault('pop-ui', 0) + 1); counts['pop-ui'] += 1
            if w2: events.append(dict({'t': round(start + 1.1 * esc, 3), 'sound': w2, 'note': 'number appears · ' + l.get('name', id_), 'sceneId': id_}, **({'level': level_for_volume(w2, 'standard')} if level_for_volume(w2, 'standard') is not None else {})))
    events.sort(key=lambda e: e.get('t', 0))
    json.dump(events, open(ev_path, 'w'), ensure_ascii=False, indent=1)
    n = sum(1 for e in events if e.get('sceneId'))
    print(f'Wrote {n} scene sound events to {ev_path}')
    update_captions(p, proj, cat)
    print('  Build the sound plan: uv run --directory "<ENGINE>" python helpers/sound_plan.py --project "%s"' % p)

def main(argv=None):
    import argparse
    import math
    import subprocess

    def finite(value):
        try:
            number = float(value)
        except ValueError:
            raise argparse.ArgumentTypeError('expected a number')
        if not math.isfinite(number):
            raise argparse.ArgumentTypeError('expected a finite number')
        return number

    def positive_count(value):
        try:
            number = int(value)
        except ValueError:
            raise argparse.ArgumentTypeError('expected a positive integer')
        if number < 1:
            raise argparse.ArgumentTypeError('expected a positive integer')
        return number

    parser = argparse.ArgumentParser(description='Apply bundled visual scenes to an editonny project.')
    parser.add_argument('--project', type=Path)
    action = parser.add_mutually_exclusive_group()
    for name in ('use', 'transition', 'pointer', 'effect', 'text', 'voice'):
        action.add_argument('--' + name)
    action.add_argument('--sounds', action='store_true')
    action.add_argument('--captions', action='store_true')
    timing = parser.add_mutually_exclusive_group()
    timing.add_argument('--start', type=finite)
    timing.add_argument('--cut', type=finite)
    parser.add_argument('--end', type=finite)
    parser.add_argument('--cuts', help='comma-separated cut times')
    parser.add_argument('--count', type=positive_count, default=1)
    parser.add_argument('--strength', type=finite)
    parser.add_argument('--ramp', type=finite)
    parser.add_argument('voice_index', nargs='?', type=positive_count)
    args = parser.parse_args(argv)
    if not args.project and args.voice != 'list':
        parser.error('--project is required')
    if args.project and not (args.project / 'project.json').is_file():
        parser.error('project.json is missing from --project')
    configuration = json.loads((args.project / 'project.json').read_text()) if args.project else {}
    if args.project and (configuration.get('workflow') or (args.project / configuration.get('edit', '../edit') / 'source_manifest.json').exists()):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'engine/helpers'))
        from project_contract import managed_config
        try:
            managed = managed_config(args.project / configuration.get('edit', '../edit'))
            if managed and args.sounds and not managed['workflow']['audio']['effects']:
                parser.error('Sound effects were not confirmed; propose a settings revision first')
            if managed and not args.sounds and managed['workflow']['visuals']['scenes'] == 'off':
                parser.error('Visual scenes were not confirmed; propose a settings revision first')
            allowed = managed['workflow']['visuals'].get('allowedSceneIds', []) if managed else []
            if allowed and args.use and args.use not in allowed:
                parser.error('This scene is outside the confirmed allowedSceneIds')
        except (ValueError, OSError) as error:
            parser.error(str(error))
    if args.start is not None and args.start < 0:
        parser.error('--start must be nonnegative')
    if args.cut is not None and args.cut < 0:
        parser.error('--cut must be nonnegative')
    if args.end is not None and (args.start is None or args.end <= args.start):
        parser.error('--end requires --start and must be greater than it')
    if args.use and args.start is None and args.cut is None:
        parser.error('--use requires --start or --cut')
    if args.strength is not None and not 0 <= args.strength <= 1:
        parser.error('--strength must be between 0 and 1')
    if args.ramp is not None and not 30 <= args.ramp <= 60:
        parser.error('--ramp must be between 30 and 60 milliseconds')
    if args.voice_index is not None and args.voice != 'remove':
        parser.error('a segment index is only valid with --voice remove')
    cuts = None
    if args.cuts:
        try:
            cuts = [finite(x) for x in args.cuts.split(',')]
        except argparse.ArgumentTypeError as error:
            parser.error(str(error))
        if any(x < 0 for x in cuts):
            parser.error('--cuts must be nonnegative')
    if args.transition and not cuts:
        parser.error('--transition requires --cuts')
    if args.cuts and not args.transition:
        parser.error('--cuts requires --transition')
    try:
        if args.voice:
            command = [sys.executable, str(Path(HERE, 'voice.py'))]
            if args.voice == 'list':
                command.append('--list')
            else:
                command += ['--project', str(args.project)]
                if args.voice in ('clear', 'rebuild'):
                    command.append('--' + args.voice)
                elif args.voice == 'remove':
                    if args.voice_index is None:
                        parser.error('--voice remove requires a segment index')
                    command += ['--remove', str(args.voice_index)]
                elif args.voice != 'inspect':
                    if args.start is None:
                        parser.error('a voice effect requires --start')
                    command += ['--effect', args.voice, '--start', str(args.start)]
                    for name in ('end', 'strength', 'ramp'):
                        value = getattr(args, name)
                        if value is not None:
                            command += ['--' + name, str(value)]
            return subprocess.call(command, env=dict(os.environ, EDITONNY_VIA_SCENE='1'))
        if args.use:
            use_scene(str(args.project), args.use, args.start or 0, args.end, args.cut)
        elif args.transition:
            transition(str(args.project), args.transition, cuts)
        elif args.pointer or args.effect or args.text:
            kind = 'pointer' if args.pointer else 'effect' if args.effect else 'text'
            pointer(str(args.project), getattr(args, kind), args.count, kind)
        elif args.sounds:
            sounds(str(args.project))
        elif args.captions:
            path = args.project / 'project.json'
            configuration = json.loads(path.read_text())
            cat = catalog()
            if sort_layers(configuration, cat, str(args.project)):
                path.write_text(json.dumps(configuration, ensure_ascii=False, indent=2) + '\n')
            update_captions(str(args.project), configuration, cat)
        else:
            project(str(args.project))
        return 0
    except (OSError, ValueError) as error:
        print(f'Error: {error}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
