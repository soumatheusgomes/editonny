#!/usr/bin/env python3
"""Check the local MCP protocol and close its process; never calls an LLM."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys

from graph_runtime import interpreter, run_native, server_command, subscription_env

ROOT = Path(__file__).resolve().parents[1]


async def probe(command, root, question):
    from mcp import Client, StdioServerParameters
    env = subscription_env(root, os.environ)
    env['GRAPHIFY_OUT'] = str(root / 'graphify-out')
    server = StdioServerParameters(command=command[0], args=command[1:], cwd=str(root), env=env)
    async with asyncio.timeout(60), Client(server) as client:
        tools = await client.list_tools()
        names = {tool.name for tool in tools.tools}
        assert {'graph_stats', 'query_graph'} <= names, names
        for name, arguments in [('graph_stats', {}), ('query_graph', {'question': question, 'token_budget': 1000})]:
            result = await client.call_tool(name, arguments)
            assert not result.is_error and result.content, (name, result)
        resources = await client.list_resources()
        assert 'graphify://report' in {str(item.uri) for item in resources.resources}
        report = await client.read_resource('graphify://report')
        assert report.contents and report.contents[0].text.strip()
        return {'protocol': client.protocol_version, 'tools': sorted(names), 'report': True, 'query': True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT, help='Optional fixture root')
    parser.add_argument('--question', default='createPrompt')
    args = parser.parse_args()
    python = interpreter()
    if Path(sys.prefix) != Path(python).parent.parent:
        return run_native([python, str(Path(__file__).resolve()), *sys.argv[1:]], ROOT,
                          subscription_env(ROOT, os.environ)).returncode
    root = args.root.resolve()
    command = ([python, str(ROOT / 'scripts/project_graph.py'), 'serve'] if root == ROOT
               else server_command(root, python))
    print(json.dumps(asyncio.run(probe(command, root, args.question)), ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors=stream.errors)
    raise SystemExit(main())
