#!/usr/bin/env python3
"""Measure font glyph widths at the actual variable-font weights used in scenes.

Run with the engine Python environment after changing fonts:
  uv run --directory ENGINE python PROJECT/tools/measure_fonts.py

Writes assets/fonts/widths.json with measurements at 1000 px. Combining accents
and Unicode characters remain glyph data; widths exclude kerning."""
import json
import os
import sys
HERE = os.path.dirname(os.path.abspath(__file__))
FONTS = os.path.join(os.path.dirname(HERE), 'assets', 'fonts')
EM = 1000
MEASURE = {'Montserrat': ('Montserrat.ttf', (700, 800, 900)), 'Caveat': ('Caveat.ttf', (700,))}
EXTRAS = '–—‘’“”…•€✓→×'

def characters():
    cs = [chr(c) for c in range(32, 127)] + [chr(c) for c in range(160, 256)]
    return cs + list(EXTRAS)

def main():
    if any((a in ('-h', '--help') for a in sys.argv[1:])):
        print(__doc__)
        return 0
    try:
        from PIL import ImageFont
    except ImportError:
        print('error: Pillow is missing. Run with the engine Python: uv run --directory <ENGINE> python ...', file=sys.stderr)
        return 2
    exit = {'at': EM, 'fonts': {}}
    for (name, (file, weights)) in MEASURE.items():
        path = os.path.join(FONTS, file)
        if not os.path.exists(path):
            print(f'warning: file not found: {path}', file=sys.stderr)
            continue
        by_weight = {}
        for weight in weights:
            f = ImageFont.truetype(path, EM)
            try:
                axes = f.get_variation_axes()
            except OSError:
                axes = []
            if axes:
                f.set_variation_by_axes([max(axes[0]['minimum'], min(axes[0]['maximum'], weight))])
            by_weight[str(weight)] = {c: round(f.getlength(c)) for c in characters()}
        exit['fonts'][name] = {'file': f'assets/fonts/{file}', 'weights': by_weight}
    destination = os.path.join(FONTS, 'widths.json')
    with open(destination, 'w', encoding='utf-8') as fh:
        json.dump(exit, fh, ensure_ascii=False, separators=(',', ':'))
    print(f'ok: {destination}')
    return 0
if __name__ == '__main__':
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors=stream.errors)
    sys.exit(main())
