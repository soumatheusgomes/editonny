import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import graph_runtime as graph
from workflow_fixtures import make_link


class GraphWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='editonny graph ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / 'guide.md'
        self.source.write_text('A guide about editing videos.', encoding='utf-8')
        self.files = {str(self.source): graph.digest(self.source)}
        self.fragment = {
            'provenance': {'provider': 'native-codex', 'auth': 'chatgpt', 'model': 'gpt-6-astra'},
            'nodes': [{'id': 'guide_editor', 'label': 'Editor', 'file_type': 'concept',
                       'source_file': str(self.source)}],
            'edges': [], 'hyperedges': [],
        }

    def test_native_result_requires_complete_current_sources_and_model(self):
        graph.validate_fragment(self.fragment, self.files, self.root)
        for field, value in [('provider', 'claude'), ('auth', 'api-key'), ('model', 'cheap-model')]:
            altered = json.loads(json.dumps(self.fragment))
            altered['provenance'][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                graph.validate_fragment(altered, self.files, self.root)
        with self.assertRaises(ValueError):
            graph.validate_fragment({**self.fragment, 'nodes': []}, self.files, self.root)
        self.source.write_text('Changed during extraction', encoding='utf-8')
        with self.assertRaises(ValueError):
            graph.validate_fragment(self.fragment, self.files, self.root)

    def test_rejects_dangling_edges_invalid_confidence_and_external_sources(self):
        edge = {'source': 'guide_editor', 'target': 'missing', 'relation': 'references',
                'source_file': str(self.source), 'confidence': 'EXTRACTED', 'confidence_score': 1.0}
        with self.assertRaises(ValueError):
            graph.validate_fragment({**self.fragment, 'edges': [edge]}, self.files, self.root)
        edge.update(target='guide_editor', confidence_score=0.5)
        with self.assertRaises(ValueError):
            graph.validate_fragment({**self.fragment, 'edges': [edge]}, self.files, self.root)
        self.fragment['nodes'][0]['source_file'] = '/tmp/private.env'
        with self.assertRaises(ValueError):
            graph.validate_fragment(self.fragment, self.files, self.root)

    def test_refuses_symlink_source_and_secret(self):
        with self.subTest(kind='symlink'):
            link = self.root / 'link.md'
            make_link(self, link, self.source)
            with self.assertRaises(ValueError):
                graph.source_path(str(link), self.root)
        for name in ['.env', 'credentials.json', 'nested/.env.local']:
            with self.subTest(name=name), self.assertRaises(ValueError):
                graph.source_path(name, self.root)

    def test_promotion_restores_previous_graph_if_rename_fails(self):
        output = self.root / 'graphify-out'
        stage = self.root / '.work/graphify'
        output.mkdir(); stage.mkdir(parents=True)
        (output / 'graph.json').write_text('old valid graph', encoding='utf-8')
        before = graph.digest(output / 'graph.json')
        (stage / 'graph.json').write_text('new graph', encoding='utf-8')
        original = Path.replace

        def fail_stage(path, target):
            if path == stage:
                raise OSError('simulated promotion failure')
            return original(path, target)

        with patch.object(Path, 'replace', fail_stage), self.assertRaises(OSError):
            graph.promote(stage, output)
        self.assertEqual(before, graph.digest(output / 'graph.json'))

    def test_subprocess_boundary_keeps_paths_as_arguments(self):
        with patch('graph_runtime.subprocess.run') as run:
            run.return_value.returncode = 0
            graph.run_native(['python with spaces', str(self.source)], self.root, {})
        args, kwargs = run.call_args
        self.assertEqual(args[0], ['python with spaces', str(self.source)])
        self.assertFalse(kwargs.get('shell', False))

    def test_promotion_preserves_memories_changed_at_the_swap(self):
        original = Path.replace
        for change in ['added', 'modified', 'deleted']:
            with self.subTest(change=change):
                output = self.root / change / 'graphify-out'
                stage = self.root / change / '.work/graphify'
                for directory, content in [(output, 'old graph'), (stage, 'new graph')]:
                    (directory / 'memory').mkdir(parents=True)
                    (directory / 'graph.json').write_text(content, encoding='utf-8')
                    (directory / 'memory/answer.md').write_text('original answer', encoding='utf-8')
                memory = output / 'memory/answer.md'
                added = output / 'memory/new.md'

                def save_during_swap(path, target):
                    if path == output:
                        if change == 'added':
                            added.write_text('new answer', encoding='utf-8')
                        elif change == 'modified':
                            memory.write_text('updated answer', encoding='utf-8')
                        else:
                            memory.unlink()
                    return original(path, target)

                with patch.object(Path, 'replace', save_during_swap), self.assertRaisesRegex(ValueError, 'Memories changed'):
                    graph.promote(stage, output)
                # The new graph is live and the late memory change survives in the backup.
                backup = stage.parent / 'graphify-previous'
                self.assertEqual((output / 'graph.json').read_text(encoding='utf-8'), 'new graph')
                if change == 'added':
                    self.assertEqual((backup / 'memory/new.md').read_text(encoding='utf-8'), 'new answer')
                elif change == 'modified':
                    self.assertEqual((backup / 'memory/answer.md').read_text(encoding='utf-8'), 'updated answer')
                else:
                    self.assertFalse((backup / 'memory/answer.md').exists())

    def test_memory_saved_after_the_swap_stays_live(self):
        output = self.root / 'graphify-out'
        stage = self.root / '.work/graphify'
        for directory in (output, stage):
            (directory / 'memory').mkdir(parents=True)
            (directory / 'memory/answer.md').write_text('verified answer', encoding='utf-8')
        original = Path.replace

        def save_after_swap(path, target):
            result = original(path, target)
            if path == stage:
                (output / 'memory/new.md').write_text('saved after promotion', encoding='utf-8')
            return result

        with patch.object(Path, 'replace', save_after_swap):
            graph.promote(stage, output)
        self.assertEqual((output / 'memory/new.md').read_text(encoding='utf-8'), 'saved after promotion')
        self.assertFalse((stage.parent / 'graphify-previous').exists())

    def test_promotion_keeps_unchanged_memory(self):
        output = self.root / 'graphify-out'
        stage = self.root / '.work/graphify'
        for directory, content in [(output, 'old graph'), (stage, 'new graph')]:
            (directory / 'memory').mkdir(parents=True)
            (directory / 'graph.json').write_text(content, encoding='utf-8')
            (directory / 'memory/answer.md').write_text('verified answer', encoding='utf-8')
        graph.promote(stage, output)
        self.assertEqual((output / 'graph.json').read_text(encoding='utf-8'), 'new graph')
        self.assertEqual((output / 'memory/answer.md').read_text(encoding='utf-8'), 'verified answer')
        self.assertFalse(stage.exists())
        self.assertFalse((stage.parent / 'graphify-previous').exists())

    def test_unmodified_snapshot_has_no_pending_files(self):
        self.assertTrue(graph.unchanged(self.files, self.files))
        self.assertFalse(graph.unchanged(self.files, {}))
        self.assertFalse(graph.unchanged(self.files, {**self.files, 'new.md': 'hash'}))

    def test_tool_environment_cannot_select_external_llm(self):
        original = {'PATH': '/bin', 'GRAPHIFY_BACKEND': 'claude', 'OPENAI_API_KEY': 'synthetic'}
        safe = graph.subscription_env(self.root, original)
        self.assertEqual(safe, {'PATH': '/bin', 'GRAPHIFY_NO_AUTO_REFRESH': '1'})
        self.assertEqual(original['GRAPHIFY_BACKEND'], 'claude')


if __name__ == '__main__':
    unittest.main()
