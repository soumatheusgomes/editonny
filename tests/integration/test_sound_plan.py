"""Project sound plans must complete with copied licenses and explicit limits."""
import importlib.util
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

HELPER = Path(__file__).resolve().parents[2] / 'skills/editonny/engine/helpers/sound_plan.py'
sys.path.insert(0, str(HELPER.parent))


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg is required')
class SoundPlanTests(unittest.TestCase):
    def test_failed_peak_retries_do_not_return_success(self):
        spec = importlib.util.spec_from_file_location('sound_peak_test', HELPER)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        result = subprocess.CompletedProcess([], 0, '', '{}')
        with patch.object(module, 'chain_loudnorm', return_value=('null', {})), \
             patch.object(module.subprocess, 'run', return_value=result), \
             patch.object(module, 'loudness', return_value={'input_tp': '-0.8', 'input_i': '-14'}):
            with self.assertRaises((SystemExit, ValueError, RuntimeError)):
                module.finalize('video.mp4', 'audio.wav', 'output.mp4')

    def test_built_in_piece_sounds_are_known_types(self):
        spec = importlib.util.spec_from_file_location('sound_types_test', HELPER)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        build = (HELPER.parents[1] / 'project-template/build.py').read_text(encoding='utf-8')
        kinds = {k for pair in re.findall(r"M\.sound\('([\w-]+)'(?: if [^,]+ else '([\w-]+)')?", build) for k in pair if k}
        self.assertIn('tick', kinds)
        # The plan rejects any suggested type missing from TYPE_NAMES.
        self.assertEqual({k for k in kinds if module.normal_value(k) not in module.TYPE_NAMES}, set())

    def test_project_plan_respects_tail_and_copies_woven_license(self):
        with tempfile.TemporaryDirectory(prefix='editonny sound plan ') as temporary:
            root = Path(temporary)
            edit, project = root / 'edit', root / 'project'
            edit.mkdir()
            project.mkdir()
            (edit / 'cut_timeline.json').write_text(json.dumps({'duration': 3}))
            (project / 'project.json').write_text(json.dumps({'edit': '../edit', 'endHold': 0.5,
                'endCard': {'enabled': True, 'seconds': 1},
                'sounds': [{'file': 'personal.wav', 'at': 0.2, 'note': 'Keep this manually chosen cue'}]}))
            (project / 'events.json').write_text(json.dumps([{'t': 1, 'type': 'pop'}]))
            for options in ([], ['--mode', 'speech', '--max', '1']):
                result = subprocess.run([sys.executable, str(HELPER), '--project', str(project), *options],
                                        capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertNotIn('mix_music.py', result.stdout)
                plan = json.loads((project / 'sound_plan.json').read_text())
                self.assertEqual(plan['video_duration'], 4.5)
                self.assertEqual(len(plan['sounds']), 1)
                self.assertTrue(plan['verification']['checked'])
                cfg = json.loads((project / 'project.json').read_text())
                self.assertEqual(len(cfg['sounds']), 2)
                self.assertTrue(any(s.get('note') == 'Keep this manually chosen cue' for s in cfg['sounds']))
                self.assertTrue((project / 'assets/sfx/LICENSE-woven.md').is_file())
                self.assertIn('CC0', (project / 'assets/sfx/LICENSES.txt').read_text())

    def test_same_named_sounds_never_replace_each_other(self):
        spec = importlib.util.spec_from_file_location('sound_names_test', HELPER)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory(prefix='editonny sound names ') as temporary:
            root = Path(temporary)
            (root / 'a').mkdir()
            (root / 'b').mkdir()
            (root / 'a/pop.wav').write_bytes(b'AAAA')
            (root / 'b/pop.wav').write_bytes(b'BBBB')
            sfx = root / 'project/assets/sfx'
            sfx.mkdir(parents=True)
            (sfx / 'pop.wav').write_bytes(b'MINE')        # Same name and size, used by a manual cue.
            from common import content_hash
            (sfx / f"pop_{content_hash(root / 'b/pop.wav')[:10]}.wav").write_bytes(b'EDIT')   # A hashed name edited by hand.
            config = root / 'project/project.json'
            sound = {'peak': 1.0, 'start': 1.0, 'offset': 0.0, 'dur': 0.2, 'volume': 1.0, 'level_db': -20,
                     'fade_out': 0.05, 'type': 'pop', 'note': 'test'}
            plan = {'sounds': [dict(sound, file=str(root / 'a/pop.wav')), dict(sound, file=str(root / 'b/pop.wav')),
                               dict(sound, file=str(root / 'a/pop.wav'))]}
            module.apply_in_project(plan, root / 'project', {'cfg': {}, 'file': config}, None)
            self.assertEqual((sfx / 'pop.wav').read_bytes(), b'MINE')
            files = [s['file'] for s in json.loads(config.read_text())['sounds']]
            self.assertEqual([(root / 'project' / f).read_bytes() for f in files], [b'AAAA', b'BBBB', b'AAAA'])
            self.assertEqual(len([p for p in sfx.iterdir() if p.suffix == '.wav']), 4)

    def test_library_prefix_resolves_current_category_schema(self):
        spec = importlib.util.spec_from_file_location('sound_plan_processor', HELPER)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory(prefix='editonny effects ') as temporary:
            library = Path(temporary)
            (library / 'effect.wav').write_bytes(b'Fixture only; resolution does not decode audio')
            (library / 'effects.json').write_text(json.dumps({'effects': {'pop-001': {
                'file': 'effect.wav', 'category': 'pop', 'name': 'Pop'}}}))
            module.LIBRARY = library
            self.assertEqual(module.resolve_sound('library:pop-001'), library / 'effect.wav')
            self.assertEqual(module.resolve_sound('library:pop'), library / 'effect.wav')


if __name__ == '__main__':
    unittest.main()
