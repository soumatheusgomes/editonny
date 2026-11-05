"""Quality checks must use the same English timing and OCR contracts as the editor."""
import importlib.util
import json
import shutil
import subprocess
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

HELPERS = Path(__file__).resolve().parents[2] / 'skills/editonny/engine/helpers'


class QualityGateTests(unittest.TestCase):
    def test_managed_blocked_checks_keep_operational_exit_status(self):
        from contextlib import ExitStack, redirect_stdout
        import io
        module = self.module
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            root = Path(tmp); video = root / 'candidate.mp4'; video.write_bytes(b'candidate')
            project = root / 'project'; project.mkdir()
            (project / 'project.json').write_text(json.dumps({'workflow': {'audio': {'mode': 'preserve'}}, 'captions': {'enabled': False}}), encoding='utf-8')
            for name, result in (('require_binary', None), ('probe_video', {'width': 128, 'height': 64, 'duration': 1, 'has_audio': False}),
                                 ('black_intervals', []), ('end_of_speech', None), ('sheet_value', None),
                                 ('text_outside', {'findings': [], 'reader': None})):
                stack.enter_context(patch.object(module, name, return_value=result))
            stack.enter_context(patch('fidelity.verify_media', return_value={'status': 'blocked', 'checks': []}))
            output = io.StringIO()
            with redirect_stdout(output):
                status = module.main([str(video), '--project', str(project), '--json'])
            self.assertEqual(status, 2)
            self.assertEqual(json.loads(output.getvalue())['status'], 'blocked')

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(HELPERS))
        spec = importlib.util.spec_from_file_location('quality_gate_processor', HELPERS / 'quality_gate.py')
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

    def test_footage_exclusions_resolve_repeated_speech_anchors(self):
        with tempfile.TemporaryDirectory(prefix='editonny quality anchors ') as temporary:
            root = Path(temporary)
            project, edit = root / 'project', root / 'edit'
            project.mkdir()
            (edit / 'transcripts').mkdir(parents=True)
            (edit / 'cut_timeline.json').write_text(json.dumps({'duration': 10}), encoding='utf-8')
            (edit / 'transcripts/cut.json').write_text(json.dumps({'words': [
                {'text': word, 'start': t, 'end': t + 0.2}
                for word, t in [('Hello', 1), ('End', 3), ('Hello', 6), ('End', 8)]]}), encoding='utf-8')
            cfg = {'edit': '../edit', 'blocks': [{'scene': 'evidence', 'start': 'Hello',
                    'startAfter': 5, 'end': 'End', 'endAfter': 7}]}
            self.assertEqual(self.module.footage_intervals(cfg, project, 10), [(6, 8)])

    def test_meter_failure_with_partial_json_cannot_pass(self):
        result = subprocess.CompletedProcess([], 1, '', '{"input_i":"-14","input_tp":"-1.5"}')
        with patch.object(self.module.subprocess, 'run', return_value=result):
            with self.assertRaises(RuntimeError):
                self.module.volume(Path('partial.mp4'))

    def test_nonfinite_loudness_is_not_valid_voice_measurement(self):
        result = subprocess.CompletedProcess([], 0, '', '{"input_i":"-inf","input_tp":"-inf"}')
        with patch.object(self.module.subprocess, 'run', return_value=result):
            with self.assertRaises(RuntimeError):
                self.module.volume(Path('silent.mp4'))

    def test_ocr_uses_current_extractor_and_reports_english_fields(self):
        module = self.module
        line = {'t': 'Sample', 'x': 0.01, 'y': 0.5, 'w': 0.2, 'h': 0.05, 'c': 1}
        with patch.object(module.cd, 'reader_value', return_value=('fixture', None)), \
             patch.object(module, 'probe_video', return_value={'fps': 24, 'duration': 1}), \
             patch.object(module.cd, 'extract_frames', return_value=1) as extract, \
             patch.object(module.cd, 'read_directory', return_value={'f_00001.jpg': [line]}):
            result = module.text_outside(Path('fixture.mp4'), 1080, 1920, 1)
        extract.assert_called_once()
        self.assertEqual(result['reader'], 'fixture')
        self.assertEqual(len(result['findings']), 1)
        self.assertEqual(result['findings'][0]['reason'], 'touching the frame edge')

    def test_volume_adjustment_refuses_an_occupied_backup_without_writing(self):
        with tempfile.TemporaryDirectory(prefix='editonny protected volume ') as temporary:
            root = Path(temporary)
            video = root / 'final.mp4'
            backup = root / 'final_before_volume.mp4'
            prior_temporary = root / 'final_new_volume.mp4'
            video.write_bytes(b'Current original')
            backup.write_bytes(b'Unrelated existing backup')
            prior_temporary.write_bytes(b'Unrelated existing output')
            with patch.object(self.module, 'volume') as measure:
                with self.assertRaises(RuntimeError):
                    self.module.adjust_volume(video)
            measure.assert_not_called()
            self.assertEqual(video.read_bytes(), b'Current original')
            self.assertEqual(backup.read_bytes(), b'Unrelated existing backup')
            self.assertEqual(prior_temporary.read_bytes(), b'Unrelated existing output')

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg is required')
    def test_volume_adjustment_preserves_original_and_unrelated_temporary_file(self):
        with tempfile.TemporaryDirectory(prefix='editonny normalize volume ') as temporary:
            root = Path(temporary)
            video = root / 'final.mp4'
            subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                'color=c=blue:s=160x240:r=24:d=1', '-f', 'lavfi', '-i',
                'sine=frequency=440:duration=1', '-c:v', 'libx264', '-pix_fmt', 'yuv420p',
                '-c:a', 'aac', '-shortest', str(video)], check=True)
            original = video.read_bytes()
            prior_temporary = root / 'final_new_volume.mp4'
            prior_temporary.write_bytes(b'Unrelated existing output')
            backup = self.module.adjust_volume(video)
            self.assertEqual(backup.read_bytes(), original)
            self.assertEqual(prior_temporary.read_bytes(), b'Unrelated existing output')
            self.assertNotEqual(video.read_bytes(), original)
            self.assertLess(abs(self.module.volume(video)['lufs'] + 14), 1)
            self.assertEqual(sorted(p.name for p in root.iterdir()),
                             ['final.mp4', 'final_before_volume.mp4', 'final_new_volume.mp4'])

    def test_failed_volume_publication_keeps_the_current_original_recoverable(self):
        with tempfile.TemporaryDirectory(prefix='editonny failed volume ') as temporary:
            video = Path(temporary) / 'final.mp4'
            video.write_bytes(b'Current original')
            def encode(command, **_kwargs):
                Path(command[-1]).write_bytes(b'Normalized output')
                return subprocess.CompletedProcess(command, 0, '', '')
            measurement = {'_loudnorm': {key: '0' for key in
                ('input_i', 'input_tp', 'input_lra', 'input_thresh', 'target_offset')}}
            measurement.update(lufs=-14, peak_db=-1.5)
            with patch.object(self.module, 'volume', return_value=measurement), \
                 patch.object(self.module.subprocess, 'run', side_effect=encode), \
                 patch.object(self.module.os, 'replace', side_effect=OSError('Publication failed')):
                with self.assertRaises(OSError):
                    self.module.adjust_volume(video)
            self.assertEqual(video.read_bytes(), b'Current original')
            self.assertEqual(video.with_name('final_before_volume.mp4').read_bytes(), b'Current original')
            self.assertEqual(len(list(video.parent.iterdir())), 2)

    def test_quiet_intervals_respect_protected_and_revision_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            edit, project = root / 'edit', root / 'project'
            edit.mkdir()
            project.mkdir()
            source = root / 'source.mp4'
            source.write_bytes(b'source')
            (edit / 'source_manifest.json').write_text(json.dumps({'sources': [{'id': 'take', 'path': str(source)}]}), encoding='utf-8')
            # Source 10-20 s plays at output 0-10 s.
            (edit / 'cut_timeline.json').write_text(json.dumps({'segments': [
                {'source_path': str(source), 'src_start': 10, 'src_end': 20, 'out_start': 0, 'out_end': 10}]}), encoding='utf-8')
            quiet = [(1.2, 1.8), (4.2, 4.8), (7.0, 7.6), (2.5, 4.1)]
            editing = {'scope': 'full', 'protectedIntervals': [{'sourceId': 'take', 'start': 14, 'end': 15}]}
            cfg = {'edit': '../edit', 'workflow': {'sourceIds': ['take'], 'editing': editing}}
            # Output 4-5 s is protected: its pause passes, the unprotected part of 2.5-4.1 s still fails.
            self.assertEqual(self.module.scoped_quiet(cfg, project, quiet), [(1.2, 1.8), (7.0, 7.6), (2.5, 4.0)])
            editing.update(scope='intervals', intervals=[{'sourceId': 'take', 'start': 11, 'end': 12.6}])
            # Only the in-scope part counts, and a sliver under the minimum duration does not.
            self.assertEqual(self.module.scoped_quiet(cfg, project, quiet + [(2.55, 2.9)]), [(1.2, 1.8)])
            self.assertEqual(self.module.scoped_quiet({}, project, quiet), quiet)
            # A pause across a cut inside one authorized source interval counts whole.
            (edit / 'cut_timeline.json').write_text(json.dumps({'segments': [
                {'source_path': str(source), 'src_start': 0, 'src_end': 1, 'out_start': 0, 'out_end': 1},
                {'source_path': str(source), 'src_start': 3, 'src_end': 4, 'out_start': 1, 'out_end': 2}]}), encoding='utf-8')
            editing.update(protectedIntervals=[], intervals=[{'sourceId': 'take', 'start': 0, 'end': 4}])
            self.assertEqual(self.module.scoped_quiet(cfg, project, [(0.8, 1.2)]), [(0.8, 1.2)])

    def test_failed_ocr_is_not_a_frame_without_text(self):
        sys.path.insert(0, str(HELPERS))
        from audit_data import read_directory
        from PIL import Image
        with tempfile.TemporaryDirectory() as tmp:
            Image.new('RGB', (16, 16)).save(Path(tmp) / 'f_00001.jpg')
            with self.assertRaises(subprocess.CalledProcessError):
                read_directory(('tesseract', shutil.which('false') or '/usr/bin/false'), Path(tmp))


if __name__ == '__main__':
    unittest.main()
