"""The on-screen text cache must follow the transcript and backend it was measured with."""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

HELPERS = Path(__file__).resolve().parents[2] / 'skills/editonny/engine/helpers'
sys.path.insert(0, str(HELPERS))
import screen_text


class ScreenTextCacheTests(unittest.TestCase):
    def test_transcript_or_backend_change_measures_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video, transcript = root / 'cut.mp4', root / 'words.json'
            video.write_bytes(b'video')
            fresh = lambda *a, **k: {'video_sig': screen_text.file_signature(video), 'version': screen_text.VERSION,
                                     'fps': 2.0, 'summary': 'measured'}
            def run(backend='heuristic'):
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(screen_text.main(['--video', str(video), '--edit-dir', str(root), '--transcript',
                                                       str(transcript), '--backend', backend]), 0)
            with mock.patch.object(screen_text, 'map_value', side_effect=fresh) as measure:
                transcript.write_text(json.dumps({'words': [{'text': 'one', 'start': 0.1}]}), encoding='utf-8')
                run()
                run()
                self.assertEqual(measure.call_count, 1)
                transcript.write_text(json.dumps({'words': [{'text': 'two', 'start': 0.1}]}), encoding='utf-8')
                run()
                self.assertEqual(measure.call_count, 2)
                run('vision')
                self.assertEqual(measure.call_count, 3)


if __name__ == '__main__':
    unittest.main()
