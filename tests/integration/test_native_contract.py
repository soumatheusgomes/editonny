import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'skills/editonny/engine/helpers'), str(ROOT / 'tests')]
from intake import inspect_sources, propose_settings
from workflow_fixtures import confirmation
from workspace import create_workspace
from media_fixtures import make_patches


class NativeContractTests(unittest.TestCase):
    def test_source_geometry_is_not_forced_to_vertical_template(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); source = root / 'source.mp4'; make_patches(source)
            inspection = inspect_sources([source], False)
            config = propose_settings(inspection, {'captions': False, 'delivery': {'workspaceRoot': str(root / 'downloads')}})['config']
            project = create_workspace(root / 'downloads', 'Native', inspection, config, confirmation(config, inspection))
            edit = project.parent / 'edit'
            (edit / 'cut.mp4').write_bytes(source.read_bytes())
            (edit / 'cut_timeline.json').write_text(json.dumps({'fps': 24, 'fps_str': '24', 'duration': 1 / 24,
                'width': 128, 'height': 64, 'segments': [{'out_start': 0}]}), encoding='utf-8')
            (edit / 'transcripts/cut.json').write_text(json.dumps({'words': [{'text': 'Hello', 'start': 0, 'end': 0.04}]}), encoding='utf-8')
            spec = importlib.util.spec_from_file_location('native_contract_build', project / 'build.py')
            module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
            p = module.Project()
            self.assertEqual((p.W, p.H), (128, 64))
            self.assertEqual(p.fps, 24)


if __name__ == '__main__':
    unittest.main()
