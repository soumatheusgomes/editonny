from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from graph_runtime import export_commands, server_command


class GraphExportTests(unittest.TestCase):
    def test_all_exports_are_local_and_preserve_argument_boundaries(self):
        with tempfile.TemporaryDirectory(prefix='graph export ') as temp:
            root = Path(temp).resolve()
            commands = export_commands(root, '/python with spaces')
            self.assertEqual(len(commands), 8)
            for command in commands:
                self.assertEqual(command[:3], ['/python with spaces', '-m', 'graphify'])
                name = '.graphify-graphml.json' if 'graphml' in command else 'graph.json'
                self.assertIn(str(root / 'graphify-out' / name), command)
                self.assertNotIn('--push', command)
                self.assertFalse(any('bolt://' in part or 'http://' in part for part in command))

    def test_mcp_uses_absolute_graph_and_stdio(self):
        root = Path(Path.cwd().anchor, 'project with spaces')
        command = server_command(root, '/tool/python')
        self.assertEqual(command, ['/tool/python', '-m', 'graphify.serve',
                                  str(root / 'graphify-out' / 'graph.json'), '--transport', 'stdio'])


if __name__ == '__main__':
    unittest.main()
