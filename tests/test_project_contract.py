import copy
import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

HELPERS = Path(__file__).resolve().parents[1] / 'skills/editonny/engine/helpers'
sys.path.insert(0, str(HELPERS))
from workflow_fixtures import config, confirmation
from workflow_fixtures import make_link


class ProjectContractTests(unittest.TestCase):
    def test_edl_cannot_use_an_observed_source_removed_from_the_selection(self):
        manifest = {'sources': [{'id': 'source', 'path': '/selected.mp4'},
                                {'id': 'removed', 'path': '/removed.mp4'}]}
        value = config()
        allowed = {'sources': {'s': '/selected.mp4'}, 'ranges': [{'source': 's', 'start': 0, 'end': 1}]}
        self.c.validate_edl_scope(value, manifest, allowed)
        removed = {'sources': {'s': '/removed.mp4'}, 'ranges': [{'source': 's', 'start': 0, 'end': 1}]}
        with self.assertRaisesRegex(ValueError, 'confirmed selection'):
            self.c.validate_edl_scope(value, manifest, removed)

    def test_unsupported_audio_track_selection_cannot_be_confirmed(self):
        value = config()
        value['workflow']['audio']['tracks'] = [1]
        with self.assertRaisesRegex(ValueError, 'track selection.*not implemented'):
            self.c.validate_contract(value)

    @classmethod
    def setUpClass(cls):
        assert (HELPERS / 'project_contract.py').is_file(), 'Confirmed workflow contract is not implemented'
        cls.c = importlib.import_module('project_contract')

    def test_exact_ratio_and_derived_format(self):
        original = config()
        self.assertEqual(self.c.validate_contract(original)['workflow']['video']['frameRate'], '30000/1001')
        self.assertEqual(original, config(), 'Validation must not mutate input')

    def test_invalid_ratio_nan_and_conflicting_format_fail(self):
        for changes in ({'frameRate': 'nan'}, {'width': 1079}, {'height': 0}, {'frameRate': '0/1'}):
            value = config()
            value['workflow']['video'].update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.c.validate_contract(value)
        value = config()
        value['format'] = '16:9'
        with self.assertRaises(ValueError):
            self.c.validate_contract(value)

    def test_preserve_has_no_creative_grade(self):
        value = config()
        value['workflow']['color']['grade'] = 'natural'
        with self.assertRaises(ValueError):
            self.c.validate_contract(value)

    def test_translated_language_cannot_label_original_words(self):
        value = config()
        value['workflow']['language']['captions'] = 'en-US'
        with self.assertRaisesRegex(ValueError, 'translated'):
            self.c.validate_caption_language(value, {'language': 'pt'})
        self.c.validate_caption_language(value, {'language': 'en'})
        value['workflow']['language']['translation'] = 'en'
        with self.assertRaisesRegex(ValueError, 'not implemented'):
            self.c.validate_contract(value)

    def test_output_cannot_replace_source_via_symlink_or_hardlink(self):
        from common import ensure_distinct_output
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'original.mp4'; source.write_bytes(b'original')
            for kind in ('symlink', 'hardlink'):
                with self.subTest(kind=kind):
                    alias = Path(tmp) / f'{kind}.mp4'
                    if kind == 'symlink':
                        make_link(self, alias, source)
                    else:
                        os.link(source, alias)
                    with self.assertRaises(ValueError):
                        ensure_distinct_output(alias, [source])
                    self.assertEqual(source.read_bytes(), b'original')

    def test_sample_and_scene_timing_change_artifact_not_consent(self):
        value = config()
        initial = self.c.settings_hash(value)
        value['workflow']['review']['sampleIntervals'] = [[1, 10]]
        value['scenes'] = [{'file': 'scene.html', 'start': 1}]
        self.assertEqual(initial, self.c.settings_hash(value))
        self.assertNotEqual(self.c.artifact_dependencies(config(), {}, {}, {}),
                            self.c.artifact_dependencies(value, {}, {}, {}))
        value['workflow']['visuals']['cameraMotion'] = True
        self.assertNotEqual(initial, self.c.settings_hash(value))

    def test_analysis_notes_do_not_change_source_identity(self):
        manifest = {'sources': [{'id': 'a', 'sha256': 'abc', 'path': '/original', 'streams': [0]}]}
        changed = copy.deepcopy(manifest)
        changed['sources'][0]['languageConfidence'] = 0.5
        self.assertEqual(self.c.source_identity_hash(manifest), self.c.source_identity_hash(changed))
        changed['sources'][0]['sha256'] = 'different'
        self.assertNotEqual(self.c.source_identity_hash(manifest), self.c.source_identity_hash(changed))

    def test_initial_prompt_cannot_confirm_its_own_summary(self):
        value, manifest = config(), {'sources': [{'id': 'source', 'sha256': 'abc'}]}
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / 'project'
            edit = Path(tmp) / 'edit'
            project.mkdir(); edit.mkdir()
            (project / 'project.json').write_text(json.dumps(value), encoding='utf-8')
            (edit / 'source_manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
            with self.assertRaises(ValueError):
                self.c.require_confirmation(project, self.c.source_identity_hash(manifest))
            approval = confirmation(value, manifest)
            approval['responseMessageId'] = approval['summaryMessageId']
            (edit / 'state.json').write_text(json.dumps({'settingsConfirmation': approval}), encoding='utf-8')
            with self.assertRaises(ValueError):
                self.c.require_confirmation(project, self.c.source_identity_hash(manifest))
            approval = confirmation(value, manifest)
            (edit / 'state.json').write_text(json.dumps({'settingsConfirmation': approval}), encoding='utf-8')
            self.c.require_confirmation(project, self.c.source_identity_hash(manifest))
            value['captions']['enabled'] = False
            (project / 'project.json').write_text(json.dumps(value), encoding='utf-8')
            with self.assertRaises(ValueError):
                self.c.require_confirmation(project, self.c.source_identity_hash(manifest))


if __name__ == '__main__':
    unittest.main()
