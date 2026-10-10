"""Offline regression checks for the three workbench review tasks."""
import os
import shutil
import subprocess
import tempfile
import wave
from pathlib import Path
from types import SimpleNamespace

os.environ['LULU_DATA_DIR'] = tempfile.mkdtemp(prefix='lulu-workbench-review-')

import pytest
from fastapi.testclient import TestClient
from backend import store, engine, assets, codex_bridge
from backend.main import app

client = TestClient(app, headers={'X-Lulu-Client': 'desktop'})


@pytest.fixture(autouse=True)
def database(monkeypatch, tmp_path):
    monkeypatch.setattr(store, 'DATA', tmp_path)
    monkeypatch.setattr(store, 'DB', tmp_path / 'library.sqlite3')
    monkeypatch.setattr(engine, 'pending', set())
    monkeypatch.setattr(engine, 'cancelled', set())
    monkeypatch.setattr(codex_bridge, 'data_dir', lambda: tmp_path)
    store.init()
    for name in ('media', 'documents'):
        (tmp_path / name).mkdir()


@pytest.mark.parametrize('entry', ['retranscribe', 'batch'])
@pytest.mark.parametrize('old_text', ['', '原有文稿'])
def test_download_then_transcribe_preserves_media_and_history(monkeypatch, entry, old_text):
    import yt_dlp
    import faster_whisper
    queued, recognized, downloads = [], [], []
    monkeypatch.setattr(engine, 'enqueue', queued.append)
    monkeypatch.setattr(engine, 'installed', lambda _: True)
    monkeypatch.setattr(engine, 'validate_url', lambda url: url)
    old_segments = [{'start': 0, 'end': 1, 'text': old_text}] if old_text else []
    item = store.add('下载后转录', source_url='https://example.test/video',
                     transcript=old_text, segments=old_segments)
    media = store.DATA / 'media' / item['id'] / 'source.wav'

    class YDL:
        def __init__(self, options): self.options = options
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def extract_info(self, *args, **kwargs):
            if not self.options.get('skip_download'):
                downloads.append(True)
                with wave.open(str(media), 'wb') as audio:
                    audio.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
                    audio.writeframes(b'\0\0' * 16000)
            return {'title': item['title'], 'duration': 1}
        def prepare_filename(self, info): return str(media)

    class Model:
        def __init__(self, *args, **kwargs): pass
        def transcribe(self, path, **kwargs):
            assert Path(path).is_file()
            recognized.append(path)
            return iter([SimpleNamespace(start=0, end=1, text='新的转录文稿')]), SimpleNamespace(duration=1, language='zh')

    monkeypatch.setattr(yt_dlp, 'YoutubeDL', YDL)
    monkeypatch.setattr(faster_whisper, 'WhisperModel', Model)
    # Download-only must retain its media even when the underlying preference is false.
    options = {'mode': 'audio', 'keep': False, 'subtitles': False, 'cover': False}
    response = client.post('/api/items/batch', json={'ids': [item['id']], 'action': 'assets', 'options': options})
    assert response.status_code == 200
    engine.process(queued.pop())
    downloaded = store.get(item['id'])
    assert downloaded['phase'] == '采集完成' and downloaded['transcript'] == old_text
    assert media.is_file() and not recognized
    original_bytes = media.read_bytes()

    if entry == 'retranscribe':
        response = client.post(f"/api/items/{item['id']}/retranscribe")
    else:
        response = client.post('/api/items/batch', json={'ids': [item['id']], 'action': 'transcribe',
                                                       'options': {**options, 'keep': True}})
    assert response.status_code == 200
    engine.process(queued.pop())
    result = store.get(item['id'])
    assert result['status'] == 'done' and result['transcript'] == '新的转录文稿'
    assert len(recognized) == 1 and len(downloads) == 1
    assert media.read_bytes() == original_bytes and result['media_path'] == str(media)
    if entry == 'retranscribe' and old_text:
        version = store.versions(item['id'])[0]
        restored = client.post(f"/api/items/{item['id']}/versions/{version['id']}/restore")
        assert restored.status_code == 200
        assert restored.json()['transcript'] == old_text and restored.json()['segments'] == old_segments
        assert media.read_bytes() == original_bytes


