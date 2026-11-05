"""Files that are only in iCloud (macOS "dataless"): check before opening them.

On a Mac with "Optimize Mac Storage", a file in iCloud Drive (Desktop and Documents too) can be only a
placeholder: `ls -lO` shows `compressed,dataless`. ffprobe and ffmpeg then hang for minutes without any
message while macOS tries to fetch it, and an ffmpeg that is killed leaves "moov atom not found". Seen on
29/09/2026 with the BODIES/*.MP4 of ~/Documents.

`garantir_local(paths)` asks macOS to download every such file (`brctl download`), waits a little and, if
something is still in the cloud, raises `CloudOnly` with an English message explaining the next step. On other
systems (or without the flag) it does nothing.

Usage from the command line (the transcribe helpers already call it):
    uv run --directory '<SKILL>' python helpers/icloud.py <file or folder> [...] [--wait 90] [--no-download]
Exit code 0 when everything is on the disk, 1 when something is still only in iCloud.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

SF_DATALESS = 0x40000000     # <sys/stat.h>: the file content is not on the disk (iCloud placeholder)
WAIT_S = 90.0              # how long to wait for iCloud before giving up
STEP_S = 2.0


class CloudOnly(RuntimeError):
    """One or more files are still only in iCloud."""

    def __init__(self, files_value: list[Path], attempted: bool):
        self.files_value = files_value
        super().__init__(message_value(files_value, attempted))


def only_in_cloud(path: Path | str) -> bool:
    """True when the file is an iCloud placeholder (dataless) on macOS."""
    try:
        flags = getattr(os.stat(path), "st_flags", 0) or 0
    except OSError:
        return False
    return bool(flags & SF_DATALESS)


def in_cloud(paths) -> list[Path]:
    return [Path(p) for p in paths if Path(p).is_file() and only_in_cloud(p)]


def message_value(files_value: list[Path], attempted: bool) -> str:
    names_value = "\n".join(f'  {p}' for p in files_value[:12]) + (f'\n  (+{len(files_value) - 12})' if len(files_value) > 12 else "")
    request_value = ('Requested the download from macOS; it has not finished.' if attempted
              else 'No media was opened because FFmpeg can stall on cloud-only placeholders.')
    return (f'{len(files_value)} files remain cloud-only in iCloud rather than on this Mac:{names_value}\n{request_value}Download files in Finder using Download Now and wait for the cloud icon to disappear, or use brctl download with a quoted file path. Then retry. Alternatively copy the videos outside iCloud.')


def request_download(paths: list[Path]) -> bool:
    brctl = shutil.which("brctl")
    if not brctl:
        return False
    ok = False
    for p in paths:
        try:
            r = subprocess.run([brctl, "download", str(p)], capture_output=True, text=True, timeout=30, encoding='utf-8', errors='replace')
            ok |= r.returncode == 0
        except (OSError, subprocess.SubprocessError):
            pass
    return ok


def ensure_local(paths, download_value: bool = True, wait_value: float = WAIT_S, notify=print) -> None:
    """Raise CloudOnly if any file is still only in iCloud after asking for it (at most `wait` seconds)."""
    if sys.platform != "darwin":
        return
    missing_value = in_cloud(paths)
    if not missing_value:
        return
    if not download_value:
        raise CloudOnly(missing_value, attempted=False)
    notify(f'{len(missing_value)} cloud-only files, for example {missing_value[0].name}; requesting download and waiting up to {wait_value:.0f} s...')
    attempted = request_download(missing_value)
    if not attempted:
        raise CloudOnly(missing_value, attempted=False)
    end_value = time.monotonic() + wait_value
    while missing_value and time.monotonic() < end_value:
        time.sleep(STEP_S)
        missing_value = [p for p in missing_value if only_in_cloud(p)]
    if missing_value:
        raise CloudOnly(missing_value, attempted=True)
    notify("Downloaded from iCloud; continuing.")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description='Check whether files are local or iCloud-only, and optionally request download.')
    ap.add_argument('paths_value', nargs="+", type=Path, help='files or folders; does not descend into subfolders')
    ap.add_argument("--wait", type=float, default=WAIT_S, help=f'seconds to wait for iCloud (default {WAIT_S:g})', dest='wait_value')
    ap.add_argument("--no-download", action="store_true", help='check only; do not request downloads', dest='nao_download')
    a = ap.parse_args(argv)
    files_value: list[Path] = []
    for c in a.paths_value:
        files_value += sorted(p for p in c.iterdir() if p.is_file()) if c.is_dir() else [c]
    try:
        ensure_local(files_value, download_value=not a.nao_download, wait_value=a.wait_value)
    except CloudOnly as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f'{len(files_value)} local files; none are iCloud-only')
    return 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors=stream.errors)
    raise SystemExit(main())
