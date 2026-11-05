"""Library operations must never reach files outside the library folder."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

HELPERS = Path(__file__).resolve().parents[2] / 'skills/editonny/engine/helpers'
sys.path.insert(0, str(HELPERS))
import music_library
from workflow_fixtures import make_link


class MusicLibraryTests(unittest.TestCase):
    def test_removal_refuses_a_track_behind_a_symlinked_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            originals, lib = root / 'originals', root / 'library'
            originals.mkdir()
            lib.mkdir()
            (originals / 'song.mp3').write_bytes(b'original')
            make_link(self, (lib / 'calm'), originals, directory=True)
            index = {'version': 1, 'moods': {}, 'trash': [],
                     'tracks': [{'id': 'song', 'mood': 'calm', 'file': 'calm/song.mp3', 'title': 'Song'}]}
            (lib / music_library.INDEX_NAME).write_text(json.dumps(index), encoding='utf-8')
            before = (lib / music_library.INDEX_NAME).read_text(encoding='utf-8')
            self.assertIsNone(music_library.library_file(lib, 'calm/song.mp3'))
            with self.assertRaises(music_library.MusicError):
                music_library.remove_track('song', lib)
            self.assertEqual((originals / 'song.mp3').read_bytes(), b'original')
            self.assertEqual((lib / music_library.INDEX_NAME).read_text(encoding='utf-8'), before)
            self.assertFalse((lib / music_library.TRASH_DIR).exists())

    def test_removal_refuses_a_trash_folder_outside_the_library(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outside, lib = root / 'elsewhere', root / 'library'
            outside.mkdir()
            (lib / 'calm').mkdir(parents=True)
            (lib / 'calm/song.mp3').write_bytes(b'track')
            make_link(self, (lib / music_library.TRASH_DIR), outside, directory=True)
            index = {'version': 1, 'moods': {}, 'trash': [],
                     'tracks': [{'id': 'song', 'mood': 'calm', 'file': 'calm/song.mp3', 'title': 'Song'}]}
            (lib / music_library.INDEX_NAME).write_text(json.dumps(index), encoding='utf-8')
            before = (lib / music_library.INDEX_NAME).read_text(encoding='utf-8')
            with self.assertRaises(music_library.MusicError):
                music_library.remove_track('song', lib)
            self.assertEqual((lib / 'calm/song.mp3').read_bytes(), b'track')
            self.assertEqual((lib / music_library.INDEX_NAME).read_text(encoding='utf-8'), before)
            self.assertEqual(list(outside.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
