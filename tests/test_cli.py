from contextlib import redirect_stdout
from pathlib import Path
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / 'editonny.py'


class CLITests(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run([sys.executable, str(CLI), *args], cwd=tempfile.gettempdir(),
                              text=True, capture_output=True, encoding='utf-8', errors='replace')

    def test_catalog_accent_insensitive_and_json(self):
        result = self.run_cli('catalog', 'transitions', '--json')
        self.assertEqual(result.returncode, 0, result.stderr)
        scenes = json.loads(result.stdout)
        self.assertGreater(len(scenes), 0)
        self.assertTrue(all('id' in s for s in scenes))

    def test_unknown_helper_rejected_without_path_traversal(self):
        result = self.run_cli('helper', '../install')
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('Traceback', result.stderr)

    def test_helper_help_lists_runnable_helpers(self):
        result = self.run_cli('helper', '--help')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('transcribe', result.stdout)
        self.assertNotIn('workspace', result.stdout.split('helpers:')[1])

    def test_scene_requires_a_project(self):
        result = self.run_cli('scene')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('--project', result.stderr)

    def test_scene_invalid_arguments_fail_cleanly_before_writing(self):
        for arguments in [('scene', '--project'), ('scene', '--project', '/tmp', '--unknown-option', 'scene'),
                          ('scene', '--project', '/tmp', '--use', 'scene', '--start', '1,2'),
                          ('scene', '--project', '/tmp', '--use', 'scene', '--start', 'nan')]:
            with self.subTest(arguments=arguments):
                result = self.run_cli(*arguments)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn('Traceback', result.stderr)

    def test_guide_runs_the_web_app_only_from_a_full_checkout_with_dependencies(self):
        spec = importlib.util.spec_from_file_location('cli', ROOT / 'skills/editonny/scripts/editonny.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            module.SKILL = root / 'skills/editonny'
            with self.assertRaisesRegex(ValueError, 'standalone skill'):
                module.guide(4321, False)
            (root / 'web').mkdir()
            (root / 'web/package.json').write_text('{}', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'npm ci'):
                module.guide(4321, False)
            (root / 'node_modules/next').mkdir(parents=True)
            started, opened = [], []

            class FakeServer:
                def __init__(self, lines, code):
                    self.stdout, self.code = iter(f'{line}\n' for line in lines), code
                def wait(self):
                    return self.code
                def terminate(self):
                    pass

            def serve(lines, code):
                return lambda command, **kwargs: started.append((command, kwargs)) or FakeServer(lines, code)

            ready = ['▲ Next.js 16.4.2 (Turbopack)', '- Local:         http://localhost:55363', '✓ Ready in 127ms']
            busy = ['⨯ Failed to start server', 'Error: listen EADDRINUSE: address already in use ::1:4321']
            with mock.patch.object(module.shutil, 'which', lambda name: '/usr/bin/npm' if name == 'npm' else None), \
                    mock.patch.object(module.webbrowser, 'open', opened.append), redirect_stdout(io.StringIO()):
                # Port 0 lets Next pick a port; the browser opens at the reported address once the server is ready.
                with mock.patch.object(module.subprocess, 'Popen', serve(ready, 0)):
                    self.assertEqual(module.guide(0, True), 0)
                self.assertEqual(opened, ['http://localhost:55363'])
                # A server that fails to start opens nothing and passes its exit code on.
                with mock.patch.object(module.subprocess, 'Popen', serve(busy, 1)):
                    self.assertEqual(module.guide(4321, True), 1)
                self.assertEqual(opened, ['http://localhost:55363'])
            command, kwargs = started[0]
            self.assertEqual(command, ['/usr/bin/npm', 'run', 'dev', '-w', '@editonny/web', '--', '--hostname', 'localhost', '--port', '0'])
            self.assertEqual(kwargs['cwd'], root)

    def test_doctor_json_has_real_checks(self):
        result = self.run_cli('doctor', '--json')
        self.assertIn(result.returncode, [0, 1])
        report = json.loads(result.stdout)
        self.assertEqual(report['product'], 'editonny')
        self.assertTrue(any(c['name'] == 'ffmpeg' for c in report['checks']))
        self.assertTrue(all('ok' in c for c in report['checks']))

    def test_help_from_other_working_directory(self):
        result = self.run_cli('--help')
        self.assertEqual(result.returncode, 0)
        for command in ['doctor', 'catalog', 'new', 'scene', 'helper', 'guide']:
            self.assertIn(command, result.stdout)


if __name__ == '__main__':
    unittest.main()
