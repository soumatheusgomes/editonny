import http.client
import json
import unittest
from preview_fixture import running_preview


class PreviewServerTests(unittest.TestCase):
    def test_style_report_names_a_motion_field_compose_accepts(self):
        import re
        from compose_final import _motion_items
        from watch_preview import format_style
        line = format_style({'elements': {'motion': True}, 'motionCount': 'some', 'motionTheme': 'paper'}, {})
        field = re.search(r'\(motion\.(\w+) (\d+)\)', line)
        self.assertIsNotNone(field, line)
        self.assertEqual(_motion_items({'motion': {field.group(1): int(field.group(2)), 'items': []}}), [])

    def test_watcher_rejects_approval_after_dependency_change(self):
        from watch_preview import stale_approval_note
        info = self.session.file_info('cut.mp4')
        approval = {'phase': 'cut', 'file': 'cut.mp4', 'file_mtime': info['mtime'], 'file_size': info['size'],
                    'file_sha256': info['sha256'], 'dependencies_hash': info['dependencies_hash']}
        self.assertIsNone(stale_approval_note(self.session.edit_dir, approval))
        (self.session.edit_dir / 'edit_data.json').write_text('{"captions":{"enabled":false}}', encoding='utf-8')
        self.assertIsNotNone(stale_approval_note(self.session.edit_dir, approval))

    def test_managed_preview_tracks_actual_asset_bytes(self):
        import sys
        from pathlib import Path
        from common import content_hash
        from preview_server import Session, STYLE_CATALOG_PATH
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from workflow_fixtures import config, confirmation
        from workspace import create_workspace
        root = self.session.edit_dir.parent
        source = root / 'original.mp4'; source.write_bytes(b'selected original')
        inspection = {'sources': [{'id': 'source', 'path': str(source), 'sha256': content_hash(source), 'streams': [0]}]}
        settings = config(root / 'Downloads')
        project = create_workspace(root / 'Downloads', 'Preview', inspection, settings, confirmation(settings, inspection))
        edit = project.parent / 'edit'; (edit / 'cut.mp4').write_bytes(b'cut')
        asset = project / 'assets/insert.png'; asset.write_bytes(b'first asset')
        session = Session(edit, STYLE_CATALOG_PATH, '127.0.0.1', 0)
        before = session.file_info('cut.mp4')
        asset.write_bytes(b'other asset')
        self.assertNotEqual(session.file_info('cut.mp4')['dependencies_hash'], before['dependencies_hash'])

    def setUp(self):
        self.fixture = running_preview()
        self.session, self.url = self.fixture.__enter__()
        self.addCleanup(self.fixture.__exit__, None, None, None)

    def request(self, path, body=None, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.session.port, timeout=5)
        try:
            connection.request('POST' if body is not None else 'GET', path,
                               json.dumps(body) if body is not None else None, headers or {})
            response = connection.getresponse()
            return response.status, dict(response.headers), response.read()
        finally:
            connection.close()

    def auth(self):
        return {'Content-Type': 'application/json', 'X-Preview-Token': self.session.token}

    def test_port_zero_advertises_the_bound_port(self):
        import json, subprocess, sys, tempfile, time, urllib.request
        from pathlib import Path
        helper = Path(__file__).resolve().parents[2] / 'skills/editonny/engine/helpers/preview_server.py'
        with tempfile.TemporaryDirectory(prefix='editonny preview port ') as temp:
            edit = Path(temp) / 'edit'
            edit.mkdir()
            info_path = edit / 'preview_cache/server.json'
            process = subprocess.Popen([sys.executable, str(helper), '--edit-dir', str(edit), '--port', '0'],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                for _ in range(100):
                    if info_path.is_file() and json.loads(info_path.read_text(encoding='utf-8')).get('status') == 'running':
                        break
                    time.sleep(0.1)
                info = json.loads(info_path.read_text(encoding='utf-8'))
                self.assertNotEqual(info['port'], 0)
                self.assertTrue(info['url'].endswith(f":{info['port']}"))
                with urllib.request.urlopen(info['url'], timeout=5) as response:
                    self.assertEqual(response.status, 200)
            finally:
                process.terminate()
                process.wait(timeout=10)

    def test_static_modules_and_csp(self):
        code, headers, body = self.request('/api.mjs')
        self.assertEqual(code, 200)
        self.assertIn('javascript', headers['Content-Type'])
        self.assertIn(b'postJson', body)
        _, headers, _ = self.request('/')
        self.assertIn("script-src 'self'", headers['Content-Security-Policy'])

    def test_project_documents_cannot_execute_on_the_preview_origin(self):
        for name, content in [('downloaded.html', '<script>window.executed=true</script>'),
                              ('active.svg', '<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"/>')]:
            (self.session.edit_dir / name).write_text(content, encoding='utf-8')
            for request_headers in ({}, {'Range': 'bytes=0-9'}):
                with self.subTest(name=name, headers=request_headers):
                    code, headers, _ = self.request('/media/' + name, headers=request_headers)
                    self.assertIn(code, (200, 206))
                    self.assertEqual(headers.get('Content-Security-Policy'), "default-src 'none'; sandbox")
                    self.assertEqual(headers.get('X-Content-Type-Options'), 'nosniff')

    def test_rejects_traversal_unlisted_paths_and_hostile_host(self):
        for path in ['/../helpers/preview_server.py', '/%2e%2e/helpers/common.py', '/state.json']:
            self.assertEqual(self.request(path)[0], 404)
        self.assertEqual(self.request('/media/../outside')[0], 403)
        self.assertEqual(self.request('/', headers={'Host': 'attacker.example'})[0], 403)
        self.assertEqual(self.request('/api/state', headers={'Sec-Fetch-Site': 'cross-site'})[0], 403)

    def test_refuses_unauthenticated_and_cross_origin_writes(self):
        body = {'phase': 'cut', 'marks': []}
        self.assertEqual(self.request('/api/marks', body)[0], 403)
        self.assertEqual(self.request('/api/marks', body, {**self.auth(), 'Origin': 'https://attacker.example'})[0], 403)
        self.assertFalse((self.session.edit_dir / 'preview_marks.json').exists())
        self.assertEqual(self.request('/api/marks', body, self.auth())[0], 200)

    def test_stale_approval_does_not_overwrite_current_approval(self):
        info = self.session.file_info('cut.mp4')
        body = {'phase': 'cut', 'file': 'cut.mp4', 'file_mtime': info['mtime'], 'file_size': info['size'],
                'file_sha256': info.get('sha256'), 'dependencies_hash': info.get('dependencies_hash')}
        self.assertEqual(self.request('/api/approve', body, self.auth())[0], 200)
        saved = self.session.edit_dir / 'preview_approval.json'
        before = saved.read_bytes()
        body['file_mtime'] -= 1
        self.assertEqual(self.request('/api/approve', body, self.auth())[0], 409)
        self.assertEqual(saved.read_bytes(), before)

    def test_missing_identity_is_not_an_approval(self):
        self.assertEqual(self.request('/api/approve', {'phase': 'cut'}, self.auth())[0], 400)

    def test_same_size_timestamp_replacement_invalidates_approval(self):
        import os
        path = self.session.edit_dir / 'cut.mp4'
        info = self.session.file_info('cut.mp4')
        body = {'phase': 'cut', 'file': 'cut.mp4', 'file_mtime': info['mtime'], 'file_size': info['size'],
                'file_sha256': info.get('sha256'), 'dependencies_hash': info.get('dependencies_hash')}
        content = path.read_bytes()
        path.write_bytes(bytes([content[0] ^ 1]) + content[1:])
        os.utime(path, (info['mtime'], info['mtime']))
        self.assertEqual(self.request('/api/approve', body, self.auth())[0], 409)


if __name__ == '__main__':
    unittest.main()
