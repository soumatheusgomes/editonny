"""Reference measurements must produce usable English reports from actual media."""
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

HELPER = Path(__file__).resolve().parents[2] / 'skills/editonny/engine/helpers/measure_reference.py'


class ReferenceTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg is required')
    def test_reference_report_and_rules_preserve_source(self):
        with tempfile.TemporaryDirectory(prefix='editonny reference ') as temporary:
            root = Path(temporary)
            video, output = root / 'source.mp4', root / 'measurement'
            subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                            'testsrc2=s=160x240:r=24:d=2', '-f', 'lavfi', '-i',
                            'sine=frequency=440:duration=2', '-c:v', 'libx264',
                            '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-shortest', str(video)], check=True)
            before = hashlib.sha256(video.read_bytes()).hexdigest()
            result = subprocess.run([sys.executable, str(HELPER), str(video), '--output',
                                     str(output), '--without-speech', '--without-text'],
                                    capture_output=True, text=True, timeout=60, encoding='utf-8', errors='replace')
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            rules = json.loads((output / 'reference-rules.json').read_text(encoding='utf-8'))
            self.assertEqual(rules['metadata']['aspect_ratio'], '2:3')
            self.assertTrue(rules['metadata']['has_audio'])
            self.assertIn('pacing', rules)
            self.assertIn('integrated_lufs', rules['audio']['loudness'])
            self.assertIn('edit_data', rules)
            self.assertIn('## 6. Audio', (output / 'style-guide.md').read_text(encoding='utf-8'))
            self.assertTrue(list((output / 'contact-sheets').glob('sheet_*.jpg')))
            self.assertEqual(hashlib.sha256(video.read_bytes()).hexdigest(), before)

    def test_caption_measurement_consumes_english_ocr_coordinates(self):
        spec = importlib.util.spec_from_file_location('reference_measurement', HELPER)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        words = [{'text': word, 'start': i * 0.5, 'end': i * 0.5 + 0.4}
                 for i, word in enumerate(['Editing', 'preserves', 'clear', 'speech', 'and', 'meaning'])]
        doc = {'backend': 'vision', 'fps': 2, 'samples': [
            {'t': i * 0.5, 'items': [{'text': word['text'], 'center_y': 0.7,
                                     'center_x': 0.5, 'height': 0.04, 'width': 0.2}]}
            for i, word in enumerate(words)]}
        result = module.analyze_texts(doc, words, 1080, 1920)
        self.assertTrue(result['has_captions'])
        self.assertTrue(result['matched_speech'])
        self.assertAlmostEqual(result['center_y'], 0.7, places=2)
        self.assertGreater(result['standard_line_height'], 0)


if __name__ == '__main__':
    unittest.main()
