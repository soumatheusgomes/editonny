from pathlib import Path
import json
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'skills/editonny/engine/helpers'), str(ROOT / 'tests')]
from workflow_fixtures import config
import project_contract


class EditingScopeTests(unittest.TestCase):
    def test_default_jcuts_cannot_trim_a_protected_pause(self):
        from intake import inspect_sources, propose_settings
        from workspace import create_workspace
        from workflow_fixtures import confirmation
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); source = root / 'source.mp4'
            subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=gray:s=128x64:r=24:d=10',
                '-vf', 'setparams=range=tv:color_primaries=bt709:color_trc=bt709:colorspace=bt709',
                '-c:v', 'libx264', str(source)], check=True)
            manifest = inspect_sources([source], False)
            for start, end in ((2.95, 3), (5, 5.1)):
                with self.subTest(start=start):
                    c = propose_settings(manifest, {'captions': False, 'delivery': {'workspaceRoot': str(root / 'downloads')}})['config']
                    c['workflow']['editing']['protectedIntervals'] = [
                        {'sourceId': manifest['sources'][0]['id'], 'start': start, 'end': end}]
                    project = create_workspace(root / 'downloads', f'Protected {start}', manifest, c, confirmation(c, manifest))
                    edl = project.parent / 'edit/edl.json'
                    edl.write_text(json.dumps({'sources': {'s': str(source)}, 'contractHash': project_contract.settings_hash(c),
                        'ranges': [{'id': 'a', 'source': 's', 'start': 0, 'end': 3},
                                   {'id': 'b', 'source': 's', 'start': 5, 'end': 7}]}), encoding='utf-8')
                    command = [sys.executable, str(ROOT / 'skills/editonny/engine/helpers/render_cut.py'), str(edl)]
                    result = subprocess.run(command, capture_output=True, text=True, encoding='utf-8', errors='replace')
                    self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertIn('protected', result.stderr)
                    self.assertFalse((edl.parent / 'cut.mp4').exists())
                    result = subprocess.run([*command, '--no-jcut'], capture_output=True, text=True, encoding='utf-8', errors='replace')
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_interval_render_keeps_the_parent_before_and_after_the_requested_window(self):
        from intake import inspect_sources, propose_settings
        from workspace import create_workspace, revise_workspace, record_confirmation
        from workflow_fixtures import confirmation
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); source = root / 'source.mp4'
            subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=s=128x64:r=24:d=10',
                '-vf', 'setparams=range=tv:color_primaries=bt709:color_trc=bt709:colorspace=bt709',
                '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(source)], check=True)
            manifest = inspect_sources([source], False)
            c = propose_settings(manifest, {'captions': False, 'delivery': {'workspaceRoot': str(root / 'downloads')}})['config']
            project = create_workspace(root / 'downloads', 'Scope', manifest, c, confirmation(c, manifest))
            edl = {'sources': {'s': str(source)}, 'contractHash': project_contract.settings_hash(c),
                   'ranges': [{'id': 'a', 'source': 's', 'start': 0, 'end': 10}]}
            (project.parent / 'edit/edl.json').write_text(json.dumps(edl), encoding='utf-8')
            def render(project):
                result = subprocess.run([sys.executable, str(ROOT / 'skills/editonny/engine/helpers/render_cut.py'),
                    str(project.parent / 'edit/edl.json'), '--no-jcut'], capture_output=True, text=True, encoding='utf-8', errors='replace')
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            render(project)
            child = revise_workspace(project, {'workflow': {'editing': {'intervals': [
                {'sourceId': manifest['sources'][0]['id'], 'start': 1, 'end': 2}]}}}, 'intervals')
            changed = project_contract.load_contract(child)
            accepted = confirmation(changed, manifest)
            accepted['parentSettingsHash'] = project_contract.settings_hash(c)
            record_confirmation(child, accepted)
            edl.update(contractHash=project_contract.settings_hash(changed), ranges=[
                {'id': 'a', 'source': 's', 'start': 0, 'end': 1.4},
                {'id': 'b', 'source': 's', 'start': 1.6, 'end': 10}])
            (child.parent / 'edit/edl.json').write_text(json.dumps(edl), encoding='utf-8')
            render(child)
            timeline = json.loads((child.parent / 'edit/cut_timeline.json').read_text(encoding='utf-8'))
            self.assertGreater(timeline['duration'], 9)
            self.assertAlmostEqual(timeline['segments'][0]['src_start'], 0, delta=1 / 24)
            self.assertAlmostEqual(timeline['segments'][-1]['src_end'], 10, delta=1 / 24)
            self.assertEqual(json.loads((project.parent / 'edit/edl.json').read_text(encoding='utf-8'))['ranges'][0]['end'], 10)

    def test_protected_interval_cannot_be_removed_by_edl(self):
        self.assertTrue(hasattr(project_contract, 'validate_edl_scope'), 'EDL does not enforce protected scope')
        c = config()
        c['workflow']['editing']['protectedIntervals'] = [{'sourceId': 'source', 'start': 1, 'end': 2}]
        manifest = {'sources': [{'id': 'source', 'path': '/source.mp4'}]}
        edl = {'sources': {'s': '/source.mp4'}, 'ranges': [{'source': 's', 'start': 0, 'end': 1.5}]}
        with self.assertRaisesRegex(ValueError, 'protected'):
            project_contract.validate_edl_scope(c, manifest, edl)
        edl['ranges'][0]['end'] = 3
        project_contract.validate_edl_scope(c, manifest, edl)

    def test_interval_revision_cannot_expand_accepted_scope(self):
        self.assertTrue(hasattr(project_contract, 'validate_edl_scope'), 'EDL does not enforce interval scope')
        c = config()
        c['workflow']['editing'].update(scope='intervals', intervals=[{'sourceId': 'source', 'start': 1, 'end': 2}])
        manifest = {'sources': [{'id': 'source', 'path': '/source.mp4'}]}
        parent = {'sources': {'s': '/source.mp4'}, 'ranges': [{'source': 's', 'start': 0, 'end': 10}]}
        edl = {'sources': {'s': '/source.mp4'}, 'ranges': [
            {'source': 's', 'start': 0, 'end': 1.4}, {'source': 's', 'start': 1.6, 'end': 10}]}
        project_contract.validate_edl_scope(c, manifest, edl, parent_edl=parent)
        edl['ranges'] = [{'source': 's', 'start': 1, 'end': 1.4}, {'source': 's', 'start': 1.6, 'end': 2}]
        with self.assertRaisesRegex(ValueError, 'scope'):
            project_contract.validate_edl_scope(c, manifest, edl, parent_edl=parent)


if __name__ == '__main__':
    unittest.main()
