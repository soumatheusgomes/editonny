import json
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest

HELPERS = Path(__file__).resolve().parents[2] / 'skills/editonny/engine/helpers'
sys.path.insert(0, str(HELPERS))
from common import probe_video
from fidelity import measure_fidelity
from media_fixtures import make_hdr_patches, make_patches
from compose_final import ComposeError, build_plan, build_tracks, run_video


class CompositionFidelityTests(unittest.TestCase):
    def test_motion_requires_its_validated_common_color_space(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'source.mp4'; make_patches(source, True, 'smpte170m')
            data = root / 'edit_data.json'
            data.write_text(json.dumps({'base': str(source), 'captions': {'enabled': False}}), encoding='utf-8')
            output = root / 'composed.mp4'
            plan = build_plan(data, str(output), False); plan.renders.mkdir(parents=True)
            overlay = root / 'motion.png'
            Image.new('RGBA', (128, 64)).save(overlay)
            clip = {'file': str(overlay), 'fps': 24, 'frames': 1, 'x': 0, 'y': 0}
            with self.assertRaisesRegex(ComposeError, 'common composition space'):
                run_video(plan, [], {'motion': [(clip, 0, [(0, 1)])]}, None, output)
            self.assertFalse(output.exists())

    def test_unsupported_chroma_overlay_blocks_before_writing(self):
        from PIL import Image
        import subprocess
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); source = root / '444.mp4'
            subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=gray:s=128x64:r=24:d=1',
                '-vf', 'format=yuv444p,setparams=range=tv:color_primaries=bt709:color_trc=bt709:colorspace=bt709',
                '-c:v', 'libx264', '-pix_fmt', 'yuv444p', str(source)], check=True)
            data = root / 'edit_data.json'; data.write_text(json.dumps({'base': str(source), 'captions': {'enabled': False}}), encoding='utf-8')
            output = root / 'composed.mp4'
            plan = build_plan(data, str(output), False); plan.renders.mkdir(parents=True)
            Image.new('RGBA', (128, 64)).save(root / 'transparent.png')
            track = root / 'overlay.ffconcat'
            track.write_text("ffconcat version 1.0\nfile 'transparent.png'\nduration 1\nfile 'transparent.png'\n", encoding='utf-8')
            with self.assertRaisesRegex(ComposeError, 'chroma|4:2:0|precision'):
                run_video(plan, [], {'captions': (track, 1, [(0, 24)])}, None, output)
            self.assertFalse(output.exists())

    def test_full_range_bt601_caption_overlay_keeps_unaffected_picture(self):
        from PIL import Image, ImageDraw
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'source.mp4'; make_patches(source, True, 'smpte170m')
            data = root / 'edit_data.json'; data.write_text(json.dumps({'base': str(source), 'captions': {'enabled': False}}), encoding='utf-8')
            output = root / 'composed.mp4'
            plan = build_plan(data, str(output), False); plan.renders.mkdir(parents=True)
            overlay = Image.new('RGBA', (128, 64))
            ImageDraw.Draw(overlay).rectangle((4, 48, 24, 60), fill=(255, 255, 255, 180))
            overlay.save(root / 'overlay.png')
            track = root / 'overlay.ffconcat'
            track.write_text("ffconcat version 1.0\nfile 'overlay.png'\nduration 0.041666667\nfile 'overlay.png'\n", encoding='utf-8')
            run_video(plan, [], {'captions': (track, 1, [(0, 1)])}, None, output)
            actual = probe_video(output)
            self.assertEqual(actual['color_range'], 'pc')
            self.assertEqual(actual['color_space'], 'smpte170m')
            report = measure_fidelity({'transfer': 'bt709', 'primaries': 'bt709', 'masks': [[0, .7, .25, 1]]}, output,
                [{'source': str(source), 'sourceTime': 0, 'outputTime': 0}])
            self.assertEqual(report['status'], 'passed', report)

    def test_no_effect_composition_preserves_full_range_and_matrix(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'source.mp4'; make_patches(source, True, 'smpte170m')
            data = root / 'edit_data.json'
            data.write_text(json.dumps({'base': str(source), 'captions': {'enabled': False}}), encoding='utf-8')
            output = root / 'composed.mp4'
            plan = build_plan(data, str(output), False)
            plan.renders.mkdir(parents=True)
            run_video(plan, [], {}, None, output)
            facts = probe_video(output)
            self.assertEqual(facts['color_range'], 'pc')
            self.assertEqual(facts['color_space'], 'smpte170m')

    def test_composition_keeps_the_confirmed_codec(self):
        from unittest import mock
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from workflow_fixtures import config
        contract = config()
        contract['captions']['enabled'] = False
        contract['workflow']['encoding']['videoCodec'] = 'hevc'
        contract['workflow']['video'].update(width=128, height=64, frameRate='24')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'cut.mp4'; make_patches(source)
            (root / 'source_manifest.json').write_text(json.dumps({'sources': [
                {'id': 'source', 'path': str(source), 'facts': probe_video(source)}]}), encoding='utf-8')
            data = root / 'edit_data.json'
            data.write_text(json.dumps({'base': str(source), 'contractHash': 'confirmed'}), encoding='utf-8')
            with mock.patch('project_contract.managed_config', return_value=contract), \
                 mock.patch('project_contract.settings_hash', return_value='confirmed'):
                plan = build_plan(data, str(root / 'composed.mp4'), False)
            self.assertEqual(plan.media_plan['encoder'], 'libx265')
            # No effects: the unchanged-essence copy must not keep an H.264 base against an HEVC contract.
            plan.renders.mkdir(parents=True)
            output = root / 'composed.mp4'
            run_video(plan, [], {}, None, output)
            self.assertEqual(probe_video(output)['codec'], 'hevc')

    def test_empty_built_tracks_preserve_hdr_packets_with_audio_only_effects(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'source.mp4'; make_hdr_patches(source, 'smpte2084')
            data = root / 'edit_data.json'
            data.write_text(json.dumps({'base': str(source), 'captions': {'enabled': False}}), encoding='utf-8')
            output = root / 'composed.mp4'
            plan = build_plan(data, str(output), False)
            tracks = build_tracks(plan, [])
            self.assertEqual(tracks['mid'], (None, [], []))
            self.assertEqual(tracks['top'], (None, [], []))
            tracks['sfx_events'] = [{'frame': 0, 'name': 'whoosh'}]
            run_video(plan, [], tracks, None, output)
            def packets(path):
                return subprocess.check_output(['ffprobe', '-v', 'error', '-select_streams', 'v:0',
                    '-show_packets', '-show_data_hash', 'sha256', '-show_entries', 'packet=data_hash',
                    '-of', 'csv=p=0', str(path)])
            self.assertEqual(packets(source), packets(output))
            self.assertEqual(probe_video(output)['pix_fmt'], 'yuv420p10le')


if __name__ == '__main__':
    unittest.main()
