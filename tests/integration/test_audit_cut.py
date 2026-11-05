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
import audit_cut


class AuditCutTests(unittest.TestCase):
    def audit(self, edit, *extra, breaths=None):
        """Run main() with every helper replaced; breaths.py returns the given --json report."""
        def run(command):
            if command[0].endswith('breaths.py'):
                return (1 if breaths else 0), json.dumps({'thresholdDb': -40, 'min_s': 0.25, 'breaths': breaths or [], 'end_hold': None}), 0
            return 0, '', 0
        out = io.StringIO()
        with mock.patch.object(audit_cut, 'run', side_effect=run) as runner, mock.patch.object(audit_cut, 'speech_of_cut', return_value=[]), \
                mock.patch.object(sys, 'argv', ['audit_cut.py', str(edit), *extra]), contextlib.redirect_stdout(out):
            code = audit_cut.main()
        return code, out.getvalue(), [c.args[0] for c in runner.call_args_list]

    def test_states_the_prompt_regime(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in ('cut.mp4', 'edl.json'):
                (Path(tmp) / name).write_text('{}', encoding='utf-8')
            for extra, regime in (([], ['--no-prompt']), (['--prompt', 'Kiwify'], ['--prompt', 'Kiwify'])):
                commands = self.audit(tmp, *extra)[2]
                command = next(c for c in commands if c[0].endswith('transcribe.py'))
                self.assertEqual(command[-len(regime):], regime)

    def test_breaths_inside_dry_false_intervals_are_kept_apart(self):
        with tempfile.TemporaryDirectory() as tmp:
            edit = Path(tmp)
            (edit / 'cut.mp4').write_text('', encoding='utf-8')
            (edit / 'edl.json').write_text(json.dumps({'ranges': [{'id': 'r01', 'dry': False}, {'id': 'r02'}]}), encoding='utf-8')
            (edit / 'cut_timeline.json').write_text(json.dumps({'segments': [
                {'id': 'r01', 'out_start': 1.0, 'out_end': 3.0, 'quote': 'keep this pause'},
                {'id': 'r02', 'out_start': 3.0, 'out_end': 8.0, 'quote': 'tight'}]}), encoding='utf-8')
            code, out, _ = self.audit(edit, breaths=[{'start': 1.5, 'end': 2.0, 'dur': 0.5}, {'start': 5.0, 'end': 5.6, 'dur': 0.6}])
            self.assertEqual(code, 1)
            self.assertIn('breaths: 1 silences of 0.25 s or more (below -40 dB):', out)
            self.assertIn('inside interval r01 with "dry": false', out)
            self.assertIn('review remaining unnecessary pauses', out)
            self.assertNotIn('"breaths"', out)
            # Only kept pauses: nothing to review, and the audit passes.
            code, out, _ = self.audit(edit, breaths=[{'start': 1.5, 'end': 2.0, 'dur': 0.5}])
            self.assertEqual(code, 0)
            self.assertIn('breaths: no silence of 0.25 s or more (below -40 dB)', out)


if __name__ == '__main__':
    unittest.main()
