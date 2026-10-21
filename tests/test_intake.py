import importlib
import json
from pathlib import Path
import sys
import unittest
import tempfile
from unittest.mock import patch

HELPERS = Path(__file__).resolve().parents[1] / 'skills/editonny/engine/helpers'
sys.path.insert(0, str(HELPERS))


class IntakeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        assert (HELPERS / 'intake.py').exists(), 'Inspection and proposal are not implemented'
        cls.i = importlib.import_module('intake')

    def source(self):
        return {'sources': [{'id': 'source', 'path': '/selected/source.mp4', 'sha256': 'abc', 'facts':
            {'display_width': 1080, 'display_height': 1920, 'fps_str': '30000/1001', 'is_vfr': False,
             'has_video': True, 'has_audio': True, 'duration': 20, 'bit_depth': 8, 'pix_fmt': 'yuv420p',
             'color_transfer': 'bt709', 'color_primaries': 'bt709', 'color_space': 'bt709', 'color_range': 'tv'}}]}

    def test_short_prompt_gets_complete_proposal_without_forcing_speech(self):
        proposal = self.i.propose_settings(self.source(), {'conversation': 'pt-BR'})
        workflow = proposal['config']['workflow']
        self.assertEqual(workflow['language']['transcription'], 'auto')
        self.assertEqual(workflow['language']['captions'], 'source')
        self.assertEqual(workflow['color']['mode'], 'preserve')
        self.assertIsNone(workflow['color']['grade'])
        self.assertEqual(workflow['video']['frameRate'], '30000/1001')
        self.assertTrue(proposal['confirmationRequired'])
        self.assertEqual(len(proposal['summary']), 10)
        self.assertIn('no color treatment', proposal['summary']['image'].lower())

    def test_complete_prompt_still_requires_summary(self):
        proposal = self.i.propose_settings(self.source(), {'captions': False, 'conversation': 'en-US', 'scope': 'captions'})
        self.assertFalse(proposal['config']['captions']['enabled'])
        self.assertEqual(proposal['config']['workflow']['editing']['scope'], 'captions')
        self.assertTrue(proposal['confirmationRequired'])

    def test_hdr_and_visual_requests_do_not_enable_audio_or_color_conversion(self):
        inspection = self.source()
        inspection['sources'][0]['facts'].update(is_hdr=True, bit_depth=10, pix_fmt='yuv420p10le',
            color_transfer='arib-std-b67', color_primaries='bt2020', color_space='bt2020nc')
        workflow = self.i.propose_settings(inspection, {'visuals': {
            'scenes': 'agent', 'transitions': 'agent'}})['config']['workflow']
        self.assertFalse(workflow['audio']['music'])
        self.assertFalse(workflow['audio']['effects'])
        self.assertEqual(workflow['color']['mode'], 'preserve')
        self.assertEqual(workflow['color']['target'], 'source')
        self.assertIsNone(workflow['color']['conversion'])
        self.assertEqual(workflow['encoding']['bitDepth'], 'source')

    def test_explicit_audio_requests_override_only_the_requested_default(self):
        for feature in ('music', 'effects'):
            with self.subTest(feature=feature):
                audio = self.i.propose_settings(self.source(), {'audio': {feature: True}})['config']['workflow']['audio']
                self.assertTrue(audio[feature])
                self.assertFalse(audio['effects' if feature == 'music' else 'music'])

    def test_proposal_attached_to_its_inspection_report_serializes(self):
        # Mirrors `inspect --requested`, which prints the report with its proposal attached.
        report = self.source()
        report['proposal'] = self.i.propose_settings(report, {'conversation': 'pt-BR'})
        printed = json.loads(json.dumps(report))
        self.assertEqual(printed['proposal']['provenance']['observed']['sources'], printed['sources'])

    def test_multiple_audio_tracks_are_disclosed_before_confirmation(self):
        inspection = self.source()
        inspection['sources'][0]['facts']['audio_tracks'] = 2
        with self.assertRaisesRegex(ValueError, 'Multi-track.*not implemented'):
            self.i.propose_settings(inspection, {})

    def test_mixed_source_formats_are_not_silently_selected(self):
        inspection = self.source()
        import copy
        other = copy.deepcopy(inspection['sources'][0])
        other['id'] = 'other'
        other['facts']['display_width'] = 1920
        other['facts']['display_height'] = 1080
        inspection['sources'].append(other)
        with self.assertRaisesRegex(ValueError, 'target'):
            self.i.propose_settings(inspection, {})

    def test_bounded_local_detection_does_not_invent_regional_locale(self):
        self.assertTrue(hasattr(self.i, 'detect_language_sample'), 'Bounded local language detection is missing')
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / 'source.mp4'; source.write_bytes(b'footage')
            facts = self.source()['sources'][0]['facts']
            with patch.object(self.i, 'probe_video', return_value=facts), \
                 patch.object(self.i, 'detect_language_sample', return_value={'status': 'detected', 'language': 'pt', 'sampleSeconds': 20}) as detect:
                result = self.i.inspect_sources([source])
            detect.assert_called_once()
            self.assertEqual(result['sources'][0]['language']['language'], 'pt')
            self.assertEqual(list(Path(temp).iterdir()), [source])


if __name__ == '__main__':
    unittest.main()
