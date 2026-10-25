"""Windows live workspace: explicit monitoring, owned recording, durable local results."""
from dataclasses import asdict
import atexit
import csv
import http.cookiejar
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import uuid

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from . import engine, store
from .vendor.live.contracts import public_link, safe_stream_url, confirmed_live_state
from .vendor.live.douyin import DouyinProvider
from .vendor.live.danmaku import CommentRecorder
from .vendor.live.comments import STATES as COMMENT_STATES

router = APIRouter(prefix='/api/live', tags=['live'])
lock = threading.RLock()
jobs = {}
recoveries = set()
closing = threading.Event()
provider = DouyinProvider()
MAX_HOSTS = 100
MAX_ACTIVE = 3
MESSAGES = {'paused': '已暂停', 'monitoring': '等待检查', 'checking': '正在检查直播状态',
            'live': '正在直播', 'offline': '等待开播', 'recording': '正在录制',
            'stopping': '正在保存最后一段', 'network_error': '网络连接失败，将稍后重试',
            'parse_error': '暂时无法识别直播状态', 'access_limited': '平台访问受限，请稍后重试',
            'login_required': '需要登录，请在采集设置导入自己的平台会话',
            'disk_full': '可用空间不足，已暂停', 'recording_error': '录制中断，已有分段保留',
            'interrupted': '上次运行中断，点击开始可继续监控', 'error': '操作失败，已有资料保留'}


def _id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-f0-9]{32}', value):
        raise ValueError('直播条目编号无效')
    return value


def _get(kind, value):
    result = store.object_get(kind, _id(value))
    if not result:
        raise HTTPException(404, '找不到这条直播记录')
    return result


def folder(recording_id, create=False):
    root = store.DATA.resolve() / 'live' / 'recordings'
    target = root / _id(recording_id)
    if (any(part.is_symlink() for part in (store.DATA / 'live', root, target))
            or not target.resolve().is_relative_to(store.DATA.resolve())):
        raise ValueError('直播文件夹位置异常，请恢复原目录')
    if create:
        target.mkdir(parents=True, exist_ok=True)
    return target


def _cookie():
    """Reuse only user-imported cookies applicable to the fixed live host."""
    path = store.settings().get('cookies_path')
    if not path or not Path(path).is_file():
        return ''
    try:
        jar = http.cookiejar.MozillaCookieJar()
        jar.load(path, ignore_discard=True, ignore_expires=False)
        request = urllib.request.Request('https://live.douyin.com/')
        jar.add_cookie_header(request)
        value = request.get_header('Cookie') or ''
        return value if len(value) <= 16384 and '\r' not in value and '\n' not in value else ''
    except (OSError, ValueError, http.cookiejar.LoadError):
        return ''


def _update_host(host_id, **values):
    with lock:
        host = store.object_get('live_host', host_id)
        if host:
            host.update(values)
            host['message'] = MESSAGES.get(host['status'], host['status'])
            store.object_put('live_host', host_id, host)
        return host


def _public_recording(recording):
    return {key: value for key, value in recording.items() if key != 'directory'}


def _save_recording(recording):
    # Importing a closed segment while the next segment records must not lose its
    # library item link when the worker saves its in-memory progress snapshot.
    with lock:
        previous = store.object_get('live_recording', recording['id']) or {}
        imported = {part['id']: part.get('item_id', '') for part in previous.get('segments', [])}
        for part in recording['segments']:
            if imported.get(part['id']):
                part['item_id'] = imported[part['id']]
        return store.object_put('live_recording', recording['id'], recording)


def _append_metrics(recording, observation):
    path = folder(recording['id']) / 'metrics.jsonl'
    if path.is_symlink():
        raise ValueError('统计文件位置异常')
    if path.exists() and path.stat().st_size >= 32 * 1024**2:
        recording['metrics_message'] = '统计记录达到 32 MiB，录像继续'
        return
    entry = {'at': time.time(), 'elapsed_seconds': round(time.time() - recording['started'], 3),
             'live_state': confirmed_live_state(observation.state),
             **(asdict(observation.metrics) if observation.metrics else {})}
    with path.open('a', encoding='utf-8') as output:
        output.write(json.dumps(entry, ensure_ascii=False, allow_nan=False) + '\n')


