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
            data.write_text(json.dumps({'base': str(source), 'captions': {'enabled': False}}))
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
            data = root / 'edit_data.json'; data.write_text(json.dumps({'base': str(source), 'captions': {'enabled': False}}))
            output = root / 'composed.mp4'
            plan = build_plan(data, str(output), False); plan.renders.mkdir(parents=True)
            Image.new('RGBA', (128, 64)).save(root / 'transparent.png')
            track = root / 'overlay.ffconcat'
            track.write_text("ffconcat version 1.0\nfile 'transparent.png'\nduration 1\nfile 'transparent.png'\n")
            with self.assertRaisesRegex(ComposeError, 'chroma|4:2:0|precision'):
                run_video(plan, [], {'captions': (track, 1, [(0, 24)])}, None, output)
            self.assertFalse(output.exists())

    def test_full_range_bt601_caption_overlay_keeps_unaffected_picture(self):
        from PIL import Image, ImageDraw
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'source.mp4'; make_patches(source, True, 'smpte170m')
            data = root / 'edit_data.json'; data.write_text(json.dumps({'base': str(source), 'captions': {'enabled': False}}))
            output = root / 'composed.mp4'
            plan = build_plan(data, str(output), False); plan.renders.mkdir(parents=True)
            overlay = Image.new('RGBA', (128, 64))
            ImageDraw.Draw(overlay).rectangle((4, 48, 24, 60), fill=(255, 255, 255, 180))
            overlay.save(root / 'overlay.png')
            track = root / 'overlay.ffconcat'
            track.write_text("ffconcat version 1.0\nfile 'overlay.png'\nduration 0.041666667\nfile 'overlay.png'\n")
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
            data.write_text(json.dumps({'base': str(source), 'captions': {'enabled': False}}))
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
                {'id': 'source', 'path': str(source), 'facts': probe_video(source)}]}))
            data = root / 'edit_data.json'
            data.write_text(json.dumps({'base': str(source), 'contractHash': 'confirmed'}))
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
            data.write_text(json.dumps({'base': str(source), 'captions': {'enabled': False}}))
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


class VariableFrameRateReferenceTests(unittest.TestCase):
    def test_reference_frame_is_the_one_on_screen(self):
        from fidelity import _frame
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'vfr.mp4'
            # Ten frames whose gray level identifies the frame; irregular steps make the cadence variable.
            subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=black:s=64x64:r=30:d=0.34',
                '-vf', "geq=lum='N*24':cb=128:cr=128,setpts='(N*0.1+mod(N,2)*0.03)/TB'",
                '-fps_mode', 'passthrough', '-video_track_timescale', '600', '-c:v', 'libx264', '-qp', '0',
                '-pix_fmt', 'yuv420p', '-color_primaries', 'bt709', '-color_trc', 'bt709', '-colorspace', 'bt709',
                str(source)], check=True)
            self.assertTrue(probe_video(source)['is_vfr'])
            # Frame 3 is presented at 0.33 s and frame 4 at 0.40 s; 0.36 s still shows frame 3.
            shown = _frame(source, 0.36, width=64)
            expected = _frame(source, 0.33, width=64)
            following = _frame(source, 0.40, width=64)
            self.assertAlmostEqual(float(abs(shown - expected).mean()), 0, places=3)
            self.assertGreater(float(abs(shown - following).mean()), 0.02)


if __name__ == '__main__':
    unittest.main()
