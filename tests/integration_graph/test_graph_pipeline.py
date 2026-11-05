"""Run with isolated Graphify Python; fixtures use neither an LLM nor network."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[2] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
from graph_runtime import PROVENANCE, digest, extraction_prompt, read_json, write_json


class GraphPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='editonny graph pipeline ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / 'one.md').write_text('One calls the shared editor.', encoding='utf-8')
        (self.root / 'two.md').write_text('Two reviews the resulting video.', encoding='utf-8')
        (self.root / 'sample.py').write_text('def edit():\n    return 1\n', encoding='utf-8')
        (self.root / '.env').write_text('PRIVATE_SENTINEL=do-not-index', encoding='utf-8')
        (self.root / '.graphifyignore').write_text('.work/\ngraphify-out/\n.env\n', encoding='utf-8')
        self.stage = self.root / '.work/graphify'
        self.output = self.root / 'graphify-out'
        self.output.mkdir()
        self.prompt = self.root / '.work/extraction-spec.md'
        self.prompt.parent.mkdir()
        self.prompt.write_text(extraction_prompt().read_text(encoding='utf-8'), encoding='utf-8')
        write_json(self.output / 'graph.json', {'nodes': [{'id': 'old'}], 'edges': []})

    def update(self, watch=False):
        env = dict(os.environ, GRAPHIFY_OUT=str(self.stage), GRAPHIFY_NO_AUTO_REFRESH='1')
        code = 'import sys,json;from pathlib import Path;sys.path.insert(0,sys.argv[1]);import graph_runtime as runtime;runtime.extraction_prompt=lambda:Path(sys.argv[3]);'
        if watch:
            code += 'import graphify.watch as watcher;watcher.watch=lambda root:watcher._rebuild_code(root);runtime.run_watch(Path(sys.argv[2]))'
        else:
            code += 'print(json.dumps(runtime.run_update(Path(sys.argv[2]))))'
        result = subprocess.run([sys.executable, '-c', code, str(SCRIPTS), str(self.root), str(self.prompt)],
                                env=env, capture_output=True, text=True, timeout=90, encoding='utf-8', errors='replace')
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        return json.loads(result.stdout.splitlines()[-1])

    def complete(self):
        result = self.update()
        if result.get('phase') == 'semantic':
            for request_path in read_json(self.stage / 'pending.json')['requests']:
                request = read_json(request_path)
                nodes, edges = [], []
                for source in request['files']:
                    stem = Path(source).stem
                    for suffix in ['a', 'b']:
                        nodes.append({'id': f'{stem}_{suffix}', 'label': f'{stem} {suffix}',
                                      'source_file': source, 'file_type': 'concept'})
                    for relation in ['references', 'implements']:
                        edges.append({'source': f'{stem}_a', 'target': f'{stem}_b', 'relation': relation,
                                      'confidence': 'EXTRACTED', 'confidence_score': 1.0, 'source_file': source})
                write_json(request['result'], {'provenance': PROVENANCE, 'nodes': nodes, 'edges': edges})
            result = self.update()
        if result.get('phase') == 'labels':
            request = read_json(self.stage / 'labels-request.json')
            write_json(self.stage / 'labels-response.json', {
                'signature': request['signature'], 'provenance': PROVENANCE,
                'labels': {key: 'Fixture local' for key in request['communities']}})
            result = self.update()
        self.assertEqual(result['status'], 'updated', result)
        return read_json(self.output / 'graph.json')

    def test_complete_cache_direction_and_parallel_relations(self):
        before = digest(self.output / 'graph.json')
        self.assertEqual(self.update()['status'], 'pending')
        self.assertEqual(before, digest(self.output / 'graph.json'))
        data = self.complete()
        self.assertTrue(data['directed'])
        from graphify.serve import _load_graph
        from graphify.export import to_graphml
        import xml.etree.ElementTree as ET
        loaded = _load_graph(str(self.output / 'graph.json'))
        self.assertEqual(loaded.number_of_edges(), len(data['links']))
        import networkx as nx
        to_graphml(nx.DiGraph(loaded), {}, str(self.output / 'graph.graphml'))
        xml = ET.parse(self.output / 'graph.graphml')
        self.assertEqual(len(xml.findall('.//{http://graphml.graphdrawing.org/xmlns}edge')),
                         len({(e['source'], e['target']) for e in data['links']}))
        edges = data.get('links', data.get('edges'))
        pair = [e for e in edges if e['source'] == 'one_a' and e['target'] == 'one_b']
        self.assertEqual({e['relation'] for e in pair}, {'references', 'implements'})
        self.assertFalse(any(e['source'] == 'one_b' and e['target'] == 'one_a' for e in edges))
        before = digest(self.output / 'graph.json')
        export = self.output / 'graph.html'
        export.write_text('export from the current graph', encoding='utf-8')
        (self.output / 'needs_update').write_text('1', encoding='utf-8')
        self.assertEqual(self.update()['status'], 'unchanged')
        self.assertTrue(export.exists())
        self.assertFalse((self.output / 'needs_update').exists())
        self.assertFalse((self.stage / 'needs_update').exists())
        self.assertEqual(before, digest(self.output / 'graph.json'))
        self.assertNotIn('PRIVATE_SENTINEL', json.dumps(data))

    def test_rename_delete_and_ignore_prune_old_semantics(self):
        self.complete()
        (self.output / 'graph.html').write_text('stale export', encoding='utf-8')
        (self.root / 'one.md').rename(self.root / 'renamed.md')
        (self.root / 'two.md').unlink()
        data = self.complete()
        self.assertFalse((self.output / 'graph.html').exists())
        sources = {n.get('source_file') for n in data['nodes']}
        self.assertIn('renamed.md', sources)
        self.assertNotIn('one.md', sources)
        self.assertNotIn('two.md', sources)
        (self.root / '.graphifyignore').write_text('.work/\ngraphify-out/\n.env\nrenamed.md\n', encoding='utf-8')
        data = self.complete()
        self.assertNotIn('renamed.md', {n.get('source_file') for n in data['nodes']})

    def test_prompt_change_invalidates_completed_cache_and_pending_response(self):
        self.complete()
        self.prompt.write_text(self.prompt.read_text(encoding='utf-8') + '\nNew extraction contract.\n', encoding='utf-8')
        self.assertEqual(self.update()['phase'], 'semantic')
        request_path = read_json(self.stage / 'pending.json')['requests'][0]
        request = read_json(request_path)
        # A valid old-prompt result must not be relabeled with the new fingerprint.
        write_json(request['result'], {'provenance': PROVENANCE, 'nodes': [
            {'id': f'old_result_{i}', 'label': 'Old prompt', 'source_file': source, 'file_type': 'concept'}
            for i, source in enumerate(request['files'])], 'edges': []})
        self.prompt.write_text(self.prompt.read_text(encoding='utf-8') + '\nAnother contract change.\n', encoding='utf-8')
        self.assertEqual(self.update()['phase'], 'semantic')
        self.assertNotIn('old_result', json.dumps(read_json(self.output / 'graph.json')))

    def test_watch_preserves_parallel_relations_and_invalidates_exports(self):
        self.complete()
        for name in ['graph.html', 'GRAPH_TREE.html', 'CALLFLOW.html', 'graph.svg', 'graph.graphml', 'cypher.txt']:
            (self.output / name).write_text('old derived output', encoding='utf-8')
        flag = self.output / 'needs_update'
        flag.write_text('1', encoding='utf-8')
        (self.root / 'sample.py').write_text('def edit():\n    return 2\ndef added():\n    return edit()\n', encoding='utf-8')
        self.assertEqual(self.update(watch=True)['status'], 'updated')
        data = read_json(self.output / 'graph.json')
        pair = [e for e in data['links'] if e['source'] == 'one_a' and e['target'] == 'one_b']
        self.assertEqual({e['relation'] for e in pair}, {'references', 'implements'})
        self.assertTrue(any('added' in n['id'] for n in data['nodes']))
        self.assertFalse((self.output / 'GRAPH_TREE.html').exists())
        self.assertEqual(read_json(self.output / 'run.json')['pending_semantic'], [])
        self.assertTrue(flag.exists())
        self.complete()
        self.assertFalse(flag.exists())

    def test_missing_and_stale_results_leave_published_graph_unchanged(self):
        self.complete()
        before = digest(self.output / 'graph.json')
        (self.root / 'one.md').write_text('Changed document requires another extraction.', encoding='utf-8')
        self.assertEqual(self.update()['phase'], 'semantic')
        self.assertEqual(before, digest(self.output / 'graph.json'))
        request_path = read_json(self.stage / 'pending.json')['requests'][0]
        request = read_json(request_path)
        write_json(request['result'], {'provenance': PROVENANCE, 'nodes': [], 'edges': []})
        (self.root / 'one.md').write_text('Changed again before the result arrived.', encoding='utf-8')
        self.assertEqual(self.update()['phase'], 'semantic')
        self.assertEqual(before, digest(self.output / 'graph.json'))

    def test_memory_sources_survive_promotion_and_changes_during_pending_work(self):
        memory = self.output / 'memory'
        memory.mkdir()
        first = memory / 'first.md'
        first.write_text('Verified answer about the editor.', encoding='utf-8')
        data = self.complete()
        self.assertIn('graphify-out/memory/first.md', {n.get('source_file') for n in data['nodes']})
        self.assertTrue(first.is_file())
        self.assertEqual(self.update()['status'], 'unchanged')
        # An unchanged check leaves staging; new answers must still be read from output.
        first.unlink()
        second = memory / 'second.md'
        second.write_text('Another verified answer about Motion.', encoding='utf-8')
        data = self.complete()
        self.assertIn('graphify-out/memory/second.md', {n.get('source_file') for n in data['nodes']})
        self.assertNotIn('graphify-out/memory/first.md', {n.get('source_file') for n in data['nodes']})
        self.assertFalse(first.exists())
        self.assertTrue(second.is_file())
        self.assertEqual(self.update()['status'], 'unchanged')

    def test_memory_saved_during_copy_blocks_promotion_without_data_loss(self):
        self.complete()
        self.assertEqual(self.update()['status'], 'unchanged')
        memory = self.output / 'memory/answer.md'
        memory.parent.mkdir()
        memory.write_text('Old verified answer.', encoding='utf-8')
        before = digest(self.output / 'graph.json')
        code = '''
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import graph_runtime as runtime
root = Path(sys.argv[2])
runtime.extraction_prompt = lambda: Path(sys.argv[3])
original = runtime.shutil.copytree
def save_during_copy(source, target, *args, **kwargs):
    result = original(source, target, *args, **kwargs)
    if Path(source) == root / 'graphify-out/memory':
        (Path(source) / 'answer.md').write_text('New verified answer.')
    return result
runtime.shutil.copytree = save_during_copy
runtime.run_update(root, semantic=False)
'''
        result = subprocess.run([sys.executable, '-c', code, str(SCRIPTS), str(self.root), str(self.prompt)],
                                env=dict(os.environ, GRAPHIFY_OUT=str(self.stage)),
                                capture_output=True, text=True, timeout=90, encoding='utf-8', errors='replace')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Memories changed', result.stderr)
        self.assertEqual(memory.read_text(encoding='utf-8'), 'New verified answer.')
        self.assertEqual(before, digest(self.output / 'graph.json'))


if __name__ == '__main__':
    unittest.main()
