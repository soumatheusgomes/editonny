import json
from pathlib import Path
import sys
import tempfile
import unittest

HELPERS = Path(__file__).resolve().parents[2] / 'skills/editonny/engine/helpers'
sys.path.insert(0, str(HELPERS))
import transcribe


class TranscribeCacheTests(unittest.TestCase):
    def test_same_size_different_sources_are_not_equal(self):
        with tempfile.TemporaryDirectory() as tmp:
            a, b = Path(tmp) / 'a.mp4', Path(tmp) / 'b.mp4'
            a.write_bytes(b'aaaa'); b.write_bytes(b'bbbb')
            self.assertFalse(transcribe.same_video(a, b))

    def test_forced_language_cache_does_not_satisfy_auto(self):
        with tempfile.TemporaryDirectory() as tmp:
            video, cached = Path(tmp) / 'a.mp4', Path(tmp) / 'a.json'
            video.write_bytes(b'aaaa')
            from common import file_signature
            cached.write_text(json.dumps({'source_sig': file_signature(video), 'engine': transcribe.engine_label('mlx', 'model'),
                                         'language': 'en', 'requested_language': 'en', 'prompt': None}), encoding='utf-8')
            self.assertFalse(transcribe.is_cached(cached, video, 'mlx', 'model', None, None))

    def test_apple_silicon_without_mlx_uses_faster_whisper(self):
        from unittest import mock
        with mock.patch.object(transcribe.sys, 'platform', 'darwin'), \
             mock.patch.object(transcribe.platform, 'machine', return_value='arm64'), \
             mock.patch.object(transcribe.importlib.util, 'find_spec', return_value=None):
            self.assertEqual(transcribe.default_engine(), 'faster-whisper')

    def test_no_prompt_reaches_whisper_as_none(self):
        # Whisper encodes "" as a one-space prompt and decodes every window with NaN log-probabilities.
        self.assertIsNone(transcribe.build_prompt(None, True))
        self.assertIsNone(transcribe.build_prompt('  ', True))
        self.assertIsNone(transcribe.build_prompt(None, False))
        self.assertEqual(transcribe.build_prompt(' Kiwify ', True), 'Kiwify')


if __name__ == '__main__':
    unittest.main()
