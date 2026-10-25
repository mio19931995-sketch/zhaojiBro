"""Portable bounded append-only comment log used by the upstream wire adapter."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import threading

from .contracts import LiveError

MAX_BYTES = 256 * 1024**2
MAX_LINE = 128 * 1024
STATES = {
    'connecting': '正在连接弹幕', 'waiting': '已连接，等待弹幕', 'recording': '正在保存弹幕',
    'reconnecting': '弹幕连接中断，正在重连', 'retrying': '弹幕暂未连接，正在重试；录像继续',
    'unavailable': '暂未取得本场弹幕编号', 'write_error': '弹幕保存失败，录像继续',
    'limit': '弹幕记录达到 256 MiB，已停止保存；录像继续', 'ended': '弹幕保存已结束',
    'interrupted': '弹幕记录中断，已有记录保留', 'disabled': '本场未开启弹幕记录',
    'legacy': '这场录像没有弹幕记录', 'missing_dependency': '弹幕组件未安装，录像继续',
}


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds')


def status_text(state, count=0):
    return f'弹幕已保存 {count:,} 条' if state == 'recording' else STATES.get(state, STATES['interrupted'])


class CommentLog:
    def __init__(self, recording):
        self.recording = recording
        self.lock = threading.RLock()

    def append(self, entries):
        if not entries:
            return
        payload = b''.join((json.dumps({**entry, 'recording_id': self.recording['id']},
                           ensure_ascii=False, allow_nan=False) + '\n').encode('utf-8') for entry in entries)
        if any(len(line) > MAX_LINE for line in payload.splitlines()):
            raise LiveError('弹幕内容过长，未保存该条')
        folder = Path(self.recording['directory'])
        path = folder / 'comments.jsonl'
        if folder.is_symlink() or path.is_symlink():
            raise LiveError('弹幕文件位置异常')
        with self.lock:
            size = path.stat().st_size if path.exists() else 0
            if size + len(payload) > MAX_BYTES:
                raise LiveError(STATES['limit'])
            if size:
                with path.open('rb') as previous:
                    previous.seek(-1, os.SEEK_END)
                    if previous.read(1) != b'\n':
                        raise LiveError('上次弹幕写入中断，已有内容保留')
            with path.open('ab') as output:
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
