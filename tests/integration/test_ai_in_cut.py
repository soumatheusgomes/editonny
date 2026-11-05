"""Generated-footage insertion must validate before it writes anything."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

RECIPE = Path(__file__).resolve().parents[2] / 'skills/editonny/visuals/recipes/common/ai_in_cut.py'


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg is required')
class AiInCutTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='editonny ai in cut '))
        self.addCleanup(shutil.rmtree, self.root)
        self.edit = self.root / 'edit'
        self.edit.mkdir()
        self.source, self.ai = self.root / 'source.mp4', self.root / 'generated.mp4'
        subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=s=160x240:r=30:d=3',
                        '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(self.source)], check=True)
        shutil.copy(self.source, self.ai)
        self.request = self.root / 'request.json'
        self.request.write_text(json.dumps({'file': str(self.source), 'source': str(self.source), 'sourceFps': 30,
                                            'pieces': [{'sourceStart': 0.0, 'start': 0.0, 'frames': 90}]}), encoding='utf-8')
        self.edl = self.edit / 'edl_manual.json'
        self.edl.write_text(json.dumps({'sources': {'main': str(self.source)},
                                        'ranges': [{'id': 'r1', 'source': 'main', 'start': 0, 'end': 3}]}), encoding='utf-8')
        self.cam = self.root / 'generated_cam.mp4'

    def run_recipe(self, *extra, edit=None):
        return subprocess.run([sys.executable, str(RECIPE), '--request', str(self.request), '--ai', str(self.ai),
                               '--edit', str(edit or self.edit), '--edl', str(self.edl), '--delay-ms', '0',
                               '--start', '1', '--end', '2', *extra], capture_output=True, text=True, timeout=120, encoding='utf-8', errors='replace')

    def test_rejected_runs_write_nothing(self):
        before = self.edl.read_text(encoding='utf-8')
        sync = self.edit / 'synchronization.json'
        sync.write_text(json.dumps({'primary': 'main', 'sources': {'AI1': {'file': '/elsewhere/other_cam.mp4'}}}), encoding='utf-8')
        result = self.run_recipe()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('choose another --name', result.stderr)
        sync.unlink()
        self.edl.write_text(json.dumps({**json.loads(before), 'synchronization': 'nested/synchronization.json'}), encoding='utf-8')
        result = self.run_recipe()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('point the EDL', result.stderr)
        self.edl.write_text(before, encoding='utf-8')
        other = self.root / 'other edit'
        other.mkdir()
        result = self.run_recipe(edit=other)       # The renderer would read the EDL folder's synchronization.json.
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('point the EDL', result.stderr)
        result = self.run_recipe('--name', 'main')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('primary source', result.stderr)
        self.assertFalse(self.cam.exists())
        self.assertEqual(list(other.iterdir()), [])
        self.assertFalse(sync.exists())

    def test_original_footage_named_like_the_copy_is_never_overwritten(self):
        original = self.root / 'generated_cam.mp4'
        self.source.rename(original)
        before = original.read_bytes()
        self.request.write_text(json.dumps({**json.loads(self.request.read_text(encoding='utf-8')), 'file': str(original), 'source': str(original)}), encoding='utf-8')
        self.edl.write_text(json.dumps({'sources': {'main': str(original)},
                                        'ranges': [{'id': 'r1', 'source': 'main', 'start': 0, 'end': 3}]}), encoding='utf-8')
        result = self.run_recipe()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(original.read_bytes(), before)
        self.assertEqual(json.loads(self.edl.read_text(encoding='utf-8'))['sources']['AI1'], str(self.root / 'generated_cam_2.mp4'))

    def test_many_windows_get_unique_segment_ids(self):
        sys.path.insert(0, str(RECIPE.parent))
        try:
            from ai_in_cut import split_ranges
        finally:
            sys.path.remove(str(RECIPE.parent))
        windows = [(k * 2 + 1, k * 2 + 2, f'AI1_p{k + 1}') for k in range(14)]
        ranges, _ = split_ranges({'ranges': [{'id': 'r1', 'source': 'main', 'start': 0, 'end': 30}]}, 'main', windows)
        ids = [r['id'] for r in ranges]
        self.assertEqual(len(ids), 29)
        self.assertEqual(len(set(ids)), len(ids))
        self.assertEqual(ids[:2] + ids[25:27], ['r1a', 'r1b', 'r1z', 'r1_27'])
        self.assertEqual([(r['start'], r['end']) for r in ranges[:3]], [(0, 1), (1, 2), (2, 3)])

    def test_insertion_splits_ranges_and_can_run_again(self):
        for _ in range(2):
            result = self.run_recipe()
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.edl.write_text((self.edit / 'edl_manual.before-ai.json').read_text(encoding='utf-8'), encoding='utf-8')
        result = self.run_recipe()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        edl = json.loads(self.edl.read_text(encoding='utf-8'))
        self.assertEqual(edl['sources']['AI1'], str(self.cam))
        self.assertEqual([r.get('picture_source') for r in edl['ranges']], [None, 'AI1', None])
        # Rebuild from both backups (there was no synchronization file): the old copy is kept, not overwritten.
        self.edl.write_text((self.edit / 'edl_manual.before-ai.json').read_text(encoding='utf-8'), encoding='utf-8')
        (self.edit / 'synchronization.json').unlink()
        result = self.run_recipe()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(self.edl.read_text(encoding='utf-8'))['sources']['AI1'], str(self.root / 'generated_cam_2.mp4'))
        self.assertTrue(self.cam.exists())


if __name__ == '__main__':
    unittest.main()
