"""Deliver faithful timed captions without retranscribing or changing the cut."""
from __future__ import annotations

import argparse
import html
import math
import sys
from pathlib import Path

from project_contract import read_json, managed_config


def _clock(seconds: float, comma: bool) -> str:
    milliseconds = round(seconds * 1000)
    hours, milliseconds = divmod(milliseconds, 3600000)
    minutes, milliseconds = divmod(milliseconds, 60000)
    seconds, milliseconds = divmod(milliseconds, 1000)
    return f'{hours:02d}:{minutes:02d}:{seconds:02d}{"," if comma else "."}{milliseconds:03d}'


def export_subtitles(project: Path, format_name: str, output: Path) -> Path:
    if format_name not in ('srt', 'vtt'):
        raise ValueError('Subtitle format must be srt or vtt')
    project, output = Path(project), Path(output)
    config = read_json(project / 'project.json')
    edit = (project / config['edit']).resolve()
    managed = managed_config(edit)
    if managed and format_name not in managed['workflow']['delivery']['subtitleFormats']:
        raise ValueError('This subtitle deliverable was not confirmed')
    if managed and managed['workflow']['language']['captions'] != 'source':
        from project_contract import validate_caption_language
        validate_caption_language(managed, read_json(edit / 'transcripts/cut.json'))
    words = read_json(edit / 'transcripts/cut.json').get('words', [])
    if not words:
        raise ValueError('Measured cut words are required; subtitles cannot be invented')
    from transcript_corrections import correct_words
    words = correct_words(words, config.get('corrections', {}))
    cues, group = [], []
    for word in words:
        start, end = word.get('start'), word.get('end')
        if any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) for x in (start, end)) or not 0 <= start < end:
            raise ValueError('Subtitle words require finite measured times')
        if group and start < group[-1]['start']:
            raise ValueError('Subtitle word order does not match the cut timeline')
        if group and (len(group) >= config.get('captions', {}).get('maxWords', 3) or start - group[-1]['end'] > 0.5):
            cues.append(group); group = []
        group.append(word)
    if group:
        cues.append(group)
    lines = ['WEBVTT', ''] if format_name == 'vtt' else []
    for index, cue in enumerate(cues, 1):
        text = ' '.join(str(w['text']).replace('\r', ' ').replace('\n', ' ') for w in cue)
        lines += [str(index), f'{_clock(cue[0]["start"], format_name == "srt")} --> {_clock(cue[-1]["end"], format_name == "srt")}',
                  html.escape(text, quote=False), '']
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('x', encoding='utf-8') as stream:
        stream.write('\n'.join(lines) + '\n')
    return output


if __name__ == '__main__':
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors=stream.errors)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', type=Path, required=True)
    parser.add_argument('--format', choices=['srt', 'vtt'], required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    export_subtitles(args.project, args.format, args.output)
