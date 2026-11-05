#!/usr/bin/env python3
"""Local editonny entry point; works from any working directory."""
from pathlib import Path
import runpy
import sys

if __name__ == '__main__':
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors=stream.errors)
    runpy.run_path(str(Path(__file__).resolve().parent / 'skills/editonny/scripts/editonny.py'), run_name='__main__')
