import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import zipfile
from workflow_fixtures import make_link

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('package', ROOT / 'scripts/package.py')
package = importlib.util.module_from_spec(spec)
spec.loader.exec_module(package)


class PackageTests(unittest.TestCase):
    def test_plugin_release_metadata_matches_the_portable_skill(self):
        version = (ROOT / 'skills/editonny/VERSION').read_text(encoding='utf-8').strip()
        for name in ('.codex-plugin/plugin.json', '.claude-plugin/plugin.json'):
            self.assertEqual(json.loads((ROOT / name).read_text(encoding='utf-8'))['version'], version, name)
        marketplace = json.loads((ROOT / '.claude-plugin/marketplace.json').read_text(encoding='utf-8'))
        self.assertEqual(marketplace['plugins'][0]['version'], version)

    def test_packages_reject_symlinked_source_roots(self):
        with tempfile.TemporaryDirectory(prefix='editonny package roots ') as temporary:
            base = Path(temporary)
            root, outside = base / 'project', base / 'private'
            root.mkdir()
            (outside / 'editonny').mkdir(parents=True)
            (outside / 'editonny/VERSION').write_text('1.0.0', encoding='utf-8')
            (outside / 'private-notes.txt').write_text('Private content must stay outside the package', encoding='utf-8')
            make_link(self, (root / 'skills'), outside, directory=True)
            with self.assertRaisesRegex(ValueError, 'symlink'):
                package.build(root, root / 'dist', 'plugin')
            self.assertFalse(list((root / 'dist').glob('*.zip')))

    def test_plugin_rejects_symlinked_readme_art(self):
        with tempfile.TemporaryDirectory(prefix='editonny readme package ') as temporary:
            root = Path(temporary) / 'project'
            source = root / 'skills/editonny'
            source.mkdir(parents=True)
            (source / 'VERSION').write_text('1.0.0', encoding='utf-8')
            outside = Path(temporary) / 'private'
            outside.mkdir()
            (outside / 'note.txt').write_text('Must not enter the public package', encoding='utf-8')
            assets = root / 'docs/assets'
            assets.mkdir(parents=True)
            make_link(self, (assets / 'readme'), outside, directory=True)
            with self.assertRaisesRegex(ValueError, 'symlink'):
                package.build(root, root / 'dist', 'plugin')
            self.assertFalse(list((root / 'dist').glob('*.zip')))

    def test_site_archives_are_no_longer_built(self):
        with tempfile.TemporaryDirectory(prefix='editonny package kinds ') as temp:
            root = Path(temp)
            (root / 'skills/editonny').mkdir(parents=True)
            (root / 'skills/editonny/VERSION').write_text('1.0.0', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'plugin'):
                package.build(root, root / 'dist', 'site')

    def test_plugin_excludes_nested_secrets_and_development_artifacts(self):
        excluded = ['private.pem', 'private.key', 'private.p12', '.env', '.env.local',
                    'debug.log', 'venv/private.txt', '.venv/private.txt', '.pytest_cache/state',
                    '.ruff_cache/state', '__pycache__/module.pyc', 'module.pyo', '.DS_Store',
                    'node_modules/package/index.js', '.work/private.txt']
        with tempfile.TemporaryDirectory(prefix='editonny package exclusions ') as temp:
            root = Path(temp)
            source = root / 'skills/editonny'
            source.mkdir(parents=True)
            (source / 'VERSION').write_text('1.0.0', encoding='utf-8')
            included = ['skills/editonny/SKILL.md', 'skills/editonny/engine/main.py', 'skills/editonny/.env.example',
                        '.codex-plugin/plugin.json', '.claude-plugin/plugin.json',
                        '.agents/plugins/marketplace.json', 'docs/assets/readme/hero.svg']
            rejected = [f'{folder}/nested/{name}' for folder in
                        ['skills/editonny', '.codex-plugin', '.claude-plugin', '.agents/plugins', 'docs/assets/readme']
                        for name in excluded]
            for name in included + rejected:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('fixture', encoding='utf-8')
            with zipfile.ZipFile(package.build(root, root / 'dist', 'plugin')) as archive:
                self.assertEqual(set(archive.namelist()), {*included, 'skills/editonny/VERSION'})

    def test_plugin_package_contents_exclude_the_web_app_and_local_secrets(self):
        with tempfile.TemporaryDirectory(prefix='editonny package ') as temp:
            root = Path(temp)
            for name in ['web/package.json', 'web/src/app/[locale]/page.tsx', 'web/messages/en-US.json', 'web/.env', 'web/.next/server.js',
                         'skills/editonny/SKILL.md', 'skills/editonny/VERSION',
                         'skills/editonny/engine/.env', 'skills/editonny/engine/.venv/private',
                         'skills/editonny/old-prompts.pdf', 'skills/editonny/clip.MP4',
                         'skills/editonny/visuals/clip.webm', 'skills/editonny/visuals/still.png',
                         '.codex-plugin/plugin.json', '.claude-plugin/plugin.json',
                         'install.py', 'editonny.py', 'README.md', 'README.pt-BR.md', 'README.es.md', 'LICENSE.md',
                         'PRODUCT.md', 'DESIGN.md', 'design-system/README.md', 'docs/architecture.md', 'docs/design.md', 'docs/setup.md',
                         'docs/installation.md', 'docs/archive/private.pdf',
                         'docs/assets/readme/hero.svg', 'docs/assets/readme/captions.gif',
                         'docs/assets/readme/.env', 'docs/assets/private.txt']:
                p = root / name
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text('1.0.0' if p.name == 'VERSION' else 'fixture', encoding='utf-8')
            plugin = package.build(root, root / 'dist', 'plugin')
            with zipfile.ZipFile(plugin) as archive:
                self.assertIn('skills/editonny/SKILL.md', archive.namelist())
                self.assertIn('install.py', archive.namelist())
                self.assertIn('DESIGN.md', archive.namelist())
                self.assertTrue(all(not p.startswith('design-system/') for p in archive.namelist()))
                self.assertIn('README.pt-BR.md', archive.namelist())
                self.assertIn('README.es.md', archive.namelist())
                self.assertIn('PRODUCT.md', archive.namelist())
                self.assertIn('docs/architecture.md', archive.namelist())
                self.assertIn('docs/assets/readme/hero.svg', archive.namelist())
                self.assertIn('docs/assets/readme/captions.gif', archive.namelist())
                self.assertNotIn('docs/assets/private.txt', archive.namelist())
                self.assertTrue(all(not p.startswith('web/') for p in archive.namelist()))
                self.assertIn('skills/editonny/visuals/still.png', archive.namelist())
                self.assertIn('skills/editonny/clip.MP4', archive.namelist())
                self.assertIn('skills/editonny/visuals/clip.webm', archive.namelist())
                self.assertTrue(all(not p.lower().endswith('.pdf') for p in archive.namelist()))
                self.assertTrue(all('.env' not in p and '.venv' not in p and 'archive/' not in p for p in archive.namelist()))


if __name__ == '__main__':
    unittest.main()
