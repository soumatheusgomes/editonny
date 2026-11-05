import importlib.util
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from workflow_fixtures import make_link

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('installer', ROOT / 'install.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class InstallationTests(unittest.TestCase):
    def test_install_includes_videos_images_and_manifest(self):
        folder = self.source / 'visuals/assets/visuals/video'
        folder.mkdir(parents=True)
        for name in ('clip.MP4', 'object.webm', 'still.png'):
            (folder / name).write_bytes(b'fixture')
        (self.source / 'visuals/media.json').write_text('{}', encoding='utf-8')
        dest = self.install()
        self.assertTrue((dest / 'visuals/media.json').is_file())
        self.assertEqual({p.name for p in (dest / folder.relative_to(self.source)).iterdir()},
                         {'clip.MP4', 'object.webm', 'still.png'})

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='editonny test ')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.source = self.base / 'source'
        self.source.mkdir()
        (self.source / 'SKILL.md').write_text('---\nname: editonny\n---\n', encoding='utf-8')
        (self.source / 'VERSION').write_text('1.0.0\n', encoding='utf-8')
        (self.source / 'engine').mkdir()
        (self.source / 'engine' / 'pyproject.toml').write_text('[project]\nname="test"\n', encoding='utf-8')
        (self.source / '.env').write_text('PRIVATE=never-copy\n', encoding='utf-8')
        (self.source / '.venv').mkdir()
        (self.source / '.venv' / 'private').write_text('not shipped', encoding='utf-8')
        self.home = self.base / 'user home'
        # The installer prints progress; capture it so the test runner's console encoding does not matter.
        output = patch('sys.stdout', new_callable=io.StringIO)
        output.start()
        self.addCleanup(output.stop)

    def install(self, **kwargs):
        return installer.install(self.source, self.home, sync=False, **kwargs)

    def test_single_copy_and_two_links_idempotent(self):
        dest = self.install()
        self.assertEqual(dest, self.home / '.local/share/editonny')
        for p in ['.agents/skills/editonny', '.claude/skills/editonny']:
            self.assertTrue(installer.is_link(self.home / p))
            self.assertEqual((self.home / p).resolve(), dest.resolve())
        self.assertFalse((dest / '.env').exists())
        self.assertFalse((dest / '.venv').exists())
        before = (dest / 'SKILL.md').stat().st_mtime_ns
        self.install()
        self.assertEqual((dest / 'SKILL.md').stat().st_mtime_ns, before)

    def test_target_claude_does_not_register_codex(self):
        self.install(target='claude')
        self.assertFalse((self.home / '.agents').exists())
        self.assertTrue(installer.is_link(self.home / '.claude/skills/editonny'))

    def test_dry_run_creates_nothing(self):
        self.install(dry_run=True)
        self.assertFalse(self.home.exists())

    def test_foreign_link_or_directory_refused_before_copy(self):
        for kind in ['directory', 'symlink']:
            with self.subTest(kind=kind):
                link = self.home / '.claude/skills/editonny'
                link.parent.mkdir(parents=True, exist_ok=True)
                if kind == 'directory':
                    link.mkdir()
                else:
                    make_link(self, link, self.source, directory=True)
                with self.assertRaisesRegex(ValueError, 'belong|unowned|existing'):
                    self.install()
                self.assertFalse((self.home / '.local/share/editonny').exists())
                if installer.is_link(link):
                    link.unlink()
                else:
                    link.rmdir()

    def test_update_keeps_env_and_original_backup(self):
        dest = self.install()
        (dest / 'engine/.env').write_text('LOCAL=keep\n', encoding='utf-8')
        (dest / 'engine/custom.txt').write_text('personal file', encoding='utf-8')
        (self.source / 'VERSION').write_text('1.1.0\n', encoding='utf-8')
        self.install()
        self.assertEqual((dest / 'engine/.env').read_text(encoding='utf-8'), 'LOCAL=keep\n')
        backups = list((self.home / '.local/share/editonny-backups').iterdir())
        self.assertEqual(len(backups), 1)
        self.assertEqual((backups[0] / 'engine/custom.txt').read_text(encoding='utf-8'), 'personal file')

    def test_reinstall_repairs_media_and_preserves_env_and_damaged_backup(self):
        (self.source / 'clip.webm').write_bytes(b'original')
        dest = self.install()
        (dest / 'engine/.env').write_text('LOCAL=keep\n', encoding='utf-8')
        for index, damage in enumerate(('missing', 'corrupt'), 1):
            with self.subTest(damage=damage):
                video = dest / 'clip.webm'
                if damage == 'missing':
                    video.unlink()
                else:
                    video.write_bytes(b'personal edit')
                self.install()
                self.assertTrue(video.is_file())
                self.assertEqual(video.read_bytes(), b'original')
                self.assertEqual((dest / 'engine/.env').read_text(encoding='utf-8'), 'LOCAL=keep\n')
                backups = sorted((self.home / '.local/share/editonny-backups').iterdir())
                self.assertEqual(len(backups), index)
                if damage == 'corrupt':
                    self.assertEqual((backups[-1] / 'clip.webm').read_bytes(), b'personal edit')
        self.install()
        self.assertEqual(len(list((self.home / '.local/share/editonny-backups').iterdir())), 2)

    def test_failed_sync_restores_old_install(self):
        dest = self.install()
        (self.source / 'VERSION').write_text('2.0.0\n', encoding='utf-8')
        with patch.object(installer.shutil, 'which', return_value='/test/uv'), \
                patch.object(installer, 'sync_engine', side_effect=RuntimeError('sync failed')):
            with self.assertRaisesRegex(RuntimeError, 'sync failed'):
                installer.install(self.source, self.home, sync=True)
        self.assertEqual((dest / 'VERSION').read_text(encoding='utf-8'), '1.0.0\n')
        self.assertTrue((self.home / '.claude/skills/editonny').is_dir())

    def test_unregister_preserves_installation_and_secrets(self):
        dest = self.install()
        (dest / '.env').write_text('LOCAL=keep', encoding='utf-8')
        installer.uninstall(self.home, target='both')
        self.assertFalse((self.home / '.agents/skills/editonny').exists())
        self.assertTrue((dest / '.env').exists())

    def test_source_symlink_not_followed(self):
        secret = self.base / 'secret.txt'
        secret.write_text('private', encoding='utf-8')
        make_link(self, (self.source / 'linked.txt'), secret)
        with self.assertRaisesRegex(ValueError, 'link'):
            self.install()


if __name__ == '__main__':
    unittest.main()
