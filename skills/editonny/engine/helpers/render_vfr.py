"""Cut-only variable-rate route: retain selected picture timestamps without a CFR grid."""
from __future__ import annotations

import json
from fractions import Fraction
import os
from pathlib import Path
import subprocess
import tempfile

from common import content_hash, ensure_distinct_output, probe_video, save_json
from media_policy import encoder_args, frame_tags


def frame_times(path: Path) -> list[float]:
    result = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_frames',
        '-show_entries', 'frame=best_effort_timestamp:stream=time_base', '-of', 'json', str(path)],
        capture_output=True, text=True, check=True, timeout=600, encoding='utf-8', errors='replace')
    data = json.loads(result.stdout)
    time_base = Fraction(data['streams'][0]['time_base'])
    times = [float(int(f['best_effort_timestamp']) * time_base) for f in data['frames']]
    if not times or any(b <= a for a, b in zip(times, times[1:])):
        raise ValueError('VFR preservation requires strictly increasing observed picture timestamps')
    return times


def render_vfr(edl: dict, edit: Path, probes: dict, contract: dict | None, media: dict, output: Path,
               parent_edl: dict | None = None) -> int:
    """This route intentionally has hard joins; picture switches, crops and J-cuts need CFR consent."""
    from render_cut import acquire_lock, ffmpeg, loudnorm_measure, true_peak_db
    if any(r.get('picture_source') or r.get('crop') or r.get('gain_db', 0) for r in edl['ranges']):
        raise ValueError('VFR cut-only does not support alternate pictures, crops or interval gain; confirm a CFR route for those changes')
    requested_jcut = edl.get('jcut')
    if requested_jcut is True or (isinstance(requested_jcut, dict) and requested_jcut.get('enabled')):
        raise ValueError('VFR hard joins cannot apply requested J-cuts; confirm CFR timing or disable J-cuts')
    first = next(iter(probes.values()))['info']
    if contract and (contract['workflow']['video']['width'], contract['workflow']['video']['height']) != (first['display_width'], first['display_height']):
        raise ValueError('VFR cut-only preserves geometry; resizing requires an explicitly supported route')
    if any((p['info']['display_width'], p['info']['display_height']) != (first['display_width'], first['display_height']) for p in probes.values()):
        raise ValueError('VFR sources must share geometry')
    output = Path(output)
    ensure_distinct_output(output, [p['path'] for p in probes.values()])
    clips = edit / 'clips'; clips.mkdir(exist_ok=True)
    lock = acquire_lock(clips)
    try:
        timestamps = {k: frame_times(p['path']) for k, p in probes.items()}
        args, chains, labels, segments, pictures = [], [], [], [], []
        position = 0.0
        has_audio = any(p['info'].get('has_audio') for p in probes.values())
        for index, interval in enumerate(edl['ranges']):
            source = probes[interval['source']]
            times = timestamps[interval['source']]
            selected = [t for t in times if interval['start'] - 1e-7 <= t < interval['end'] - 1e-7]
            if not selected:
                raise ValueError('A VFR cut interval contains no source frames')
            start = selected[0]
            end = next((t for t in times if t > selected[-1] + 1e-7), source['info']['duration'])
            end = min(end, float(interval['end']))
            duration = end - start
            args += ['-i', str(source['path'])]
            time_base = Fraction(source['info']['time_base'])
            start_pts, end_pts = round(start / float(time_base)), round(end / float(time_base))
            chain = f'trim=start_pts={start_pts}:end_pts={end_pts},setpts=PTS-{start_pts},settb=1/1000000'
            if media.get('inputOverride'):
                chain += ',' + frame_tags(media['inputOverride'])
            if media['filters']:
                chain += ',' + ','.join(media['filters'])
            if frame_tags(media):
                chain += ',' + frame_tags(media)
            pictures.append((source['path'], chain, duration, end - selected[-1], len(selected)))
            if has_audio:
                audio = (f'[{index}:a]atrim=start={start:.6f}:end={end:.6f},asetpts=PTS-STARTPTS,aresample=48000,aformat=channel_layouts=stereo'
                         if source['info'].get('has_audio') else 'anullsrc=r=48000:cl=stereo')
                chains.append(audio + f',apad,atrim=duration={duration:.6f}[a{index}]')
            labels.append(f'[a{index}]')
            segments.append({'id': interval['id'], 'source': interval['source'], 'source_path': str(source['path']),
                'src_start': start, 'src_end': end, 'out_start': position, 'out_end': position + duration,
                'sourceFrameTimes': selected, 'outputFrameTimes': [position + t - start for t in selected]})
            position += duration
        if contract:
            from project_contract import read_json, validate_edl_scope
            effective = {**edl, 'ranges': [{**interval, 'start': segment['src_start'], 'end': segment['src_end']}
                for interval, segment in zip(edl['ranges'], segments)]}
            validate_edl_scope(contract, read_json(edit / 'source_manifest.json'), effective, parent_edl=parent_edl)
        if has_audio:
            chains.append(''.join(labels) + f'concat=n={len(labels)}:v=0:a=1[a]')
        with tempfile.TemporaryDirectory(prefix='vfr-', dir=clips) as tmp:
            tmp = Path(tmp)
            script = tmp / 'graph.txt'; script.write_text(';\n'.join(chains), encoding='utf-8')
            video, audio = tmp / 'picture.mp4', tmp / 'voice.wav'
            lines = ['ffconcat version 1.0']
            for index, (source, chain, duration, last_duration, count) in enumerate(pictures):
                clip = tmp / f'clip-{index}.mp4'
                ffmpeg(['ffmpeg', '-y', '-v', 'error', '-i', str(source), '-vf', chain,
                    '-fps_mode', 'passthrough', '-bf', '0',
                    '-enc_time_base', '1/1000000', '-video_track_timescale', '1000000', *encoder_args(media),
                    '-bsf:v', f'setts=duration=if(eq(N\\,{count - 1})\\,{last_duration:.6f}/TB\\,DURATION)', '-an', str(clip)],
                    'encode selected VFR frames without resampling')
                lines += [f"file 'clip-{index}.mp4'", f'duration {duration:.6f}']
            listing = tmp / 'picture.ffconcat'; listing.write_text('\n'.join(lines) + '\n', encoding='utf-8')
            ffmpeg(['ffmpeg', '-v', 'error', '-y', '-f', 'concat', '-safe', '0', '-i', str(listing),
                    '-map', '0:v', '-c:v', 'copy', '-video_track_timescale', '1000000', str(video)], 'join VFR timestamps')
            if has_audio:
                ffmpeg(['ffmpeg', '-y', '-v', 'error', *args, '-filter_complex', script.read_text(encoding='utf-8'),
                        '-map', '[a]', '-c:a', 'pcm_f32le', str(audio)], 'join VFR audio intervals')
            policy = contract['workflow']['audio'] if contract else {'mode': 'normalize', 'targetLufs': -14, 'processingPeak': -1.5, 'truePeakLimit': -1}
            normalize = has_audio and policy['mode'] == 'normalize'
            af = None
            if normalize:
                targets = {'I': policy['targetLufs'], 'TP': policy['processingPeak'], 'LRA': 11}
                measured = loudnorm_measure(audio, targets)
                if not measured:
                    raise ValueError('VFR voice normalization requires a finite measurement; select preserve for silence')
                af = f"loudnorm=I={targets['I']}:TP={targets['TP']}:LRA=11:measured_I={measured['input_i']}:measured_TP={measured['input_tp']}:measured_LRA={measured['input_lra']}:measured_thresh={measured['input_thresh']}:offset={measured['target_offset']}:linear=true"
            candidate = tmp / 'candidate.mp4'
            gain = 0.0
            for attempt in range(4):
                audio_args = ['-i', str(audio), '-map', '1:a', '-c:a', 'aac', '-b:a', '192k'] if has_audio else ['-an']
                filters = ([af] if af else []) + ([f'volume={gain}dB'] if gain else [])
                if filters:
                    audio_args += ['-af', ','.join(filters)]
                ffmpeg(['ffmpeg', '-v', 'error', '-y', '-i', str(video), *audio_args, '-map', '0:v', '-c:v', 'copy',
                        '-t', f'{position:.6f}', '-movflags', '+faststart', str(candidate)], 'mux VFR candidate')
                peak = true_peak_db(candidate) if normalize else None
                if not normalize or (peak is not None and peak <= policy['truePeakLimit']):
                    break
                if peak is None or attempt == 3:
                    raise ValueError('VFR encoded audio did not meet the confirmed peak limit')
                gain += policy['truePeakLimit'] - peak - 0.15
            for source in probes.values():
                if content_hash(source['path']) != source['sha256']:
                    raise ValueError('Source changed during VFR rendering')
            if contract:
                from project_contract import managed_config
                managed_config(edit, 'cut')
            actual = probe_video(candidate)
            os.replace(candidate, output)
        save_json(output.with_name(output.stem + '_timeline.json'), {'duration': position, 'width': first['display_width'],
            'height': first['display_height'], 'fps': actual['fps'], 'fps_str': actual['fps_str'], 'timing': 'vfr',
            'segments': segments, 'mediaPolicy': media, 'contractHash': edl.get('contractHash')})
        print(f'VFR cut-only: {output}; {sum(len(s["sourceFrameTimes"]) for s in segments)} source frames; hard joins')
        return 0
    finally:
        lock.close()
