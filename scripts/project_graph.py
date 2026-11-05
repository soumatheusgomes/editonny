#!/usr/bin/env python3
"""Local index: prepare in the terminal, complete semantic tasks in the native Codex session."""
from __future__ import annotations

import argparse
from importlib.metadata import version
import json
import os
from pathlib import Path
import sys

from graph_runtime import VERSION, interpreter, run_native, server_command, subscription_env, verify_graph

ROOT = Path(__file__).resolve().parents[1]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['check', 'update', 'code', 'exports', 'serve', 'watch', 'test'])
    args = parser.parse_args(argv)
    python = interpreter()
    env = subscription_env(ROOT, os.environ)
    env['GRAPHIFY_OUT'] = str(ROOT / ('.work/graphify' if args.command in {'update', 'code'} else 'graphify-out'))
    if Path(sys.prefix) != Path(python).parent.parent:
        return run_native([python, str(Path(__file__).resolve()), args.command], ROOT, env).returncode
    os.environ.clear()
    os.environ.update(env)
    if version('graphifyy') != VERSION:
        raise ValueError(f'This workflow requires Graphifyy {VERSION}')
    if args.command == 'serve':
        os.execve(python, server_command(ROOT, python), env)
    if args.command == 'test':
        import unittest
        suite = unittest.defaultTestLoader.discover(str(ROOT / 'tests/integration_graph'))
        return 0 if unittest.TextTestRunner().run(suite).wasSuccessful() else 1
    if args.command == 'watch':
        from graph_runtime import run_watch
        run_watch(ROOT)
        return 0
    if args.command in {'update', 'code'}:
        from graph_runtime import run_update
        result = run_update(ROOT, semantic=args.command == 'update')
    elif args.command == 'exports':
        from graph_runtime import run_exports
        result = run_exports(ROOT)
    else:
        result = {'version': VERSION, 'python': python, **verify_graph(ROOT / 'graphify-out/graph.json', ROOT)}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result.get('status') == 'pending':
        print('In Codex: complete the listed tasks with native gpt-6-astra/ChatGPT agents, then repeat graph:update.', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors=stream.errors)
    try:
        raise SystemExit(main())
    except (ValueError, OSError) as error:
        print(f'Graphify: {error}', file=sys.stderr)
        raise SystemExit(1)
