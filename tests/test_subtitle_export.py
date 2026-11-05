import importlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

HELPERS = Path(__file__).resolve().parents[1] / 'skills/editonny/engine/helpers'
sys.path.insert(0, str(HELPERS))


class SubtitleExportTests(unittest.TestCase):
    def test_sidecars_apply_the_same_accepted_text_corrections(self):
        from export_subtitles import export_subtitles
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); project = root / 'project'; project.mkdir()
            edit = root / 'edit'; (edit / 'transcripts').mkdir(parents=True)
            (project / 'project.json').write_text(json.dumps({'edit': '../edit', 'corrections': {'Codecs': 'Codex'}}), encoding='utf-8')
            (edit / 'transcripts/cut.json').write_text(json.dumps({'words': [{'text': 'Codecs', 'start': 0.1, 'end': 0.9}]}), encoding='utf-8')
            for format_name in ('srt', 'vtt'):
                output = root / ('captions.' + format_name)
                export_subtitles(project, format_name, output)
                self.assertIn('Codex', output.read_text(encoding='utf-8'))
                self.assertNotIn('Codecs', output.read_text(encoding='utf-8'))

    def test_srt_vtt_preserve_unicode_negation_and_timing_without_overwrite(self):
        self.assertTrue((HELPERS / 'export_subtitles.py').exists(), 'Subtitle delivery is missing')
        module = importlib.import_module('export_subtitles')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); project = root / 'project'; project.mkdir()
            edit = root / 'edit'; (edit / 'transcripts').mkdir(parents=True)
            (project / 'project.json').write_text(json.dumps({'edit': '../edit'}), encoding='utf-8')
            (edit / 'transcripts/cut.json').write_text(json.dumps({'words': [
                {'text': 'Não', 'start': 0.1, 'end': 0.4}, {'text': 'mude', 'start': 0.4, 'end': 0.9}]}), encoding='utf-8')
            for name in ('srt', 'vtt'):
                output = root / ('captions.' + name)
                module.export_subtitles(project, name, output)
                text = output.read_text(encoding='utf-8')
                self.assertIn('Não mude', text)
                self.assertIn('00:00:00,100' if name == 'srt' else '00:00:00.100', text)
                with self.assertRaises(FileExistsError):
                    module.export_subtitles(project, name, output)


if __name__ == '__main__':
    unittest.main()
