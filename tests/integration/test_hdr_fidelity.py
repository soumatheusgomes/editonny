import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'skills/editonny/engine/helpers'))
from common import probe_video
from fidelity import delta_e_2000, measure_fidelity
from media_fixtures import make_hdr_patches


class HdrFidelityTests(unittest.TestCase):
    def test_ciede2000_published_reference_pairs(self):
        import numpy as np
        # Sharma, Wu and Dalal supplementary test pairs (2005), first three rows.
        first = np.array([[50, 2.6772, -79.7751], [50, 3.1571, -77.2803], [50, 2.8361, -74.02]])
        second = np.array([[50, 0, -82.7485]] * 3)
        np.testing.assert_allclose(delta_e_2000(first, second), [2.0425, 2.8615, 3.4412], atol=0.0001)

    def test_pq_hlg_and_twelve_bit_cut_preserve_picture(self):
        for transfer, depth in [('smpte2084', 10), ('arib-std-b67', 10), ('smpte2084', 12)]:
            with self.subTest(transfer=transfer, depth=depth), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); source = root / 'source.mp4'
                make_hdr_patches(source, transfer, depth)
                edit = root / 'edit'; edit.mkdir()
                edl = edit / 'edl.json'
                edl.write_text(json.dumps({'sources': {'s': str(source)}, 'ranges': [
                    {'id': 'a', 'source': 's', 'start': 0, 'end': 1}]}), encoding='utf-8')
                result = subprocess.run([sys.executable, str(ROOT / 'skills/editonny/engine/helpers/render_cut.py'), str(edl), '--no-jcut'], capture_output=True, text=True, encoding='utf-8', errors='replace')
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                facts = probe_video(edit / 'cut.mp4')
                self.assertEqual(facts['bit_depth'], depth)
                self.assertEqual(facts['color_transfer'], transfer)
                result = measure_fidelity({'transfer': transfer}, edit / 'cut.mp4', [
                    {'source': str(source), 'sourceTime': 0.5, 'outputTime': 0.5}])
                self.assertEqual(result['status'], 'passed', result)


if __name__ == '__main__':
    unittest.main()
