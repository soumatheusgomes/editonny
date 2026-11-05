import importlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

HELPERS = Path(__file__).resolve().parents[1] / 'skills/editonny/engine/helpers'
sys.path.insert(0, str(HELPERS))
from workflow_fixtures import config, confirmation


class WorkspaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        assert (HELPERS / 'workspace.py').exists(), 'Workspace ownership is not implemented'
        cls.w = importlib.import_module('workspace')

    def prepare(self, root):
        source = root / 'original.mp4'
        source.write_bytes(b'untouched footage')
        from common import content_hash
        return {'sources': [{'id': 'source', 'path': str(source), 'sha256': content_hash(source), 'streams': [0]}]}

    def test_init_without_confirmation_writes_nothing(self):
        with tempfile.TemporaryDirectory() as temp:
            parent = Path(temp) / 'downloads'
            inspection = self.prepare(Path(temp))
            with self.assertRaises(ValueError):
                self.w.create_workspace(parent, 'Interview', inspection, config(parent), {})
            self.assertFalse(parent.exists())

    def test_collision_and_revision_preserve_parent_data(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            inspection = self.prepare(root)
            c = config(root / 'downloads')
            approval = confirmation(c, inspection)
            project = self.w.create_workspace(root / 'downloads', 'Interview', inspection, c, approval)
            self.assertEqual(project.parts[-3:], ('project', 'v001', 'project'))
            self.assertEqual(sorted(p.name for p in project.parents[2].iterdir()), ['exports', 'project'])
            second = self.w.create_workspace(root / 'downloads', 'Interview', inspection, c, approval)
            self.assertNotEqual(project, second)
            cut = project.parent / 'edit/cut.mp4'
            cut.write_bytes(b'approved cut')
            asset = project / 'assets/deliveries/photo.png'
            asset.parent.mkdir(parents=True); asset.write_bytes(b'composition asset')
            revised = self.w.revise_workspace(project, {'captions': {'enabled': False}}, 'captions')
            self.assertEqual((revised / 'assets/deliveries/photo.png').read_bytes(), b'composition asset')
            (revised.parent / 'edit/cut.mp4').write_bytes(b'revised cut')
            self.assertEqual(cut.read_bytes(), b'approved cut')
            self.assertEqual(self.w.read_status(revised)['confirmation'], 'required')
            from project_contract import load_contract, settings_hash
            delta = confirmation(load_contract(revised), inspection)
            delta.update(kind='delta', parentSettingsHash=settings_hash(c))
            self.w.record_confirmation(revised, delta)
            self.assertEqual(self.w.read_status(revised)['confirmation'], 'confirmed')

    def test_failed_candidate_is_not_published(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            inspection = self.prepare(root)
            project = self.w.create_workspace(root / 'downloads', 'Interview', inspection, config(root / 'downloads'), confirmation(config(root / 'downloads'), inspection))
            candidate = project.parent / 'edit/final.mp4'
            candidate.write_bytes(b'candidate')
            with self.assertRaises(ValueError):
                self.w.publish_export(project, candidate, {'status': 'failed'})
            self.assertEqual(list((project.parents[2] / 'exports').iterdir()), [])

    def test_old_summary_cannot_confirm_new_hash(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            inspection = self.prepare(root)
            old = config(root / 'downloads')
            project = self.w.create_workspace(root / 'downloads', 'Interview', inspection, old, confirmation(old, inspection))
            revised = self.w.revise_workspace(project, {'captions': {'enabled': False}}, 'captions')
            with self.assertRaises(ValueError):
                self.w.record_confirmation(revised, confirmation(old, inspection))

    def test_unchanged_decisions_and_preapproved_variant_inherit_acceptance(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); inspection = self.prepare(root)
            c = config(root / 'downloads')
            c['workflow']['delivery']['variants'] = [{'name': 'archive', 'encoding': {'videoCodec': 'hevc'}}]
            project = self.w.create_workspace(root / 'downloads', 'Variants', inspection, c, confirmation(c, inspection))
            unchanged = self.w.revise_workspace(project, {}, 'full')
            self.assertEqual(self.w.read_status(unchanged)['confirmation'], 'confirmed')
            variant = self.w.revise_workspace(project, {}, 'full', 'archive')
            self.assertEqual(self.w.read_status(variant)['confirmation'], 'confirmed')
            from project_contract import read_json
            path = variant / 'project.json'; changed = read_json(path)
            changed['workflow']['audio']['denoise'] = 'strong'
            path.write_text(json.dumps(changed), encoding='utf-8')
            self.assertEqual(self.w.read_status(variant)['confirmation'], 'required')

    def test_recovery_does_not_remove_a_live_lock(self):
        import os
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); inspection = self.prepare(root); c = config(root / 'downloads')
            project = self.w.create_workspace(root / 'downloads', 'Lock', inspection, c, confirmation(c, inspection))
            lock = project.parent / 'edit/.workflow.lock'
            lock.write_text(json.dumps({'pid': os.getpid(), 'token': 'owned'}), encoding='utf-8')
            with self.assertRaises(ValueError):
                self.w.recover_revision(project)
            self.assertTrue(lock.exists())

    def test_non_posix_recovery_never_sends_a_signal(self):
        from types import SimpleNamespace
        from unittest.mock import Mock, patch
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); inspection = self.prepare(root); c = config(root / 'downloads')
            project = self.w.create_workspace(root / 'downloads', 'Lock', inspection, c, confirmation(c, inspection))
            lock = project.parent / 'edit/.workflow.lock'
            lock.write_text(json.dumps({'pid': 123, 'token': 'owned'}), encoding='utf-8')
            kill = Mock()
            with patch.object(self.w, 'os', SimpleNamespace(name='nt', kill=kill)):
                with self.assertRaisesRegex(ValueError, 'POSIX|Windows'):
                    self.w.recover_revision(project)
            kill.assert_not_called()
            self.assertTrue(lock.exists())

    def test_destination_cannot_change_after_summary_acceptance(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); inspection = self.prepare(root); c = config(root / 'downloads')
            c['workflow']['delivery'].update(workspaceRoot=str(root / 'approved'), executionContext='local')
            with self.assertRaisesRegex(ValueError, 'destination|root'):
                self.w.create_workspace(root / 'different', 'Wrong root', inspection, c, confirmation(c, inspection))
            self.assertFalse((root / 'different').exists())

    def test_composition_asset_or_instructions_change_invalidates_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); inspection = self.prepare(root); c = config(root / 'downloads')
            project = self.w.create_workspace(root / 'downloads', 'Assets', inspection, c, confirmation(c, inspection))
            before = self.w.render_dependencies(project)
            (project / 'assets/logo.txt').write_text('changed scene asset', encoding='utf-8')
            (project.parent / 'edit/edit_data.json').write_text('{"hook": {"text": "new hook"}}', encoding='utf-8')
            self.assertNotEqual(before, self.w.render_dependencies(project))


if __name__ == '__main__':
    unittest.main()
