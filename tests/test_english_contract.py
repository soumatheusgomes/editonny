"""Exercise the public English project and catalog contracts."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class EnglishContractTests(unittest.TestCase):
    def test_catalog_exposes_english_fields_and_scene_ids(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / 'editonny.py'), 'catalog', '--json'],
            capture_output=True, text=True, check=True,
        encoding='utf-8', errors='replace')
        scenes = json.loads(result.stdout)
        self.assertEqual(len(scenes), 145)
        self.assertTrue(all({'id', 'name', 'category', 'whenToUse'} <= scene.keys() for scene in scenes))
        self.assertTrue(all(not scene['id'].startswith('mp_') for scene in scenes))

    def test_new_project_uses_english_options_and_writes_english_configuration(self):
        with tempfile.TemporaryDirectory(prefix='editonny English project ') as temporary:
            root = Path(temporary)
            edit = root / 'edit'
            edit.mkdir()
            project = root / 'new project'
            result = subprocess.run(
                [sys.executable, str(ROOT / 'editonny.py'), 'new', str(project),
                 '--edit', str(edit), '--style', 'paper'],
                capture_output=True, text=True,
            encoding='utf-8', errors='replace')
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            configuration = json.loads((project / 'project.json').read_text(encoding='utf-8'))
            self.assertEqual(configuration['style'], 'paper')
            self.assertTrue(configuration['captions']['enabled'])
            self.assertEqual(configuration['blocks'][0]['scene'], 'fullscreen')
            self.assertTrue((project / 'compositions' / 'scenes').is_dir())


if __name__ == '__main__':
    unittest.main()
