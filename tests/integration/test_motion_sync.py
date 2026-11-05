"""Exercise camera planning and synchronization through their public CLIs."""
import json
from pathlib import Path
import shutil
import runpy
import subprocess
import sys
import tempfile
import unittest
import wave

import numpy as np
from PIL import Image

HELPERS = Path(__file__).resolve().parents[2] / 'skills/editonny/engine/helpers'


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg required')
class MotionSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def run_cli(self, name, *args):
        result = subprocess.run([sys.executable, str(HELPERS / name), *map(str, args)],
                                capture_output=True, text=True, timeout=60, encoding='utf-8', errors='replace')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def test_camera_default_json_and_undo_preserve_explicit_content(self):
        project = self.root / 'project'
        edit = self.root / 'edit'
        project.mkdir()
        (edit / 'transcripts').mkdir(parents=True)
        manual = {'scene': 'fullscreen', 'start': 0, 'end': 0.5, 'camera_motion': False, 'note': 'Olá café'}
        cfg = {'edit': '../edit', 'blocks': [manual], 'scenes': [
            {'file': 'card.html', 'overPerson': True, 'start': 'Hello', 'end': 'End', 'startAfter': 5, 'endAfter': 7}]}
        (project / 'project.json').write_text(json.dumps(cfg), encoding='utf-8')
        (edit / 'cut_timeline.json').write_text(json.dumps({'duration': 10, 'segments': [{'out_start': 0}]}), encoding='utf-8')
        words = [{'text': text, 'start': t, 'end': t + .4} for t, text in
                 [(1, 'Hello'), (2, '25'), (3, 'End.'), (6, 'Hello'), (7, '50'), (8, 'End.')]]
        (edit / 'transcripts/cut.json').write_text(json.dumps({'words': words}), encoding='utf-8')
        self.assertIn('camera motion:', self.run_cli('camera_motion.py', project))
        report = json.loads(self.run_cli('camera_motion.py', project, '--json'))
        self.assertEqual(report['overPerson'], [[6, 8]])
        self.assertGreater(report['automatic_blocks'], 0)
        self.assertIn('largest_interval', report)
        actual = json.loads((project / 'project.json').read_text(encoding='utf-8'))
        self.assertEqual(actual['blocks'][0], manual)
        under_card = [block for block in actual['blocks'] if block.get('auto') and block['start'] >= 6 and block.get('end', 10) <= 8]
        self.assertTrue(under_card)
        self.assertTrue(all(block['zoom'] == 1 and not block.get('zooms') for block in under_card))
        self.run_cli('camera_motion.py', project, '--undo')
        self.assertEqual(json.loads((project / 'project.json').read_text(encoding='utf-8'))['blocks'], [manual])

    def test_sync_default_and_audio_only_evidence_preserve_manual_adjustment(self):
        sample = self.root / 'source.wav'
        sr = 16000
        rng = np.random.default_rng(2026)
        t = np.arange(sr * 10) / sr
        envelope = np.repeat(rng.uniform(.02, .9, 100), sr // 10)
        samples = np.clip(rng.normal(0, .25, len(t)) * envelope, -1, 1)
        with wave.open(str(sample), 'wb') as wav:
            wav.setparams((1, 2, sr, 0, 'NONE', 'not compressed'))
            wav.writeframes((samples * 32767).astype('<i2').tobytes())
        output = self.root / 'synchronization.json'
        args = [f'main={sample}', f'other={sample}', '-o', output]
        self.run_cli('sync_cameras.py', *args)
        doc = json.loads(output.read_text(encoding='utf-8'))
        row = doc['sources']['other']
        self.assertAlmostEqual(row['offset'], 0, places=3)
        self.assertEqual(row['confidence'], 'good')
        self.assertIn('signature', row)
        self.assertIn('overlap_on_primary', row)
        self.assertTrue(any(window['used'] for window in row['windows']))
        row['adjustment_s'] = .025
        output.write_text(json.dumps(doc), encoding='utf-8')
        self.assertIn('no video', self.run_cli('sync_cameras.py', *args, '--evidence'))
        self.assertEqual(json.loads(output.read_text(encoding='utf-8'))['sources']['other']['adjustment_s'], .025)

    def test_filmstrip_writes_a_sheet(self):
        video = self.root / 'sample.mp4'
        subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=160x240:rate=10:duration=1',
                        '-c:v', 'libx264', str(video)], check=True)
        sheet = self.root / 'sheet.png'
        self.run_cli('filmstrip.py', video, '0', '0.8', '--output', sheet, '--columns', '3')
        self.assertGreater(sheet.stat().st_size, 100)
        with Image.open(sheet) as image:
            self.assertEqual(image.width, 3 * 240)
            self.assertEqual(image.height, 3 * 360)

    def test_camera_speech_lexicons_keep_recorded_language_words(self):
        camera = runpy.run_path(str(HELPERS / 'camera_motion.py'))
        self.assertTrue({'de', 'em', 'nos', 'como'} <= camera['WEAK'])
        self.assertTrue({'nenhum', 'nenhuma', 'never', 'not'} <= camera['NEGATIONS'])
        self.assertTrue({'dois', 'two'} <= camera['NUMBERS'])
        self.assertFalse({'start', 'nodes', 'method'} & camera['WEAK'])
