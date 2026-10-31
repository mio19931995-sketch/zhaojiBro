"""Isolated live contracts and real synthetic FFmpeg, never a platform request."""
import gzip
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
import time
import uuid
from types import SimpleNamespace

os.environ['LULU_DATA_DIR'] = tempfile.mkdtemp(prefix='lulu-live-tests-')

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from backend import live, store
from backend.vendor.live import danmaku
from backend.vendor.live.comments import CommentLog
from backend.vendor.live.contracts import LiveMetrics, Observation, Stream, public_link, safe_stream_url
from backend.vendor.live.douyin import parse_room, room_metrics

app = FastAPI()
app.include_router(live.router)


@app.exception_handler(ValueError)
async def invalid(request, error):
    return JSONResponse({'detail': str(error)}, status_code=400)


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(store, 'DATA', tmp_path)
    monkeypatch.setattr(store, 'DB', tmp_path / 'library.sqlite3')
    monkeypatch.setattr(live, 'jobs', {})
    store.init()
    live.startup()
    def forbidden(*args, **kwargs):
        raise AssertionError('Tests must not access any live platform')
    monkeypatch.setattr('requests.Session.request', forbidden)
    with TestClient(app) as value:
        yield value
    live.shutdown()


def host(client, **options):
    response = client.post('/api/live/hosts', json={'source': 'https://live.douyin.com/123456', **options})
    assert response.status_code == 200, response.text
    return response.json()


def recording():
    value = {'id': uuid.uuid4().hex, 'host_id': uuid.uuid4().hex, 'name': '离线合成直播', 'title': '本地测试',
             'source': 'https://live.douyin.com/123456', 'started': time.time(), 'ended': None,
             'status': 'recording', 'segments': [], 'duration': 0, 'bytes': 0,
             'comments_state': 'disabled', 'comments_count': 0, 'warning': ''}
    value['directory'] = str(live.folder(value['id'], create=True))
    store.object_put('live_recording', value['id'], value)
    return value


def wait_until(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.02)
    raise AssertionError('Timed out waiting for state transition')


def test_probe_contract_distinguishes_rounded_unknown_and_offline(client):
    metrics = room_metrics({'title': '直播', 'stats': {'user_count_str': '1.2万', 'total_user_str': '10万+'}, 'like_count': 0})
    assert metrics.viewer_count is None and metrics.viewer_count_raw == '1.2万'
    assert metrics.like_count == 0 and metrics.cumulative_view_count is None
    assert parse_room({'data': {}}).state == 'parse_error'
    assert parse_room({'status_code': 403}).state == 'access_limited'
    assert parse_room({'data': {'data': [{'status': 4}]}}).state == 'offline'
    assert parse_room({'data': {'data': [{'status': 2}]}}).state == 'parse_error'
    payload = {'data': {'data': [{'status': 2, 'id_str': '123', 'owner': {'nickname': '样例主播'},
                                'stream_url': {'flv_pull_url': {'FULL_HD1': 'https://live.douyincdn.com/a.flv?token=SECRET'}}}]}}
    observation = parse_room(payload, '123456')
    assert observation.state == 'live' and len(observation.streams) == 1
    assert 'SECRET' not in repr(observation)


@pytest.mark.parametrize('url', ['http://live.douyin.com/123', 'https://127.0.0.1/123',
    'https://live.douyin.com.evil.test/123', 'file:///C:/secret.txt', 'https://u:p@live.douyin.com/123'])
def test_room_input_rejects_non_platform_or_credentials(client, url):
    assert client.post('/api/live/hosts', json={'source': url}).status_code == 400


def test_add_is_offline_and_bounded_and_stream_paths_are_not_user_input(client):
    saved = host(client)
    assert saved['enabled'] is False and saved['metrics'] == {}
    assert client.post('/api/live/hosts', json={'source': saved['source']}).status_code == 400
    assert client.post('/api/live/hosts', json={'source': 'https://live.douyin.com/999', 'segment_seconds': 0}).status_code == 422
    assert not safe_stream_url('http://localhost/a.ts')
    assert not safe_stream_url('https://evil.test/a.ts')
    with pytest.raises(ValueError): live.folder('../outside')
    assert 'directory' not in client.get('/api/live/state').text


def test_check_is_one_shot_and_does_not_record(client, monkeypatch):
    saved = host(client, name='我的主播备注')
    monkeypatch.setattr(live, 'provider', SimpleNamespace(check=lambda *args: Observation('offline', name='测试主播', metrics=LiveMetrics(viewer_count=0))))
    assert client.post(f"/api/live/hosts/{saved['id']}/check").status_code == 200
    wait_until(lambda: not live.jobs)
    state = client.get('/api/live/state').json()
    assert state['hosts'][0]['live_state'] == 'offline'
    assert state['hosts'][0]['enabled'] is False
    assert state['hosts'][0]['name'] == '我的主播备注'
    assert state['recordings'] == []


