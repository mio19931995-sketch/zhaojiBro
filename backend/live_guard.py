"""Own one FFmpeg child; parent pipe EOF also stops it after a backend crash.

Standalone entry intentionally imports no Lulu store, secrets or application modules.
"""
import json
import os
import subprocess
import sys
import threading


def flush(process):
    try:
        if process.poll() is None:
            process.stdin.write(b'q\n')
            process.stdin.flush()
            process.wait(timeout=4)
    except (OSError, subprocess.TimeoutExpired):
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)
    finally:
        if process.stdin:
            process.stdin.close()


def main():
    raw = sys.stdin.buffer.readline(65537)
    if len(raw) > 65536:
        return 2
    command = json.loads(raw)['command']
    if not isinstance(command, list) or not command or not all(isinstance(x, str) for x in command):
        return 2
    stopped = threading.Event()
    def watch_parent():
        sys.stdin.buffer.read(1)
        stopped.set()
    threading.Thread(target=watch_parent, daemon=True).start()
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    try:
        print(json.dumps({'ready': True}), flush=True)
        while process.poll() is None and not stopped.wait(.1):
            pass
        flush(process)
        return 0 if process.returncode == 0 else 1
    finally:
        flush(process)


if __name__ == '__main__':
    try:
        result = main()
    except (ValueError, KeyError, OSError):
        result = 2
    # The reader deliberately blocks until parent EOF. Skip Python buffered-I/O
    # finalization of that daemon thread after FFmpeg has already been reaped.
    os._exit(result)
