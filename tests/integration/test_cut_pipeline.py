"""Exercise the English EDL, render, caption, and verification contracts together."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

HELPERS = Path(__file__).resolve().parents[2] / 'skills/editonny/engine/helpers'


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg is required')
class CutPipelineTests(unittest.TestCase):
    def test_cut_maps_only_selected_words_and_detects_stale_edl(self):
        with tempfile.TemporaryDirectory(prefix='editonny cut pipeline ') as temporary:
            root = Path(temporary)
            edit = root / 'edit'
            (edit / 'transcripts').mkdir(parents=True)
            source = root / 'source.mp4'
            subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                'testsrc2=s=160x240:r=24:d=3', '-f', 'lavfi', '-i',
                r"aevalsrc=0.2*sin(2*PI*440*t)*(between(t\,0.4\,0.9)+between(t\,1.4\,1.9)+between(t\,2.4\,2.9)):s=48000:d=3",
                '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-shortest', str(source)], check=True)
            original_hash = hashlib.sha256(source.read_bytes()).hexdigest()
            (edit / 'transcripts/source.json').write_text(json.dumps({'words': [
                {'text': word, 'start': start, 'end': start + 0.5}
                for word, start in [('Opening', 0.4), ('Excluded', 1.4), ('Closing', 2.4)]]}), encoding='utf-8')
            edl = edit / 'edl.json'
            # No output size: the cut keeps the source size and must not be mistaken for a vertical preview.
            data = {'sources': {'source': str(source)}, 'output': {'fps': 24},
                    'ranges': [{'id': 'first', 'source': 'source', 'start': 0.25, 'end': 1.125},
                               {'id': 'last', 'source': 'source', 'start': 2.25, 'end': 3.0}]}
            edl.write_text(json.dumps(data), encoding='utf-8')
            def run(helper, *arguments):
                result = subprocess.run([sys.executable, str(HELPERS / helper), *map(str, arguments)],
                                        capture_output=True, text=True, timeout=60, encoding='utf-8', errors='replace')
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                return result.stdout
            run('render_cut.py', edl, '--no-jcut')
            timeline = json.loads((edit / 'cut_timeline.json').read_text(encoding='utf-8'))
            self.assertEqual([s['id'] for s in timeline['segments']], ['first', 'last'])
            run('captions.py', '--edit-dir', edit)
            captions = json.loads((edit / 'captions.json').read_text(encoding='utf-8'))
            self.assertIn('Opening', json.dumps(captions))
            self.assertIn('Closing', json.dumps(captions))
            self.assertNotIn('Excluded', json.dumps(captions))
            state = (edit / 'state.json').read_bytes()
            run('verify_cut.py', edl, edit / 'cut.mp4', '--without-approval', '--report-only')
            report = json.loads((edit / 'verify/cut_report.json').read_text(encoding='utf-8'))
            self.assertNotIn('STALE-TIMELINE', [f['code'] for f in report['flags']])
            self.assertNotIn('PREVIEW', [f['code'] for f in report['flags']])
            self.assertEqual((edit / 'state.json').read_bytes(), state)
            data['ranges'][0]['end'] = 1.0
            edl.write_text(json.dumps(data), encoding='utf-8')
            run('verify_cut.py', edl, edit / 'cut.mp4', '--without-approval', '--report-only')
            report = json.loads((edit / 'verify/cut_report.json').read_text(encoding='utf-8'))
            self.assertIn('STALE-TIMELINE', [f['code'] for f in report['flags']])
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), original_hash)


if __name__ == '__main__':
    unittest.main()
