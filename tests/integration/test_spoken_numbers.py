"""Recorded amounts must survive parsing and the full suggestion CLI unchanged."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

HELPERS = Path(__file__).resolve().parents[2] / 'skills/editonny/engine/helpers'
sys.path.insert(0, str(HELPERS))
import spoken_numbers as numbers


class SpokenMoneyTests(unittest.TestCase):
    CASES = [('$1,500', 1500, '$'), ('$1,500.25', 1500.25, '$'),
             ('two thousand five hundred dollars', 2500, '$'),
             ('one hundred and fifty dollars', 150, '$'), ('150 dollars', 150, '$'),
             ('R$ 1.500,25', 1500.25, 'R$'), ('dois mil e quinhentos reais', 2500, 'R$'),
             ('dos mil quinientos dolares', 2500, '$'), ('1.500,25 euros', 1500.25, '€'),
             ('one million two hundred thousand dollars', 1200000, '$'),
             ('£1,250.50', 1250.5, '£'), ('US$ 1,500', 1500, 'US$')]

    def test_whole_money_spans_and_values(self):
        for expression, expected, _ in self.CASES:
            with self.subTest(expression=expression):
                text = numbers.normal_value(expression)
                found = numbers.find_money(text)
                self.assertEqual(len(found), 1)
                self.assertEqual(text[found[0].start:found[0].end], text)
                self.assertEqual(found[0].value, expected)
                self.assertEqual(numbers.parse_money(expression), expected)

    def test_compound_values_and_count_exclusions(self):
        for expression, expected in [('two thousand five hundred',2500), ('one hundred twenty five',125),
                                     ('um milhao e meio',1500000), ('un millon y medio',1500000),
                                     ('1 million and 200 thousand',1200000)]:
            self.assertEqual(numbers.parse_money(expression), expected, expression)
            if any(scale in expression for scale in ['thousand', 'milhao', 'millon', 'million']):
                matches = numbers.find_money(numbers.normal_value(expression))
                self.assertEqual(len(matches), 1, expression)
                self.assertEqual(matches[0].value, expected, expression)
                self.assertEqual(matches[0].end, len(expression), expression)
        self.assertEqual(numbers.find_money('10 mil seguidores'), [])
        self.assertEqual(numbers.find_money('five thousand followers'), [])

    def test_suggestion_cli_preserves_amount_and_currency(self):
        with tempfile.TemporaryDirectory() as tmp:
            edit = Path(tmp)
            (edit / 'transcripts').mkdir()
            (edit / 'cut_timeline.json').write_text(json.dumps({'duration':20,'segments':[]}), encoding='utf-8')
            for expression, expected, prefix in self.CASES:
                with self.subTest(expression=expression):
                    words = [{'text':word,'start':5+i*.3,'end':5+i*.3+.25} for i,word in enumerate(expression.split())]
                    (edit / 'transcripts/cut.json').write_text(json.dumps({'words':words}), encoding='utf-8')
                    result = subprocess.run([sys.executable,str(HELPERS/'suggest_visuals.py'),'--edit-dir',str(edit),'--json'],
                                            capture_output=True,text=True,timeout=20, encoding='utf-8', errors='replace')
                    self.assertEqual(result.returncode,0,result.stderr)
                    motions=json.loads(result.stdout)['motion']
                    money=[m for m in motions if m['template'] in ('counter','price_tag')]
                    self.assertEqual(len(money),1,result.stdout)
                    params=money[0]['params']
                    self.assertEqual(params.get('value',params.get('price')),expected)
                    self.assertEqual(params['prefix'],prefix)

    def test_comment_suggestion_renders_its_keyword(self):
        from visuals.templates import comment_cta
        with tempfile.TemporaryDirectory() as tmp:
            edit = Path(tmp)
            (edit / 'transcripts').mkdir()
            (edit / 'cut_timeline.json').write_text(json.dumps({'duration':20,'segments':[]}), encoding='utf-8')
            words = [{'text':word,'start':5+i*.3,'end':5+i*.3+.25} for i,word in enumerate('comenta a palavra ideia aqui'.split())]
            (edit / 'transcripts/cut.json').write_text(json.dumps({'words':words}), encoding='utf-8')
            result = subprocess.run([sys.executable,str(HELPERS/'suggest_visuals.py'),'--edit-dir',str(edit),'--json'],
                                    capture_output=True,text=True,timeout=20, encoding='utf-8', errors='replace')
            self.assertEqual(result.returncode,0,result.stderr)
            comments=[m for m in json.loads(result.stdout)['motion'] if m['template']=='comment_cta']
            self.assertEqual(len(comments),1,result.stdout)
            params=comments[0]['params']
            prompt=comment_cta._prompt(params,params['keyword'])
            self.assertIn('IDEIA',prompt)
            self.assertNotIn('{',prompt)

    def test_assemble_sources_cli(self):
        result=subprocess.run([sys.executable,str(HELPERS/'assemble_cut.py'),'--help'],capture_output=True,text=True, encoding='utf-8', errors='replace')
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('--sources',result.stdout)
        self.assertNotIn('--fonts',result.stdout)
        with tempfile.TemporaryDirectory() as edit:
            selected = subprocess.run([sys.executable,str(HELPERS/'assemble_cut.py'),'--edit-dir',edit,
                                       '--sources','requested.mov','--dry-run'],capture_output=True,text=True, encoding='utf-8', errors='replace')
            self.assertEqual(selected.returncode,2)
            self.assertIn('no transcript: requested',selected.stderr)
            self.assertNotIn('unrecognized arguments',selected.stderr)
