"""Regression checks for the bundled engine and visual resources."""
import contextlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import warnings

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / 'skills/editonny/engine'
VISUALS = ROOT / 'skills/editonny/visuals'


class EngineTests(unittest.TestCase):
    def test_scene_application_keeps_audio_unchanged_without_suggesting_sounds(self):
        sys.path.insert(0, str(VISUALS))
        import apply
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            sounds = [{'file': 'assets/user-sound.wav', 'start': 1}]
            (project / 'project.json').write_text(json.dumps({'sounds': sounds}))
            events = project / 'events.json'
            events.write_text('[{"t": 1, "sound": "user-sound.wav"}]')
            original_events = events.read_bytes()
            output = io.StringIO()
            with mock.patch.object(apply, 'copy_assets', return_value=0), contextlib.redirect_stdout(output), warnings.catch_warnings():
                warnings.simplefilter('ignore', ResourceWarning)
                apply.use_scene(str(project), 'three-steps', 0, 6)
            self.assertEqual(json.loads((project / 'project.json').read_text())['sounds'], sounds)
            self.assertEqual(events.read_bytes(), original_events)
            self.assertNotIn('--sounds', output.getvalue())

    def test_scene_sound_generation_respects_confirmed_audio_independently_of_visuals(self):
        sys.path.insert(0, str(ENGINE / 'helpers'))
        from common import content_hash
        from workflow_fixtures import config, confirmation
        from workspace import create_workspace
        for effects, scenes in ((False, 'requested'), (True, 'requested'), (True, 'off')):
            with self.subTest(effects=effects, scenes=scenes), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                source = root / 'original.mp4'
                source.write_bytes(b'original footage')
                inspection = {'sources': [{'id': 'source', 'path': str(source),
                                          'sha256': content_hash(source), 'streams': [0]}]}
                c = config(root / 'downloads')
                c['workflow']['audio']['effects'] = effects
                c['workflow']['visuals']['scenes'] = scenes
                project = create_workspace(root / 'downloads', 'Audio scope', inspection, c, confirmation(c, inspection))
                path = project / 'project.json'
                if scenes != 'off':
                    current = json.loads(path.read_text())
                    current['scenes'] = [{'sceneId': 'transition-glow', 'start': 0, 'end': 1}]
                    path.write_text(json.dumps(current))
                events = project / 'events.json'
                events.write_text('[{"t": 2, "sound": "user-sound.wav"}]')
                original_project, original_events = path.read_bytes(), events.read_bytes()
                result = subprocess.run([sys.executable, str(VISUALS / 'apply.py'),
                    '--project', str(project), '--sounds'], capture_output=True, text=True)
                self.assertEqual(path.read_bytes(), original_project)
                self.assertEqual(source.read_bytes(), b'original footage')
                if not effects:
                    self.assertNotEqual(result.returncode, 0, result.stdout)
                    self.assertIn('Sound effects were not confirmed', result.stderr)
                    self.assertEqual(events.read_bytes(), original_events)
                else:
                    self.assertEqual(result.returncode, 0, result.stderr)
                    generated = json.loads(events.read_text())
                    self.assertEqual(generated[0 if scenes == 'off' else -1]['sound'], 'user-sound.wav')
                    self.assertEqual(sum(bool(e.get('sceneId')) for e in generated), int(scenes != 'off'))

    def test_missing_media_does_not_modify_project(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project = root / 'project'
            project.mkdir()
            config = project / 'project.json'
            config.write_text('{}')
            bundle = root / 'incomplete bundle'
            bundle.mkdir()
            for name in ('apply.py', 'resources.py', 'media.py', 'media.json', 'catalog.json', 'numbers.json'):
                (bundle / name).write_bytes((VISUALS / name).read_bytes())
            (bundle / 'scenes').mkdir()
            scene = 'object-card.html'
            (bundle / 'scenes' / scene).write_bytes((VISUALS / 'scenes' / scene).read_bytes())
            result = subprocess.run([sys.executable, str(bundle / 'apply.py'),
                '--project', str(project), '--use', 'object-card', '--start', '0'],
                env=dict(os.environ, EDITONNY_MEDIA_DIR=str(root / 'absent')), capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('media import', result.stderr)
            self.assertNotIn('Traceback', result.stderr)
            self.assertEqual(config.read_text(), '{}')
            self.assertEqual(list(project.iterdir()), [config])

    def test_scene_without_arguments_does_not_create_an_installation(self):
        with tempfile.TemporaryDirectory(prefix='editonny motion entry ') as tmp:
            home = Path(tmp)
            bundle = home / 'bundle'
            bundle.mkdir()
            for name in ('apply.py', 'resources.py', 'media.py', 'numbers.json'):
                (bundle / name).write_bytes((VISUALS / name).read_bytes())
            result = subprocess.run([sys.executable, str(bundle / 'apply.py')],
                                    env=dict(os.environ, HOME=str(home)),
                                    capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('--project', result.stderr)
            self.assertFalse((home / '.local/share/editonny').exists())

    def test_scene_copy_names_preserve_edits_and_gaps(self):
        for option, name, prefix, suffix in (
                ('--transition', 'glow', 'transition', 'glow'),
                ('--pointer', 'down-arrow', 'pointer', 'down-arrow')):
            with self.subTest(option=option), tempfile.TemporaryDirectory() as tmp:
                project = Path(tmp)
                scenes = project / 'compositions/scenes'
                scenes.mkdir(parents=True)
                (project / 'project.json').write_text('{}')
                # A gap must not make the next copy collide with a later edit.
                for number in (1, 3):
                    (scenes / f'{prefix}-{number:02d}-{suffix}.html').write_text('PERSONAL EDIT')
                extra = ['--cuts', '1,2'] if option == '--transition' else ['--count', '2']
                result = subprocess.run(
                    [sys.executable, str(VISUALS / 'apply.py'), '--project',
                     str(project), option, name, *extra], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                # Pasted rows need sceneId for sounds and layer order.
                rows = json.loads(result.stdout[result.stdout.index('['):])
                self.assertEqual({row['sceneId'] for row in rows}, {f'{prefix}-{suffix}'})
                for number in (1, 3):
                    self.assertEqual((scenes / f'{prefix}-{number:02d}-{suffix}.html').read_text(),
                                     'PERSONAL EDIT')
                self.assertEqual(len(list(scenes.glob('*.html'))), 4)

    def test_catalog_scenes_are_bundled(self):
        scenes = json.loads((VISUALS / 'catalog.json').read_text())['scenes']
        self.assertGreater(len(scenes), 100)
        self.assertEqual(len({c['id'] for c in scenes}), len(scenes))
        for scene in scenes:
            with self.subTest(scene=scene['id']):
                self.assertTrue((VISUALS / 'scenes' / (scene['id'] + '.html')).is_file())

    def test_scene_uses_its_bundle_in_an_isolated_home(self):
        with tempfile.TemporaryDirectory(prefix='editonny engine ') as tmp:
            home = Path(tmp)
            project = home / 'project with spaces'
            result = subprocess.run(
                [sys.executable, str(ENGINE / 'project-template/build.py'),
                 'new', str(project)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            cfg = json.loads((project / 'project.json').read_text())
            self.assertEqual(Path(cfg['skill']), ENGINE)
            result = subprocess.run(
                [sys.executable, str(VISUALS / 'apply.py'), '--project',
                 str(project), '--transition', 'glow', '--cuts', '1'],
                env=dict(os.environ, HOME=str(home)), capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((project / 'compositions/scenes/transition-01-glow.html').is_file())
            result = subprocess.run(
                [sys.executable, str(VISUALS / 'apply.py'), '--project',
                 str(project), '--use', 'transition-glow', '--cut', '1'],
                env=dict(os.environ, HOME=str(home)), capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            result = subprocess.run(
                [sys.executable, str(VISUALS / 'apply.py'), '--project',
                 str(project), '--use', 'object-card', '--cut', '1'],
                env=dict(os.environ, HOME=str(home)), capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)   # Centered on 1 s, a 4.5 s scene would start before the video.
            self.assertIn('before the video', result.stderr)
            cfg = json.loads((project / 'project.json').read_text())
            self.assertEqual(len(cfg['scenes']), 1)

    def test_command_reference_lists_every_helper(self):
        text = (ENGINE / 'references/commands.md').read_text()
        commands = set(re.findall(r'^- `([a-z_]+\.py)`$', text, re.M))
        library = set(re.findall(r'`([a-z_]+\.py)`', text.split('These modules have no command line')[1]))
        for path in (ENGINE / 'helpers').glob('*.py'):
            runnable = re.search(r"__name__ == ['\"]__main__['\"]", path.read_text()) is not None
            with self.subTest(helper=path.name):
                self.assertIn(path.name, commands if runnable else library)

    def test_cut_copy_replaces_a_dangling_link(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location('copy_build', ENGINE / 'project-template/build.py')
        build = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(build)
        with tempfile.TemporaryDirectory(prefix='editonny copy ') as tmp:
            src, dst = Path(tmp) / 'cut.mp4', Path(tmp) / 'assets/cut.mp4'
            src.write_bytes(b'cut')
            dst.parent.mkdir()
            dst.symlink_to(Path(tmp) / 'missing.mp4')
            build.copy_video(str(src), str(dst))
            self.assertFalse(dst.is_symlink())
            self.assertEqual(dst.read_bytes(), b'cut')

    @unittest.skipUnless(shutil.which('ffmpeg'), 'FFmpeg is required')
    def test_resized_images_keep_their_folder_identity(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location('resize_build', ENGINE / 'project-template/build.py')
        build = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(build)
        with tempfile.TemporaryDirectory(prefix='editonny resize ') as tmp, mock.patch.object(build, 'HERE', tmp):
            for folder, color in (('before', 'red'), ('after', 'blue')):
                Path(tmp, 'assets', folder).mkdir(parents=True)
                subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', f'color=c={color}:s=400x400',
                                '-frames:v', '1', str(Path(tmp, 'assets', folder, 'photo.png'))], check=True)
            first = build.resize_image('assets/before/photo.png', 100, 100)
            second = build.resize_image('assets/after/photo.png', 100, 100)
            self.assertNotEqual(first, second)
            self.assertNotEqual(Path(tmp, first).read_bytes(), Path(tmp, second).read_bytes())

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg is required')
    def test_preserved_audio_keeps_the_rendered_ending(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location('audio_build', ENGINE / 'project-template/build.py')
        build = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(build)
        with tempfile.TemporaryDirectory(prefix='editonny preserve ') as tmp:
            render, cut = Path(tmp, 'render.mp4'), Path(tmp, 'cut.mp4')
            # A 10 s cut rendered with a 2 s endHold: the render is 12 s long.
            for path, seconds in ((render, 12), (cut, 10)):
                subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', f'color=c=gray:s=64x64:r=24:d={seconds}',
                                '-f', 'lavfi', '-i', f'sine=frequency=440:duration={seconds}', '-c:v', 'libx264',
                                '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-shortest', str(path)], check=True)
            build.preserve_audio(render, cut)
            probe = json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-show_streams', '-show_format',
                                                        '-of', 'json', str(render)]))
            video = next(s for s in probe['streams'] if s['codec_type'] == 'video')
            audio = next(s for s in probe['streams'] if s['codec_type'] == 'audio')
            self.assertGreater(float(video['duration']), 11.9)
            self.assertLess(float(audio['duration']), 10.1)

    def test_project_config_points_at_hyperframes_hosts(self):
        config = json.loads((ENGINE / 'project-template/hyperframes.json').read_text())
        self.assertEqual(config['$schema'], 'https://hyperframes.heygen.com/schema/hyperframes.json')
        self.assertEqual(config['registry'], 'https://raw.githubusercontent.com/heygen-com/hyperframes/main/registry')

    def test_scene_copies_keep_runtime_built_selectors(self):
        sys.path.insert(0, str(VISUALS))
        import apply
        outside = lambda html: set(re.findall(r'(?<![\w-])id="([^"]+)"', re.sub(r'<script\b.*?</script>', '', html, flags=re.S))) - {'root'}
        for scene in apply.catalog():
            source = (VISUALS / 'scenes' / (scene + '.html')).read_text()
            copies = []
            for new in (scene + '-01', scene + '-02'):
                copies.append(apply.replace_id(source, scene, new))
                with self.subTest(copy=new):
                    self.assertEqual(apply.check_ids(copies[-1], new, '_' + re.sub(r'[^a-z0-9]', '', new)), [])
            self.assertFalse(outside(copies[0]) & outside(copies[1]), scene)
        copy = apply.replace_id((VISUALS / 'scenes/three-steps.html').read_text(), 'three-steps', 'three-steps-01')
        self.assertIn('id="c3_threesteps01-p1"', copy)
        self.assertIn('T.fromTo("#c3_threesteps01-p" + n,', copy)
        # A family renamed at the end of the ID no longer matches "#c3-p" + n.
        broken = '<div id="c3-p1_threesteps01"></div><script>T.fromTo("#c3-p" + n, {}, {});</script>'
        self.assertEqual(apply.check_ids(broken, 'three-steps-01', '_threesteps01'), ['c3-p...'])

    def test_fixed_scene_holds_through_the_final_frame(self):
        sys.path.insert(0, str(VISUALS))
        import apply
        with tempfile.TemporaryDirectory() as tmp:
            project, edit = Path(tmp) / 'project', Path(tmp) / 'edit'
            project.mkdir(); edit.mkdir()
            (project / 'project.json').write_text('{}')
            (edit / 'cut_timeline.json').write_text('{"duration": 8.733}')
            with mock.patch.object(apply, 'copy_assets', return_value=0), contextlib.redirect_stdout(io.StringIO()), warnings.catch_warnings():
                warnings.simplefilter('ignore', ResourceWarning)
                apply.use_scene(str(project), 'three-steps', 3.5, 8.733)
                apply.use_scene(str(project), 'three-steps', 0, 6)
            held, plain = ((project / f'compositions/scenes/three-steps-{n}.html').read_text() for n in ('01', '02'))
        # The exit fades start 0.36 s before the 6 s source end (0.46 s in step-by-step); the hold drops them only while the scene reaches the end.
        self.assertIn('if (typeof C.end === "number" && C.end >= 8.733 - .3) tl.getChildren(false, true, false).forEach(function (t) { if (t.startTime() >= 5.5 && t.vars.opacity === 0) t.kill(); });', held)
        self.assertIn('tl.tweenFromTo(0, 6.0, { duration: 5.233,', held)
        self.assertNotIn('t.kill()', plain)
        self.assertIn('window.__timelines["three-steps-02"] = tl;', plain)


class ContentHashTests(unittest.TestCase):
    def hash_with_open_ctime(self, os_name):
        sys.path.insert(0, str(ENGINE / 'helpers'))
        import common
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'clip.mp4'
            path.write_bytes(b'video')
            real = os.stat(path)
            # Windows path stat reports creation time as st_ctime; fstat reports metadata change time.
            opened = mock.Mock(st_dev=real.st_dev, st_ino=real.st_ino, st_size=real.st_size,
                               st_mtime_ns=real.st_mtime_ns, st_ctime_ns=real.st_ctime_ns + 1)
            with mock.patch.object(common.os, 'name', os_name), mock.patch.object(common.os, 'fstat', return_value=opened):
                return common.content_hash(path)

    def test_windows_ctime_semantics_do_not_reject_an_unchanged_file(self):
        self.assertEqual(len(self.hash_with_open_ctime('nt')), 64)

    def test_ctime_change_still_rejects_elsewhere(self):
        with self.assertRaisesRegex(ValueError, 'changed while hashing'):
            self.hash_with_open_ctime('posix')


if __name__ == '__main__':
    unittest.main()