class OwnedProcess:
    """A separate helper closes FFmpeg on our pipe EOF, including backend crashes."""
    def __init__(self, command):
        self.lock = threading.RLock()
        self.process = subprocess.Popen([sys.executable, '-u', str(Path(__file__).with_name('live_guard.py'))],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.process.stdin.write((json.dumps({'command': command}) + '\n').encode('utf-8'))
        self.process.stdin.flush()

    def poll(self):
        return self.process.poll()

    def stop(self):
        self.request_stop()
        try:
            self.process.wait(timeout=9)
        except subprocess.TimeoutExpired:
            # The helper should have reaped its FFmpeg child before this deadline.
            # Keep it owned; do not orphan a writer with an indiscriminate kill.
            raise ValueError('录制组件正在收尾，请稍后重试') from None
        finally:
            if self.process.poll() is not None and self.process.stdout:
                self.process.stdout.close()

    def request_stop(self):
        with self.lock:
            if self.process.stdin and not self.process.stdin.closed:
                self.process.stdin.close()


class SegmentWriter:
    def __init__(self, recording, stream, segment_seconds):
        from requests.certs import where
        if not safe_stream_url(stream.url):
            raise ValueError('平台未提供可用直播流')
        engine.validate_url(stream.url)
        executable = shutil.which('ffmpeg')
        if not executable:
            raise ValueError('缺少 FFmpeg，无法录制直播')
        self.recording = recording
        self.directory = folder(recording['id'], create=True)
        self.prefix = uuid.uuid4().hex[:12]
        self.list_path = self.directory / (self.prefix + '.csv')
        self.bytes = 0
        self.last_growth = time.monotonic()
        tls = ['-tls_verify', '1', '-ca_file', where()] if stream.url.startswith('https://') else []
        command = [executable, '-hide_banner', '-loglevel', 'error', '-n', '-rw_timeout', '15000000',
                   '-protocol_whitelist', 'http,https,tcp,tls,crypto', *tls,
                   '-user_agent', 'Mozilla/5.0', '-i', stream.url, '-map', '0:v?', '-map', '0:a?',
                   '-c', 'copy', '-f', 'segment', '-segment_format', 'mpegts',
                   '-segment_time', str(segment_seconds), '-reset_timestamps', '1',
                   '-segment_list', str(self.list_path), '-segment_list_type', 'csv',
                   str(self.directory / (self.prefix + '-%06d.ts'))]
        self.process = OwnedProcess(command)

    def collect(self, final=False):
        paths = []
        if self.list_path.is_file() and not self.list_path.is_symlink():
            with self.list_path.open(encoding='utf-8', newline='') as source:
                for row in csv.reader(source):
                    if len(row) == 3 and re.fullmatch(self.prefix + r'-\d{6}\.ts', Path(row[0]).name):
                        paths.append(self.directory / Path(row[0]).name)
        if final:
            paths = sorted(self.directory.glob(self.prefix + '-*.ts'))
        publish_segments(self.recording, paths)
        size = sum(p.stat().st_size for p in self.directory.glob(self.prefix + '-*.ts') if p.is_file() and not p.is_symlink())
        if size > self.bytes:
            self.last_growth = time.monotonic()
            self.bytes = size
        return time.monotonic() - self.last_growth < 45

    def stop(self, finalize=True):
        self.process.stop()
        if finalize:
            self.collect(final=True)


def publish_segments(recording, paths):
    """Remux closed TS segments to playable MP4; retain source on every failure."""
    directory = folder(recording['id'])
    seen = {entry['source'] for entry in recording['segments']}
    changed = False
    for path in paths:
        if path.name in seen:
            continue
        if (path.parent != directory or path.is_symlink() or not path.is_file()
                or not re.fullmatch(r'[a-f0-9]{12}-\d{6}\.ts', path.name) or path.stat().st_size == 0):
            continue
        target = path.with_suffix('.mp4')
        if target.is_symlink():
            raise ValueError('录像分段位置异常')
        duration = engine.media_info(target) if target.exists() else 0
        if duration <= 0:
            temporary = target.with_suffix('.part.mp4')
            if temporary.is_symlink():
                raise ValueError('录像临时文件位置异常')
            try:
                result = engine.run_command([shutil.which('ffmpeg') or 'ffmpeg', '-nostdin', '-hide_banner',
                    '-loglevel', 'error', '-y', '-protocol_whitelist', 'file', '-f', 'mpegts', '-i', str(path),
                    '-map', '0:v?', '-map', '0:a?', '-c', 'copy', '-movflags', '+faststart', str(temporary)], timeout=30)
                duration = engine.media_info(temporary) if result.returncode == 0 else 0
            except subprocess.TimeoutExpired:
                duration = 0
            if duration <= 0:
                recording['warning'] = '部分片段尚未完成封装，原始录像保留，可点击恢复分段重试'
                continue
            os.replace(temporary, target)
        recording['segments'].append({'id': uuid.uuid4().hex, 'source': path.name, 'file': target.name,
            'duration': round(duration, 3), 'bytes': target.stat().st_size, 'item_id': ''})
        seen.add(path.name)
        changed = True
    recording['duration'] = round(sum(s['duration'] for s in recording['segments']), 3)
    recording['bytes'] = sum(s['bytes'] for s in recording['segments'])
    if changed:
        _save_recording(recording)


class Monitor:
    def __init__(self, host, check_only=False):
        self.host = host
        self.cancel = threading.Event()
        self.thread = threading.Thread(target=self.run, name='lulu-live-monitor', daemon=True)
        self.writer = None
        self.comments = None
        self.recording = None
        self.writer_lock = threading.RLock()
        self.check_only = check_only
        self.exiting = False

    def start_recording(self, observation, credential):
        now = time.time()
        recording_id = uuid.uuid4().hex
        self.recording = {'id': recording_id, 'host_id': self.host['id'],
            'name': self.host.get('alias') or observation.name or self.host['name'], 'title': observation.metrics.title if observation.metrics else '',
            'source': self.host['source'], 'started': now, 'ended': None, 'status': 'recording',
            'directory': str(folder(recording_id, create=True)), 'segments': [], 'duration': 0, 'bytes': 0,
            'comments_state': 'disabled', 'comments_count': 0, 'warning': ''}
        _save_recording(self.recording)
        preferred = next((stream for stream in observation.streams if stream.codec.lower() in {'h264', 'avc', 'avc1'}), observation.streams[0])
        self.writer = SegmentWriter(self.recording, preferred, self.host['segment_seconds'])
        if self.host['comments']:
            if importlib.util.find_spec('websocket'):
                self.comments = CommentRecorder(self.recording, observation.room_id, credential)
                self.comments.start()
            else:
                self.recording['comments_state'] = 'missing_dependency'
        _update_host(self.host['id'], status='recording', recording_id=recording_id)

    def finish_recording(self, reason, finalize=True):
        with self.writer_lock:
            if self.comments:
                self.comments.request_stop()
                if self.comments.thread:
                    self.comments.thread.join(timeout=1 if self.exiting else 7)
                self.recording.update(self.comments.snapshot())
                self.comments = None
            if self.writer:
                try:
                    self.writer.stop(finalize=finalize and not self.exiting)
                except (OSError, ValueError, subprocess.TimeoutExpired):
                    reason = 'interrupted'
                self.writer = None
            if self.recording:
                if reason == 'saved' and not self.recording['segments']:
                    reason = 'interrupted'
                    self.recording['warning'] = '尚未取得可播放分段，原始文件保留；请检查平台状态或尝试恢复分段'
                self.recording.update(ended=time.time(), status='interrupted' if self.exiting else reason)
                if self.exiting:
                    self.recording['warning'] = '程序退出时已保留原始分段，可点击恢复分段完成封装'
                _save_recording(self.recording)
                self.recording = None
            _update_host(self.host['id'], recording_id='')

    def run(self):
        try:
            while not self.cancel.is_set():
                host = _update_host(self.host['id'], status='recording' if self.writer else 'checking')
                if not host:
                    break
                credential = _cookie()
                observation = provider.check(host, credential)
                if self.cancel.is_set():
                    break
                _update_host(host['id'], name=host.get('alias') or observation.name or host['name'], sec_uid=observation.sec_uid,
                    web_rid=observation.web_rid, live_state=confirmed_live_state(observation.state),
                    checked_at=time.time(), metrics=asdict(observation.metrics) if observation.metrics else {},
                    status='recording' if self.writer else observation.state)
                with self.writer_lock:
                    if self.cancel.is_set():
                        break
                    if self.writer and observation.state == 'offline':
                        self.finish_recording('saved')
                        _update_host(host['id'], status='offline')
                    if not self.check_only and not self.writer and observation.state == 'live' and host['auto_record']:
                        if shutil.disk_usage(store.DATA).free < 512 * 1024**2:
                            _update_host(host['id'], status='disk_full', enabled=False)
                            break
                        self.start_recording(observation, credential)
                    if self.recording:
                        _append_metrics(self.recording, observation)
                        if self.comments:
                            self.recording.update(self.comments.snapshot())
                        _save_recording(self.recording)
                credential = ''
                if self.check_only:
                    break
                deadline = time.monotonic() + host['interval_seconds']
                while not self.cancel.wait(2) and time.monotonic() < deadline:
                    with self.writer_lock:
                        if self.writer:
                            growing = self.writer.collect()
                            if self.writer.process.poll() is not None or not growing:
                                self.finish_recording('interrupted')
                                _update_host(host['id'], status='recording_error')
                                break
                            if shutil.disk_usage(folder(self.recording['id'])).free < 256 * 1024**2:
                                self.cancel.set()
                                _update_host(host['id'], status='disk_full', enabled=False)
                                break
                # A failed stream should not create a new session every two
                # seconds. Preserve the same bounded probe cadence on failures.
                self.cancel.wait(max(0, deadline - time.monotonic()))
        except Exception:
            _update_host(self.host['id'], status='error', enabled=False)
        finally:
            self.finish_recording('saved' if self.cancel.is_set() else 'interrupted')
            with lock:
                host = store.object_get('live_host', self.host['id'])
                if host and not self.check_only and host['status'] not in {'disk_full', 'error'}:
                    _update_host(self.host['id'], status='paused', enabled=False)
                elif host:
                    _update_host(self.host['id'], enabled=False)
                jobs.pop(self.host['id'], None)

    def stop(self):
        self.cancel.set()
        _update_host(self.host['id'], status='stopping', enabled=False)
        writer = self.writer
        if writer:
            writer.process.request_stop()
        # The worker owns all closing/remux work; UI remains responsive.


def startup():
    """Never restart network activity on application launch."""
    closing.clear()
    for host in store.object_list('live_host'):
        if (host.get('enabled') or host.get('recording_id')
                or host.get('status') in {'checking', 'stopping', 'monitoring', 'recording'}):
            _update_host(host['id'], enabled=False, status='interrupted', recording_id='')
    for recording in store.object_list('live_recording'):
        if recording.get('ended') is None:
            recording.update(status='interrupted', ended=time.time(), comments_state='interrupted')
            store.object_put('live_recording', recording['id'], recording)


def shutdown():
    closing.set()
    with lock:
        active = list(jobs.values())
    for job in active:
        job.exiting = True
        job.stop()
    closers = []
    for job in active:
        closer = threading.Thread(target=job.finish_recording, args=('interrupted', False), daemon=True)
        closer.start()
        closers.append(closer)
    deadline = time.monotonic() + 12
    for closer in closers:
        closer.join(max(0, deadline - time.monotonic()))


atexit.register(shutdown)


class HostInput(BaseModel):
    source: str = Field(min_length=1, max_length=4096)
    name: str = Field(default='', max_length=200)
    segment_seconds: int = Field(default=1800, ge=60, le=3600)
    interval_seconds: int = Field(default=60, ge=30, le=600)
    auto_record: bool = True
    comments: bool = True


@router.get('/state')
def state():
    return {'hosts': store.object_list('live_host'),
            'recordings': [_public_recording(r) for r in store.object_list('live_recording')],
            'ffmpeg': bool(shutil.which('ffmpeg')), 'comments_available': bool(importlib.util.find_spec('websocket')),
            'max_active': MAX_ACTIVE}


@router.post('/hosts')
def add_host(body: HostInput):
    source = public_link(body.source)
    with lock:
        hosts = store.object_list('live_host')
        if any(h['source'] == source for h in hosts):
            raise ValueError('这个主播已在监控列表中')
        if len(hosts) >= MAX_HOSTS:
            raise ValueError('最多保存 100 位主播，请先移除不需要的条目')
        host = {**body.model_dump(), 'source': source, 'id': uuid.uuid4().hex,
                'name': body.name.strip() or '待识别主播', 'alias': body.name.strip(), 'status': 'paused', 'message': '已暂停',
                'enabled': False, 'live_state': 'unknown', 'checked_at': None, 'metrics': {},
                'web_rid': '', 'sec_uid': '', 'recording_id': ''}
        return store.object_put('live_host', host['id'], host)


@router.post('/hosts/{host_id}/start')
def start(host_id: str):
    with lock:
        if closing.is_set():
            raise ValueError('程序正在退出，请重新打开后开始监控')
        host = _get('live_host', host_id)
        if host_id in jobs:
            raise ValueError('该主播正在监控或收尾，请等待当前操作完成')
        if len(jobs) >= MAX_ACTIVE:
            raise ValueError('最多同时监控 3 位主播，请先暂停其他任务')
        if host['auto_record'] and not shutil.which('ffmpeg'):
            raise ValueError('缺少 FFmpeg，无法开始录制')
        _update_host(host_id, status='monitoring', enabled=True)
        job = Monitor(host)
        jobs[host_id] = job
        job.thread.start()
    return {'ok': True}


@router.post('/hosts/{host_id}/pause')
def pause(host_id: str):
    with lock:
        _get('live_host', host_id)
        job = jobs.get(host_id)
        if job:
            job.stop()
        else:
            _update_host(host_id, enabled=False, status='paused')
    return {'ok': True}


@router.post('/hosts/{host_id}/check')
def check(host_id: str):
    with lock:
        if closing.is_set():
            raise ValueError('程序正在退出，请重新打开后检查')
        host = _get('live_host', host_id)
        if host_id in jobs or len(jobs) >= MAX_ACTIVE:
            raise ValueError('请等待当前检查完成，或暂停一个监控任务')
        _update_host(host_id, status='checking', enabled=True)
        job = Monitor(host, check_only=True)
        jobs[host_id] = job
        job.thread.start()
    return {'ok': True}


@router.delete('/hosts/{host_id}')
def remove(host_id: str):
    with lock:
        _get('live_host', host_id)
        if host_id in jobs:
            raise ValueError('请先暂停监控，等待录像保存后再移除主播')
        store.object_delete('live_host', host_id)
    return {'ok': True}


@router.get('/recordings/{recording_id}')
def detail(recording_id: str):
    return _public_recording(_get('live_recording', recording_id))


def _segment(recording_id, segment_id):
    recording = _get('live_recording', recording_id)
    segment = next((s for s in recording['segments'] if s['id'] == _id(segment_id)), None)
    if not segment or not re.fullmatch(r'[a-f0-9]{12}-\d{6}\.mp4', segment['file']):
        raise HTTPException(404, '找不到录像分段')
    path = folder(recording_id) / segment['file']
    if path.is_symlink() or not path.is_file():
        raise HTTPException(404, '分段文件不存在，原记录保留')
    return recording, segment, path


@router.get('/recordings/{recording_id}/segments/{segment_id}/media')
def media(recording_id: str, segment_id: str):
    _, _, path = _segment(recording_id, segment_id)
    return FileResponse(path, media_type='video/mp4')


@router.get('/recordings/{recording_id}/segments/{segment_id}/download')
def download(recording_id: str, segment_id: str):
    _, _, path = _segment(recording_id, segment_id)
    return FileResponse(path, media_type='video/mp4', filename=path.name)


@router.post('/recordings/{recording_id}/segments/{segment_id}/import')
def import_segment(recording_id: str, segment_id: str):
    with lock:
        recording, segment, path = _segment(recording_id, segment_id)
        existing = store.get(segment.get('item_id', '')) if segment.get('item_id') else None
        if existing:
            return existing
        number = next(i + 1 for i, part in enumerate(recording['segments']) if part['id'] == segment_id)
        item = store.add(f"{recording['name']} · 直播片段 {number}", kind='clip',
                         media_path=str(path), duration=segment['duration'], phase='直播录像，可开始转录',
                         metadata={'live_recording_id': recording_id, 'live_segment_id': segment_id})
        segment['item_id'] = item['id']
        store.object_put('live_recording', recording_id, recording)
        return item


@router.post('/recordings/{recording_id}/recover')
def recover(recording_id: str):
    with lock:
        recording = _get('live_recording', recording_id)
        if recording['ended'] is None or recording['host_id'] in jobs or recording_id in recoveries:
            raise ValueError('请先暂停录制并等待收尾')
        if closing.is_set():
            raise ValueError('程序正在退出，请重新打开后恢复分段')
        recoveries.add(recording_id)
    try:
        paths = sorted(folder(recording_id).glob('*.ts'))
        deadline = time.monotonic() + 30
        for path in paths:
            if closing.is_set() or time.monotonic() >= deadline:
                recording['warning'] = '本次已保存完成的分段；剩余原始录像可再次点击恢复分段'
                break
            publish_segments(recording, [path])
        recording['status'] = 'recovered' if recording['segments'] else 'interrupted'
        if paths and {path.name for path in paths} <= {part['source'] for part in recording['segments']}:
            recording['warning'] = ''
        _save_recording(recording)
        return _public_recording(recording)
    finally:
        with lock:
            recoveries.discard(recording_id)


def _lines(recording_id, name):
    path = folder(recording_id) / name
    if not path.exists():
        return
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 256 * 1024**2:
        raise ValueError('记录文件过大或位置异常')
    with path.open('r', encoding='utf-8') as source:
        while True:
            line = source.readline(128 * 1024 + 1)
            if not line:
                break
            if len(line) > 128 * 1024:
                raise ValueError('记录内容过长')
            if not line.endswith('\n'):
                break  # Concurrent append or interrupted tail; complete earlier rows remain readable.
            try:
                yield json.loads(line)
            except ValueError:
                continue


@router.get('/recordings/{recording_id}/data')
def recording_data(recording_id: str, page: int = 1, query: str = ''):
    recording = _get('live_recording', recording_id)
    if page < 1 or page > 100000 or len(query) > 300:
        raise ValueError('无效的筛选条件')
    total, comments = 0, []
    for entry in _lines(recording_id, 'comments.jsonl'):
        if entry.get('kind') != 'chat' or query.casefold() not in (str(entry.get('nickname', '')) + str(entry.get('text', ''))).casefold():
            continue
        if (page - 1) * 100 <= total < page * 100:
            comments.append(entry)
        total += 1
    # Recent samples are enough for the preview; the export retains every sample.
    from collections import deque
    metrics = list(deque(_lines(recording_id, 'metrics.jsonl'), maxlen=300))
    return {'comments': comments, 'total': total, 'page': page, 'metrics': metrics,
            'comments_message': COMMENT_STATES.get(recording['comments_state'], '弹幕状态未知')}


@router.get('/recordings/{recording_id}/export/{kind}')
def export_data(recording_id: str, kind: str):
    _get('live_recording', recording_id)
    if kind not in {'comments', 'metrics'}:
        raise ValueError('请选择弹幕或互动数据')
    fields = (['received_at', 'elapsed_seconds', 'nickname', 'text'] if kind == 'comments' else
              ['at', 'elapsed_seconds', 'live_state', 'title', 'viewer_count', 'viewer_count_raw', 'like_count', 'cumulative_view_count', 'cumulative_view_count_raw'])
    def chunks():
        yield '\ufeff'
        content = io.StringIO(newline='')
        writer = csv.writer(content)
        writer.writerow(fields)
        yield content.getvalue()
        for entry in _lines(recording_id, kind + '.jsonl'):
            if kind == 'comments' and entry.get('kind') != 'chat':
                continue
            row = []
            for key in fields:
                value = entry.get(key, '')
                if isinstance(value, str) and value.lstrip().startswith(('=', '+', '-', '@')):
                    value = "'" + value
                row.append(value)
            content.seek(0)
            content.truncate()
            writer.writerow(row)
            yield content.getvalue()
    return StreamingResponse(chunks(), media_type='text/csv',
                    headers={'Content-Disposition': f'attachment; filename="lulu-live-{kind}-{recording_id[:8]}.csv"'})
