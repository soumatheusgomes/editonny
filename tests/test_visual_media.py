"""Bundled media integrity and recovery without overwriting existing files."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from workflow_fixtures import make_link

ROOT = Path(__file__).resolve().parents[1]
VISUALS = ROOT / 'skills/editonny/visuals'


def media_module():
    spec = importlib.util.spec_from_file_location('visual_media', VISUALS / 'media.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fixture(root):
    source = root / 'source'
    source.mkdir()
    items = []
    for name, data in [('one.webm', b'original one'), ('two.mp4', b'original two')]:
        relative = f'assets/visuals/video/{name}'
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        items.append(dict(file=relative, bytes=len(data), sha256=hashlib.sha256(data).hexdigest(), destination=relative))
    (source / 'media.json').write_text(json.dumps({'version': 1, 'videos': items}), encoding='utf-8')
    return source, items


def empty_bundle(root, source):
    bundle = root / 'bundle'
    bundle.mkdir()
    (bundle / 'media.json').write_bytes((source / 'media.json').read_bytes())
    return bundle


class VisualMediaTests(unittest.TestCase):
    def setUp(self):
        environment = tempfile.TemporaryDirectory(prefix='media isolated home ')
        self.addCleanup(environment.cleanup)
        isolation = patch.dict(os.environ, HOME=environment.name,
                               EDITONNY_MEDIA_DIR=str(Path(environment.name) / 'obsolete library'))
        isolation.start()
        self.addCleanup(isolation.stop)

    def test_import_is_verified_idempotent_and_keeps_originals(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source, items = fixture(root)
            bundle = empty_bundle(root, source)
            media = media_module()
            self.assertEqual(media.import_media(bundle, source), 2)
            self.assertEqual(media.import_media(bundle, source), 0)
            for item in items:
                self.assertEqual((source / item['file']).read_bytes(), (bundle / item['file']).read_bytes())
            self.assertEqual(media.status(bundle)['available'], 2)
            (bundle / items[0]['file']).write_bytes(b'corrupted')
            (bundle / items[1]['file']).unlink()
            report = media.status(bundle)
            self.assertEqual(report['available'], 0)
            self.assertEqual(report['missing'], [items[1]['file']])
            self.assertEqual(report['invalid'], [items[0]['file']])

    def test_preflight_refuses_conflict_or_bad_source_before_copying(self):
        for side in ('source', 'destination'):
            with self.subTest(side=side), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                source, items = fixture(root)
                bundle = empty_bundle(root, source)
                target = (source if side == 'source' else bundle) / items[1]['file']
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b'personal / corrupt')
                with self.assertRaises(ValueError):
                    media_module().import_media(bundle, source)
                self.assertEqual(target.read_bytes(), b'personal / corrupt')
                self.assertFalse((bundle / items[0]['file']).exists())

    def test_rejects_unsafe_paths_symlinks_and_duplicate_destinations(self):
        for case in ('traversal', 'absolute', 'symlink', 'destination_link', 'duplicate'):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                source, items = fixture(root)
                bundle = empty_bundle(root, source)
                if case == 'traversal': items[0]['file'] = '../outside.webm'
                if case == 'absolute': items[0]['file'] = '/outside.webm'
                if case == 'duplicate': items[1]['destination'] = items[0]['destination']
                if case == 'symlink':
                    path = source / items[0]['file']
                    path.unlink()
                    make_link(self, path, source / items[1]['file'])
                if case == 'destination_link':
                    make_link(self, (bundle / 'assets'), source / 'assets', directory=True)
                (bundle / 'media.json').write_text(json.dumps({'version': 1, 'videos': items}), encoding='utf-8')
                with self.assertRaises(ValueError):
                    media_module().import_media(bundle, source)

    def test_cli_reports_incomplete_bundle_without_traceback(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source, _ = fixture(root)
            bundle = empty_bundle(root, source)
            script = bundle / 'media.py'
            script.write_bytes((VISUALS / 'media.py').read_bytes())
            result = subprocess.run([sys.executable, str(script), 'status', '--json'],
                env=dict(os.environ, HOME=temp, EDITONNY_MEDIA_DIR=str(source)), capture_output=True, text=True, encoding='utf-8', errors='replace')
            self.assertEqual(result.returncode, 1, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report['available'], 0)
            self.assertEqual(len(report['missing']), report['total'])
            self.assertEqual(Path(report['root']).resolve(), bundle.resolve())
            self.assertNotIn('Traceback', result.stderr)

    def test_complete_skill_works_without_external_library_configuration(self):
        with tempfile.TemporaryDirectory() as temp:
            result = subprocess.run([sys.executable, str(ROOT / 'editonny.py'), 'media', 'status', '--json'],
                env=dict(os.environ, HOME=temp, EDITONNY_MEDIA_DIR=str(Path(temp) / 'obsolete')), capture_output=True, text=True, encoding='utf-8', errors='replace')
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(Path(report['root']).resolve(), VISUALS.resolve())
            self.assertEqual(report['available'], len(media_module().manifest(VISUALS)))


if __name__ == '__main__':
    unittest.main()
