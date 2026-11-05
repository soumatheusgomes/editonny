import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from test_visual_media import fixture, media_module
from workflow_fixtures import make_link

ROOT = Path(__file__).resolve().parents[1]
VISUALS = ROOT / 'skills/editonny/visuals'


def resources():
    spec = importlib.util.spec_from_file_location('visual_resources', VISUALS / 'resources.py')
    module = importlib.util.module_from_spec(spec)
    with patch.object(sys, 'path', [str(VISUALS), *sys.path]):
        spec.loader.exec_module(module)
    return module


class VisualResourceTests(unittest.TestCase):
    def test_older_personal_assets_and_scenes_are_preserved(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source, _ = fixture(root)
            project = root / 'project'
            module = resources()
            for relative, destination in (
                ('assets/visuals/image.png', 'assets/visuals/image.png'),
                ('scenes/custom.html', 'compositions/scenes/visuals/custom.html'),
            ):
                bundled = source / relative
                bundled.parent.mkdir(parents=True, exist_ok=True)
                bundled.write_bytes(b'new package resource')
                personal = project / destination
                personal.parent.mkdir(parents=True, exist_ok=True)
                personal.write_bytes(b'older personal customization')
                os.utime(personal, (1, 1))
            self.assertEqual(module.copy_assets(source, project, '<div>CSS-only scene</div>'), 0)
            self.assertEqual(module.copy_scenes(source, project), 0)
            for relative in ('assets/visuals/image.png', 'compositions/scenes/visuals/custom.html'):
                self.assertEqual((project / relative).read_bytes(), b'older personal customization')
                self.assertEqual((project / relative).stat().st_mtime, 1)

    def test_interrupted_nonvideo_copy_leaves_no_partial_destination(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source, _ = fixture(root)
            (source / 'assets/visuals/image.png').write_bytes(b'complete image')
            project = root / 'project'
            module = resources()

            def interrupted_copy(_source, destination, **_kwargs):
                Path(destination).write_bytes(b'partial')
                raise OSError('Disk full')

            with patch('shutil.copyfile', side_effect=interrupted_copy):
                with self.assertRaises(OSError):
                    module.copy_assets(source, project)
            self.assertEqual([p for p in project.rglob('*') if p.is_file()], [])
            self.assertEqual(module.copy_assets(source, project), 1)
            self.assertEqual((project / 'assets/visuals/image.png').read_bytes(), b'complete image')

    def test_invalid_nonvideo_destinations_fail_before_any_copy(self):
        for invalid in ('directory', 'parent-file', 'dangling-symlink'):
            with self.subTest(invalid=invalid), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                source, _ = fixture(root)
                for name in ('first.png', 'last.png'):
                    (source / 'assets/visuals' / name).write_bytes(b'image')
                project = root / 'project'
                target = project / 'assets/visuals/last.png'
                if invalid == 'parent-file':
                    project.mkdir()
                    (project / 'assets').write_bytes(b'personal file')
                else:
                    target.parent.mkdir(parents=True)
                    if invalid == 'directory':
                        target.mkdir()
                    else:
                        make_link(self, target, root / 'missing')
                with self.assertRaises(ValueError):
                    resources().copy_assets(source, project)
                self.assertFalse((project / 'assets/visuals/first.png').exists())

    def test_interrupted_video_copy_can_retry_without_accepting_partial_file(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source, items = fixture(root)
            project = root / 'project'
            target = project / items[0]['destination']
            module = resources()
            text = '<video src="assets/visuals/video/one.webm">'
            def interrupted_copy(_source, destination, **_kwargs):
                Path(destination).write_bytes(b'partial')
                raise OSError('Disk full')
            with patch('shutil.copyfile', side_effect=interrupted_copy):
                with self.assertRaises(OSError):
                    module.copy_assets(source, project, text)
            self.assertFalse(target.exists())
            self.assertEqual(list(project.rglob('*.*')), [])
            self.assertEqual(module.copy_assets(source, project, text), 1)
            self.assertEqual(target.read_bytes(), b'original one')

    def test_only_selected_video_is_copied_and_personal_files_are_preserved(self):
        with tempfile.TemporaryDirectory(prefix='motion assets ') as temp:
            root = Path(temp)
            source, items = fixture(root)
            project = root / 'project with spaces'
            asset = source / 'assets/visuals/font.ttf'
            asset.write_bytes(b'font')
            target = project / 'assets/visuals/font.ttf'
            target.parent.mkdir(parents=True)
            target.write_bytes(b'newer personal font')
            os.utime(target, (asset.stat().st_mtime + 10, asset.stat().st_mtime + 10))
            text = '<video src="assets/visuals/video/one.webm"></video>'
            self.assertEqual(resources().copy_assets(source, project, text), 1)
            video = project / items[0]['destination']
            self.assertEqual(video.read_bytes(), b'original one')
            self.assertFalse((project / items[1]['destination']).exists())
            self.assertEqual(target.read_bytes(), b'newer personal font')
            video.write_bytes(b'personal video')
            os.utime(video, (1, 1))
            (source / items[0]['file']).unlink()
            self.assertEqual(resources().copy_assets(source, project, text), 0)
            self.assertEqual(video.read_bytes(), b'personal video')

    def test_code_scene_copies_no_videos(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source, _ = fixture(root)
            (source / 'assets/visuals/image.png').write_bytes(b'image')
            resources().copy_assets(source, root / 'project', '<div>CSS animation</div>')
            self.assertEqual([p.name for p in (root / 'project').rglob('*') if p.is_file()], ['image.png'])

    def test_missing_corrupt_unknown_media_fail_before_any_copy(self):
        for case in ('missing', 'corrupt', 'unknown'):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                source, items = fixture(root)
                (source / 'assets/visuals/image.png').write_bytes(b'image')
                if case == 'missing':
                    (source / items[0]['file']).unlink()
                if case == 'corrupt':
                    (source / items[0]['file']).write_bytes(b'corrupt')
                name = 'unknown' if case == 'unknown' else 'one'
                with self.assertRaises(ValueError):
                    resources().copy_assets(source, root / 'project', f'<video src="assets/visuals/video/{name}.webm">')
                self.assertFalse((root / 'project').exists())

    def test_symlink_source_and_destination_cannot_escape(self):
        for side in ('source', 'destination'):
            with self.subTest(side=side), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                source, project = root / 'source', root / 'project'
                (source / 'assets/visuals').mkdir(parents=True)
                project.mkdir()
                outside = root / 'outside'
                outside.mkdir()
                if side == 'source':
                    (outside / 'file').write_bytes(b'private')
                    make_link(self, (source / 'assets/visuals/file'), outside / 'file')
                else:
                    (source / 'assets/visuals/file').write_bytes(b'asset')
                    make_link(self, (project / 'assets'), outside, directory=True)
                with self.assertRaises(ValueError):
                    resources().copy_assets(source, project)
                self.assertFalse((outside / 'visuals/file').exists())

    def test_catalog_video_references_have_unambiguous_manifest_entries(self):
        import re
        mapping = {i['destination']: i for i in media_module().manifest(VISUALS) if 'destination' in i}
        for path in (VISUALS / 'scenes').glob('*.html'):
            refs = re.findall(r'assets/visuals/[^\s"\'`<>]+\.(?:webm|mp4)', path.read_text(encoding='utf-8'))
            for ref in refs:
                self.assertIn(ref, mapping, path.name)


if __name__ == '__main__':
    unittest.main()