@pytest.mark.parametrize('markers', [{'download_only': True}, {'action': 'assets'}])
def test_retranscribe_converts_either_legacy_download_marker(monkeypatch, markers):
    item = store.add('旧下载任务', source_url='https://example.test/video', metadata=markers)
    queued = []
    monkeypatch.setattr(engine, 'enqueue', queued.append)
    assert client.post(f"/api/items/{item['id']}/retranscribe").status_code == 200
    converted = store.get(item['id'])
    assert not converted['metadata'].get('download_only')
    assert converted['metadata'].get('action') != 'assets'
    assert converted['phase'] == '等待转录' and queued == [item['id']]


@pytest.fixture(scope='module')
def synthetic_video(tmp_path_factory):
    if not shutil.which('ffmpeg'):
        pytest.skip('FFmpeg is required for synthetic video verification')
    path = tmp_path_factory.mktemp('video-fixture') / 'fixture.mp4'
    subprocess.run(['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-f', 'lavfi', '-i',
                    'color=c=teal:s=160x90:d=2', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(path)],
                   check=True, capture_output=True, timeout=30)
    return path


@pytest.mark.parametrize('keep,cache', [(True, False), (False, True), (False, False)])
def test_reacquire_retains_playable_video_only_when_requested(monkeypatch, synthetic_video, keep, cache):
    store.save_settings({'codex_keep_video': str(cache).lower()})
    options = {'mode': 'audio' if cache else 'video', 'keep': keep, 'cover': False, 'subtitles': False}
    item = store.add('仅有文稿的作品', status='done', source_url='https://example.test/video',
                     transcript='已有文稿', duration=2, metadata={'provider': 'douyin',
                     'direct_url': 'https://example.test/synthetic-video', 'options': options})
    assert not codex_bridge.asset({'item_id': item['id']})['has_media']
    transfers = []
    def transfer(url, path, *args):
        transfers.append(url)
        shutil.copyfile(synthetic_video, path)
    monkeypatch.setattr(assets, 'direct_download', transfer)
    acquired, _ = assets.acquire(item)
    assets.finish(item['id'])
    result = store.get(item['id'])
    assert len(transfers) == 1 and result['transcript'] == item['transcript']
    if keep or cache:
        assert Path(result['media_path']).is_file()
        assert not result['metadata']['temporary_media']
        assert client.get(f"/api/items/{item['id']}/media").status_code == 200
        assert codex_bridge.asset({'item_id': item['id']})['has_video_file']
        assert codex_bridge.video_frame({'item_id': item['id'], 'seconds': 1})['mimeType'] == 'image/jpeg'
    else:
        assert result['media_path'] == '' and not Path(acquired['media_path']).exists()
        assert not codex_bridge.asset({'item_id': item['id']})['has_media']


def test_cover_only_with_codex_cache_does_not_download_video(monkeypatch):
    store.save_settings({'codex_keep_video': 'true'})
    item = store.add('仅封面', source_url='https://example.test/video', metadata={
        'action': 'assets', 'download_only': True, 'provider': 'douyin',
        'direct_url': 'https://example.test/video-file', 'cover_url': 'https://example.test/cover',
        'options': {'mode': 'audio', 'keep': False, 'media': False, 'cover': True}})
    transfers = []
    def transfer(url, path, *args):
        transfers.append(url)
        path.write_bytes(b'cover fixture')
    monkeypatch.setattr(assets, 'direct_download', transfer)
    engine.process(item['id'])
    result = store.get(item['id'])
    assert transfers == ['https://example.test/cover'] and result['media_path'] == ''
    assert Path(result['metadata']['cover_path']).is_file()
