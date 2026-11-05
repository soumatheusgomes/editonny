"""Real preview with synthetic files; never accesses the personal library."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'skills/editonny/engine/helpers'))
import preview_server as preview


@contextmanager
def running_preview(video=False):
    with tempfile.TemporaryDirectory(prefix='editonny preview ') as temp:
        root = Path(temp)
        edit = root / 'edit with spaces'
        edit.mkdir()
        previous = os.environ.get('EDITONNY_MUSIC_LIBRARY')
        os.environ['EDITONNY_MUSIC_LIBRARY'] = str(root / 'library')
        server = None
        try:
            clip = edit / 'cut.mp4'
            if video:
                subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                                'color=c=0x65886c:s=320x240:r=30:d=3', '-f', 'lavfi',
                                '-i', 'sine=frequency=440:duration=3', '-c:v', 'libx264',
                                '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-shortest', str(clip)], check=True)
            else:
                clip.write_bytes(b'synthetic media')
            os.utime(clip, (time.time() - 10, time.time() - 10))
            (edit / 'final.mp4').write_bytes(clip.read_bytes())
            os.utime(edit / 'final.mp4', (clip.stat().st_mtime, clip.stat().st_mtime))
            state = {'phase': 'cut', 'project': 'Local test', 'cut_video': 'cut.mp4',
                     'final_video': 'final.mp4', 'awaiting': 'approval_cut'}
            (edit / 'state.json').write_text(json.dumps(state), encoding='utf-8')
            timeline = {'fps': 30, 'duration': 3, 'segments': [
                {'id': 's1', 'out_start': 0, 'out_end': 3, 'beat': 'Opening', 'quote': 'Test video'}]}
            (edit / 'cut_timeline.json').write_text(json.dumps(timeline), encoding='utf-8')
            session = preview.Session(edit, preview.STYLE_CATALOG_PATH, '127.0.0.1', 0)
            handler = type('FixtureHandler', (preview.Handler,), {'session': session})
            server = preview.ThreadingHTTPServer(('127.0.0.1', 0), handler)
            server.daemon_threads = True
            session.port = server.server_port
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            yield session, f'http://127.0.0.1:{session.port}'
        finally:
            if server:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
            if previous is None:
                os.environ.pop('EDITONNY_MUSIC_LIBRARY', None)
            else:
                os.environ['EDITONNY_MUSIC_LIBRARY'] = previous


def wait_for_stdin_close():
    """Return when the caller closes stdin. Windows serializes synchronous pipe I/O, so a pending read
    blocks DLL loads that inspect the standard handles (the first numpy import hangs); poll there instead."""
    if os.name != 'nt':
        sys.stdin.read()
        return
    import _winapi
    import msvcrt
    handle = msvcrt.get_osfhandle(sys.stdin.fileno())
    while True:
        try:
            _winapi.PeekNamedPipe(handle, 0)
        except OSError:
            return
        time.sleep(0.2)


if __name__ == '__main__':
    with running_preview(video=True) as (session, url):
        print(json.dumps({'url': url, 'edit': str(session.edit_dir)}), flush=True)
        wait_for_stdin_close()
