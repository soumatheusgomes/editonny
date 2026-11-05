import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

HELPERS = Path(__file__).resolve().parents[2] / 'skills/editonny/engine/helpers'
sys.path.insert(0, str(HELPERS))
import verify_cut


class VerificationContractTests(unittest.TestCase):
    def test_findings_fail_strict_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            edl, video = root / 'edl.json', root / 'cut.mp4'
            edl.write_text(json.dumps({'ranges': [], 'sources': {}}), encoding='utf-8'); video.write_bytes(b'fixture')
            report = {'flags': [{'code': 'STALE-TIMELINE', 'time': 0, 'message': 'changed'}]}
            with patch.object(sys, 'argv', ['verify_cut.py', str(edl), str(video), '--without-approval']), \
                 patch.object(verify_cut, 'verify', return_value=(report, [])), \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(verify_cut.main(), 1)

    def test_approve_existing_requires_content_bound_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            edl, video, report = root / 'edl.json', root / 'cut.mp4', root / 'report.json'
            edl.write_text(json.dumps({'ranges': [], 'sources': {}}), encoding='utf-8'); video.write_bytes(b'fixture')
            report.write_text(json.dumps({'video': str(video), 'flags': []}), encoding='utf-8')
            with patch.object(sys, 'argv', ['verify_cut.py', str(edl), str(video), '--approve-existing', '-o', str(report)]), \
                 patch.object(verify_cut, 'update_state', return_value=(None, None)), \
                 contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(verify_cut.main(), 2)


if __name__ == '__main__':
    unittest.main()