def test_stop_while_probe_pending_cannot_start_recording(client, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    def probe(*args):
        entered.set(); release.wait(5)
        return Observation('live', streams=(Stream('https://live.douyincdn.com/a.flv'),))
    monkeypatch.setattr(live, 'provider', SimpleNamespace(check=probe))
    saved = host(client)
    assert client.post(f"/api/live/hosts/{saved['id']}/start").status_code == 200
    assert entered.wait(3)
    assert client.delete(f"/api/live/hosts/{saved['id']}").status_code == 400
    assert client.post(f"/api/live/hosts/{saved['id']}/pause").status_code == 200
    release.set()
    wait_until(lambda: not live.jobs)
    assert store.object_list('live_recording') == []
    assert store.object_get('live_host', saved['id'])['status'] == 'paused'


def test_restart_preserves_files_and_pauses_without_network(client):
    saved = host(client)
    live._update_host(saved['id'], enabled=False, status='stopping', recording_id='')
    value = recording()
    original = live.folder(value['id']) / '123456abcdef-000000.ts'
    original.write_bytes(b'keep original recording')
    live.startup()
    result = client.get('/api/live/state').json()
    assert not live.jobs and not result['hosts'][0]['enabled']
    assert result['recordings'][0]['status'] == 'interrupted'
    assert result['recordings'][0]['ended'] is not None
    assert original.read_bytes() == b'keep original recording'


def test_comment_protocol_decodes_gzip_chat_and_ack_without_network(client):
    f = danmaku.encode_field
    chat = f(1, f(2, 77)) + f(2, f(3, '观众'.encode())) + f(3, '你好直播'.encode())
    message = f(1, b'WebcastChatMessage') + f(2, chat)
    response = f(1, message) + f(5, b'ack-token') + f(9, 1)
    raw = f(2, 11) + f(8, gzip.compress(response))
    chats, ack = danmaku.decode_frame(raw)
    assert chats[0]['text'] == '你好直播' and chats[0]['nickname'] == '观众'
    assert ack and danmaku.protobuf(ack)[7] == [b'ack']
    with pytest.raises(ValueError): danmaku.decode_frame(b'\x0a\xff')


def test_comments_filter_pagination_csv_formula_and_unknown_metrics(client):
    value = recording()
    log = CommentLog(value)
    entries = [{'kind': 'chat', 'nickname': '=HYPERLINK("x")', 'text': f'弹幕 {i}',
                'received_at': '2026-10-25T00:00:00Z', 'elapsed_seconds': i, 'message_id': str(i)} for i in range(105)]
    log.append(entries)
    live._append_metrics(value, Observation('live', metrics=LiveMetrics(viewer_count=None, viewer_count_raw='1.2万', like_count=12)))
    route = f"/api/live/recordings/{value['id']}"
    first = client.get(route + '/data').json()
    assert first['total'] == 105 and len(first['comments']) == 100
    assert first['metrics'][0]['viewer_count'] is None
    assert len(client.get(route + '/data?page=2').json()['comments']) == 5
    assert client.get(route + '/data?query=不存在').json()['total'] == 0
    text = client.get(route + '/export/comments').content.decode('utf-8-sig')
    assert "'=HYPERLINK" in text and '弹幕 104' in text
    metrics = client.get(route + '/export/metrics').content.decode('utf-8-sig')
    assert '1.2万' in metrics


def test_comment_log_keeps_partial_tail_and_enforces_size_limit(client, monkeypatch):
    from backend.vendor.live import comments
    value = recording()
    log = CommentLog(value)
    path = live.folder(value['id']) / 'comments.jsonl'
    path.write_bytes(b'{"partial":')
    with pytest.raises(ValueError): log.append([{'kind': 'chat', 'text': 'new'}])
    assert path.read_bytes() == b'{"partial":'
    path.write_bytes(b'')
    monkeypatch.setattr(comments, 'MAX_BYTES', 10)
    with pytest.raises(ValueError): log.append([{'kind': 'chat', 'text': 'new'}])
    assert path.read_bytes() == b''


def test_monitor_records_metrics_and_shutdown_does_not_wait_for_network(client, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    calls = []
    def probe(*args):
        if calls:
            entered.set(); release.wait(5)
        calls.append(True)
        return Observation('live', name='合成主播', streams=(Stream('https://live.douyincdn.com/fake.flv'),),
                           metrics=LiveMetrics(viewer_count=23, like_count=100))
    class FakeWriter:
        def __init__(self, saved, *args):
            self.process = SimpleNamespace(poll=lambda: None, request_stop=lambda: None)
            self.saved = saved
        def collect(self): return True
        def stop(self, finalize=True): pass
    monkeypatch.setattr(live, 'provider', SimpleNamespace(check=probe))
    monkeypatch.setattr(live, 'SegmentWriter', FakeWriter)
    saved = host(client, comments=False)
    saved['interval_seconds'] = .01
    store.object_put('live_host', saved['id'], saved)
    client.post(f"/api/live/hosts/{saved['id']}/start")
    assert entered.wait(4)
    values = store.object_list('live_recording')
    assert len(values) == 1 and values[0]['status'] == 'recording'
    data = client.get(f"/api/live/recordings/{values[0]['id']}/data").json()
    assert data['metrics'][0]['viewer_count'] == 23
    started = time.monotonic()
    live.shutdown()
    assert time.monotonic() - started < 2
    assert store.object_get('live_recording', values[0]['id'])['status'] == 'interrupted'
    release.set()
    wait_until(lambda: not live.jobs)
    assert len(store.object_list('live_recording')) == 1


def test_progress_snapshot_preserves_imported_library_link(client):
    value = recording()
    value['segments'] = [{'id': 'a' * 32, 'item_id': '', 'source': 'a.ts', 'duration': 1, 'bytes': 1}]
    live._save_recording(value)
    copied = store.object_get('live_recording', value['id'])
    copied['segments'][0]['item_id'] = 'b' * 32
    store.object_put('live_recording', value['id'], copied)
    live._save_recording(value)
    assert store.object_get('live_recording', value['id'])['segments'][0]['item_id'] == 'b' * 32


@pytest.mark.skipif(not shutil.which('ffmpeg') or not shutil.which('ffprobe'), reason='Needs installed FFmpeg')
def test_real_synthetic_segment_remux_play_import_and_recover(client):
    value = recording()
    directory = live.folder(value['id'])
    command = [shutil.which('ffmpeg'), '-hide_banner', '-loglevel', 'error', '-n',
               '-f', 'lavfi', '-i', 'testsrc2=size=160x90:rate=10', '-t', '2.4',
               '-c:v', 'mpeg2video', '-g', '10', '-f', 'segment', '-segment_time', '1',
               '-segment_format', 'mpegts', '-reset_timestamps', '1', str(directory / '123456abcdef-%06d.ts')]
    process = live.OwnedProcess(command)
    wait_until(lambda: process.poll() is not None, timeout=10)
    process.stop()
    assert process.poll() == 0
    value['ended'] = time.time()
    store.object_put('live_recording', value['id'], value)
    route = f"/api/live/recordings/{value['id']}"
    result = client.post(route + '/recover')
    assert result.status_code == 200, result.text
    saved = result.json()
    assert len(saved['segments']) >= 2 and saved['duration'] > 2
    part = saved['segments'][0]
    media = client.get(route + f"/segments/{part['id']}/media", headers={'Range': 'bytes=0-31'})
    assert media.status_code in (200, 206) and b'ftyp' in media.content[:32]
    imported = client.post(route + f"/segments/{part['id']}/import").json()
    again = client.post(route + f"/segments/{part['id']}/import").json()
    assert imported['id'] == again['id']
    assert imported['status'] == 'idle' and Path(imported['media_path']).is_file()
    assert Path(imported['media_path']).with_suffix('.ts').exists()
    recovered = client.post(route + '/recover').json()
    assert [entry['id'] for entry in recovered['segments']] == [entry['id'] for entry in saved['segments']]
    assert recovered['segments'][0]['item_id'] == imported['id']
    assert len(store.items()) == 1


@pytest.mark.skipif(not shutil.which('ffmpeg'), reason='Needs installed FFmpeg')
def test_parent_pipe_eof_flushes_owned_ffmpeg(client):
    value = recording()
    target = live.folder(value['id']) / '123456abcdef-000000.ts'
    process = live.OwnedProcess([shutil.which('ffmpeg'), '-hide_banner', '-loglevel', 'error', '-n', '-re',
        '-f', 'lavfi', '-i', 'testsrc2=size=160x90:rate=10', '-c:v', 'mpeg2video', '-f', 'mpegts', str(target)])
    wait_until(lambda: target.exists() and target.stat().st_size > 0, timeout=8)
    started = time.monotonic()
    process.request_stop()  # Exactly the EOF seen if the backend disappears.
    wait_until(lambda: process.poll() is not None, timeout=9)
    process.stop()
    assert time.monotonic() - started < 9
    assert target.exists() and live.engine.media_info(target) > 0
