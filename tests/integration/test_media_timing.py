from fractions import Fraction
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

HELPERS = Path(__file__).resolve().parents[2] / 'skills/editonny/engine/helpers'
sys.path[:0] = [str(HELPERS), str(Path(__file__).resolve().parents[1])]
from common import output_fps_for, probe_video


class MediaTimingTests(unittest.TestCase):
    def test_vfr_snapping_cannot_remove_protection_or_unrelated_parent_content(self):
        from intake import inspect_sources, propose_settings
        from workspace import create_workspace, revise_workspace, record_confirmation
        from project_contract import load_contract, settings_hash
        from workflow_fixtures import confirmation
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); source = root / 'sparse.mp4'
            subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=gray:s=128x64:r=30:d=1',
                '-vf', r'select=eq(n\,0)+eq(n\,15)+eq(n\,24),setparams=range=tv:color_primaries=bt709:color_trc=bt709:colorspace=bt709',
                '-fps_mode', 'vfr', '-bf', '0', '-c:v', 'libx264', str(source)], check=True)
            manifest = inspect_sources([source], False)
            source_id = manifest['sources'][0]['id']
            for scope in ('full', 'intervals'):
                with self.subTest(scope=scope):
                    c = propose_settings(manifest, {'captions': False, 'delivery': {'workspaceRoot': str(root / 'downloads')}})['config']
                    if scope == 'full':
                        c['workflow']['editing']['protectedIntervals'] = [{'sourceId': source_id, 'start': .1, 'end': .2}]
                    project = create_workspace(root / 'downloads', scope, manifest, c, confirmation(c, manifest))
                    edl = {'sources': {'s': str(source)}, 'ranges': [{'id': 'a', 'source': 's', 'start': .1, 'end': .8}]}
                    (project.parent / 'edit/edl.json').write_text(json.dumps(edl), encoding='utf-8')
                    if scope == 'intervals':
                        project = revise_workspace(project, {'workflow': {'editing': {'intervals': [
                            {'sourceId': source_id, 'start': .6, 'end': .7}]}}}, 'intervals')
                        changed = load_contract(project)
                        accepted = confirmation(changed, manifest); accepted['parentSettingsHash'] = settings_hash(c)
                        record_confirmation(project, accepted)
                        c = changed
                    edl['contractHash'] = settings_hash(c)
                    path = project.parent / 'edit/edl.json'; path.write_text(json.dumps(edl), encoding='utf-8')
                    result = subprocess.run([sys.executable, str(HELPERS / 'render_cut.py'), str(path)], capture_output=True, text=True, encoding='utf-8', errors='replace')
                    self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertRegex(result.stderr, 'protected|scope')
                    self.assertFalse((path.parent / 'cut.mp4').exists())

    def test_equal_nominal_and_average_rates_do_not_hide_irregular_timestamps(self):
        from intake import inspect_sources, propose_settings
        from render_vfr import frame_times
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); edit = root / 'edit'; edit.mkdir(); source = root / 'irregular.mp4'
            subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=gray:s=128x64:r=24:d=2',
                '-vf', 'settb=1/24000,setpts=PTS+if(eq(N\\,10)\\,20\\,0),setparams=range=tv:color_primaries=bt709:color_trc=bt709:colorspace=bt709',
                '-fps_mode', 'passthrough', '-enc_time_base', '1/24000', '-bf', '0', '-c:v', 'libx264', str(source)], check=True)
            observed = inspect_sources([source], False)
            facts = observed['sources'][0]['facts']
            self.assertEqual(facts['fps_str'], facts['nominal_fps_str'])
            self.assertEqual(propose_settings(observed, {})['config']['workflow']['video']['timing'], 'vfr')
            edl = edit / 'edl.json'
            edl.write_text(json.dumps({'sources': {'s': str(source)}, 'ranges': [{'id': 'a', 'source': 's', 'start': 0, 'end': 2}]}), encoding='utf-8')
            result = subprocess.run([sys.executable, str(HELPERS / 'render_cut.py'), str(edl), '--no-jcut'], capture_output=True, text=True, encoding='utf-8', errors='replace')
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue(all(abs(a - b) <= 1e-6 for a, b in zip(frame_times(source), frame_times(edit / 'cut.mp4'))))

    def test_vfr_cut_keeps_irregular_picture_timestamps(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); edit = root / 'edit'; edit.mkdir()
            source = root / 'vfr.mp4'
            subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=s=160x96:r=30:d=1',
                            '-vf', "select='eq(mod(n,3),0)+eq(mod(n,7),1)'", '-fps_mode', 'vfr',
                            '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(source)], check=True)
            edl = edit / 'edl.json'
            edl.write_text(json.dumps({'jcut': False, 'sources': {'s': str(source)}, 'ranges': [
                {'id': 'a', 'source': 's', 'start': 0, 'end': 0.4},
                {'id': 'b', 'source': 's', 'start': 0.5, 'end': 0.9}]}), encoding='utf-8')
            result = subprocess.run([sys.executable, str(HELPERS / 'render_cut.py'), str(edl), '--no-jcut'], capture_output=True, text=True, encoding='utf-8', errors='replace')
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            def timestamps(path):
                data = json.loads(subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_frames',
                    '-show_entries', 'frame=best_effort_timestamp_time', '-of', 'json', str(path)], capture_output=True, text=True, check=True, encoding='utf-8', errors='replace').stdout)
                return [float(f['best_effort_timestamp_time']) for f in data['frames']]
            original = timestamps(source)
            output = timestamps(edit / 'cut.mp4')
            first = [t for t in original if 0 <= t < 0.4]
            second = [t for t in original if 0.5 <= t < 0.9]
            expected = first + [t - second[0] + 0.4 for t in second]
            self.assertEqual(len(output), len(expected))
            self.assertTrue(all(abs(a - b) <= 1e-6 for a, b in zip(output, expected)), (output, expected))

    def test_source_sixty_fps_is_not_reduced(self):
        self.assertEqual(output_fps_for(60), 60)

    def test_fractional_cut_preserves_exact_rate_and_source_dimensions(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            edit = root / 'edit'; edit.mkdir()
            source = root / 'source.mp4'
            subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                            'testsrc2=s=160x96:r=30000/1001:d=1', '-c:v', 'libx264', '-pix_fmt', 'yuv420p',
                            '-color_range', 'tv', '-colorspace', 'bt709', '-color_trc', 'bt709',
                            '-color_primaries', 'bt709', str(source)], check=True)
            edl = edit / 'edl.json'
            edl.write_text(json.dumps({'sources': {'s': str(source)}, 'ranges': [{'id': 'a', 'source': 's', 'start': 0, 'end': 1}]}), encoding='utf-8')
            result = subprocess.run([sys.executable, str(HELPERS / 'render_cut.py'), str(edl), '--no-jcut'], capture_output=True, text=True, encoding='utf-8', errors='replace')
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            facts = probe_video(edit / 'cut.mp4')
            self.assertEqual(Fraction(facts['fps_str']), Fraction(30000, 1001))
            self.assertEqual((facts['width'], facts['height']), (160, 96))

    def test_fractional_render_rate_is_restamped_onto_the_exact_grid(self):
        from common import _video_stamps, restamp_cfr
        rate = '263400/4391'
        with tempfile.TemporaryDirectory() as temp:
            exact, render, coarse = (Path(temp) / name for name in ('exact.mp4', 'render.mp4', 'coarse.mp4'))
            subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', f'testsrc=s=64x64:r={rate}:d=1', '-c:v', 'libx264',
                '-pix_fmt', 'yuv420p', '-video_track_timescale', '263400', str(exact)], check=True)
            # Renderer-style rounding jitter of -2, 0 or +2 ticks; the same shift keeps the B-frame decode order valid.
            jitter = '2*(N-3*floor(N/3))-2'
            subprocess.run(['ffmpeg', '-v', 'error', '-i', str(exact), '-c', 'copy', '-bsf:v', f'setts=pts=PTS+{jitter}:dts=DTS+{jitter}',
                '-video_track_timescale', '263400', str(render)], check=True)
            b_frames = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries', 'stream=has_b_frames',
                '-of', 'csv=p=0', str(render)], capture_output=True, text=True, check=True, encoding='utf-8', errors='replace').stdout.strip()
            self.assertNotEqual(b_frames, '0')
            self.assertNotEqual(Fraction(probe_video(render)['fps_str']), Fraction(rate))
            restamp_cfr(render, rate)
            shown = sorted(pts for pts, _ in _video_stamps(render)[1])
            self.assertEqual({b - a for a, b in zip(shown, shown[1:])}, {4391})
            # The delivery frameRate check compares exactly this value.
            self.assertEqual(Fraction(probe_video(render)['fps_str']), Fraction(rate))
            subprocess.run(['ffmpeg', '-v', 'error', '-i', str(exact), '-c', 'copy', '-video_track_timescale', '19200', str(coarse)], check=True)
            with self.assertRaisesRegex(ValueError, 'cannot represent'):
                restamp_cfr(coarse, rate)


if __name__ == '__main__':
    unittest.main()
