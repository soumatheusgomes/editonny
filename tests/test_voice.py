"""Local voice processing must preserve media timing and original files."""
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
VOICE = ROOT / 'skills/editonny/visuals/voice.py'


class VoiceTests(unittest.TestCase):
    def test_nonfinite_voice_intervals_are_rejected_before_processing(self):
        spec = importlib.util.spec_from_file_location('voice_processor', VOICE)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        presets = module.load_presets()
        for field in ('start', 'end', 'strength'):
            for value in (float('nan'), float('inf'), float('-inf')):
                with self.subTest(field=field, value=value), self.assertRaises(module.VoiceError):
                    module.normalize_segments([{'effect': 'old-phone', 'start': 0,
                                                'end': 1, field: value}], presets)

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg is required')
    def test_project_rebuild_and_clear_preserve_unowned_media(self):
        with tempfile.TemporaryDirectory(prefix='editonny voice project ') as temporary:
            root = Path(temporary)
            edit, project = root / 'edit', root / 'project'
            edit.mkdir()
            (project / 'assets').mkdir(parents=True)
            source = edit / 'cut.mp4'
            subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                            'color=c=green:s=160x240:r=24:d=1', '-f', 'lavfi', '-i',
                            'sine=frequency=440:duration=1', '-c:v', 'libx264', '-pix_fmt',
                            'yuv420p', '-c:a', 'aac', '-shortest', str(source)], check=True)
            original = source.read_bytes()
            collision = edit / 'cut_voice.mp4'
            collision.write_bytes(b'Unrelated personal footage')
            config = project / 'project.json'
            config.write_text(json.dumps({'title': 'Voice lifecycle', 'edit': '../edit'}), encoding='utf-8')
            def run(*arguments):
                result = subprocess.run([sys.executable, str(VOICE), '--project', str(project),
                                         *arguments], capture_output=True, text=True, encoding='utf-8', errors='replace')
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                return result.stdout
            run('--effect', 'old-phone', '--start', '0.2', '--end', '0.8')
            self.assertEqual(collision.read_bytes(), b'Unrelated personal footage')
            settings = json.loads(config.read_text(encoding='utf-8'))
            processed = edit / settings['video']
            self.assertNotEqual(processed, collision)
            marker = json.loads(processed.with_suffix('.json').read_text(encoding='utf-8'))
            self.assertIn('sourceSignature', marker)
            self.assertIn('outputSignature', marker)
            self.assertIn('speech', marker)
            self.assertIn('report', marker)
            self.assertIn(': current', run())
            self.assertIn('Rebuilding', run('--effect', 'old-phone', '--start', '0.2', '--end', '0.8', '--ramp', '60'))
            self.assertEqual(json.loads(processed.with_suffix('.json').read_text(encoding='utf-8'))['rampMs'], 60)
            self.assertIn('Rebuilding', run('--rebuild'))
            run('--clear')
            self.assertEqual(json.loads(config.read_text(encoding='utf-8')), {'title': 'Voice lifecycle', 'edit': '../edit'})
            self.assertFalse(processed.exists())
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(collision.read_bytes(), b'Unrelated personal footage')

    def test_list_exposes_english_effect_names(self):
        result = subprocess.run([sys.executable, str(VOICE), '--list'], capture_output=True, text=True, encoding='utf-8', errors='replace')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('old-phone', result.stdout)
        self.assertIn('robot', result.stdout)

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg is required')
    def test_temporary_output_never_replaces_the_source(self):
        with tempfile.TemporaryDirectory(prefix='editonny voice temp ') as temporary:
            root = Path(temporary)
            source, output = root / 'clip.processing.mp4', root / 'clip.mp4'
            subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                            'color=c=green:s=160x240:r=24:d=1', '-f', 'lavfi', '-i',
                            'sine=frequency=440:duration=1', '-c:v', 'libx264', '-pix_fmt',
                            'yuv420p', '-c:a', 'aac', '-shortest', str(source)], check=True)
            before = source.read_bytes()
            result = subprocess.run([sys.executable, str(VOICE), '--file', str(source),
                                     '--output', str(output), '--effect', 'old-phone',
                                     '--start', '0.2', '--end', '0.8'], capture_output=True, text=True, encoding='utf-8', errors='replace')
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertEqual(source.read_bytes(), before)
            self.assertEqual(sorted(p.name for p in root.iterdir()), ['clip.mp4', 'clip.processing.mp4'])

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg is required')
    def test_effect_preserves_original_frames_and_audio_timing(self):
        with tempfile.TemporaryDirectory(prefix='editonny voice ') as temporary:
            root = Path(temporary)
            source, output = root / 'source.mp4', root / 'output.mp4'
            subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                            'color=c=green:s=160x240:r=24:d=1', '-f', 'lavfi', '-i',
                            'sine=frequency=440:duration=1', '-c:v', 'libx264', '-pix_fmt',
                            'yuv420p', '-c:a', 'aac', '-shortest', str(source)], check=True)
            before = hashlib.sha256(source.read_bytes()).hexdigest()
            result = subprocess.run([sys.executable, str(VOICE), '--file', str(source),
                                     '--output', str(output), '--effect', 'old-phone',
                                     '--start', '0.2', '--end', '0.8'], capture_output=True, text=True, encoding='utf-8', errors='replace')
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), before)
            def probe(path):
                return json.loads(subprocess.check_output(['ffprobe', '-v', 'error',
                    '-show_streams', '-show_format', '-of', 'json', str(path)]))
            original, processed = probe(source), probe(output)
            self.assertEqual(original['streams'][0]['nb_frames'], processed['streams'][0]['nb_frames'])
            self.assertLess(abs(float(original['format']['duration']) - float(processed['format']['duration'])), 0.03)


if __name__ == '__main__':
    unittest.main()
