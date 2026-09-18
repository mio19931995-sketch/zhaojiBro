"""Creator content protection, with a separate temporary database for every test."""
import os
import tempfile
import json
from pathlib import Path
from types import SimpleNamespace

os.environ['LULU_DATA_DIR'] = tempfile.mkdtemp(prefix='lulu-creator-tests-')

import pytest
from fastapi.testclient import TestClient
from backend import store, engine
from backend.main import app

client = TestClient(app, headers={'X-Lulu-Client': 'desktop'})


@pytest.fixture(autouse=True)
def isolated_database(monkeypatch, tmp_path):
    monkeypatch.setattr(store, 'DATA', tmp_path)
    monkeypatch.setattr(store, 'DB', tmp_path / 'library.sqlite3')
    monkeypatch.setattr(engine, 'pending', set())
    monkeypatch.setattr(engine, 'cancelled', set())
    store.init()


def timed_item():
    return store.add('视频原稿', transcript='第一句话\nSecond sentence.', status='done', progress=100,
                     source_url='https://example.test/video',
                     segments=[{'start': 1.25, 'end': 3.5, 'text': '第一句话'},
                               {'start': 4, 'end': 6, 'text': 'Second sentence.'}])


def test_fulltext_correction_preserves_timing_and_archives_original():
    item = timed_item()
    result = client.patch(f"/api/items/{item['id']}", json={'transcript': '修正第一句\nCorrected second sentence.'})
    assert result.status_code == 200, result.text
    segments = result.json()['segments']
    assert [(s['start'], s['end']) for s in segments] == [(1.25, 3.5), (4, 6)]
    assert [s['text'] for s in segments] == ['修正第一句', 'Corrected second sentence.']
    history = client.get(f"/api/items/{item['id']}/versions").json()
    assert len(history) == 1 and history[0]['segment_count'] == 2
    assert 'transcript' not in history[0] and 'segments' not in history[0]
    saved = client.get(f"/api/items/{item['id']}/versions/{history[0]['id']}").json()
    assert saved['transcript'] == item['transcript'] and saved['segments'] == item['segments']


def test_changed_line_count_is_rejected_without_mutating_content_or_history():
    item = timed_item()
    result = client.patch(f"/api/items/{item['id']}", json={'transcript': '全部合成一句'})
    assert result.status_code == 400 and '时间轴逐句校对' in result.json()['detail']
    assert store.get(item['id']) == item
    assert store.versions(item['id']) == []


def test_explicit_conversion_retains_recoverable_timestamps():
    item = timed_item()
    result = client.patch(f"/api/items/{item['id']}", json={'transcript': '另一个完整口播稿', 'clear_timestamps': True})
    assert result.status_code == 200 and result.json()['segments'] == []
    history = store.versions(item['id'])
    assert len(history) == 1
    assert store.version(item['id'], history[0]['id'])['segments'] == item['segments']


def test_timeline_edits_are_versioned_but_identical_saves_are_not():
    item = timed_item()
    assert client.patch(f"/api/items/{item['id']}", json={'segments': item['segments']}).status_code == 200
    assert store.versions(item['id']) == []
    changed = [{**item['segments'][0], 'text': '已校对'}, item['segments'][1]]
    result = client.patch(f"/api/items/{item['id']}", json={'segments': changed})
    assert result.status_code == 200 and result.json()['transcript'].startswith('已校对\n')
    assert len(store.versions(item['id'])) == 1
    empty = store.add('空文稿')
    assert client.patch(f"/api/items/{empty['id']}", json={'transcript': '首稿'}).status_code == 200
    assert store.versions(empty['id']) == []


def test_edit_and_snapshot_are_atomic(monkeypatch):
    item = timed_item()
    def failing_update(*args, **kwargs):
        raise RuntimeError('simulated write failure')
    monkeypatch.setattr(store, '_update', failing_update)
    with pytest.raises(RuntimeError, match='simulated write failure'):
        store.update_with_snapshot(item['id'], '编辑文稿前', transcript='changed')
    assert store.get(item['id']) == item
    assert store.versions(item['id']) == []


def test_retranscribe_archives_before_enqueue(monkeypatch):
    item = timed_item()
    queued = []
    def enqueue(item_id):
        history = store.versions(item_id)
        assert len(history) == 1 and history[0]['reason'] == '重新转录前'
        assert store.version(item_id, history[0]['id'])['transcript'] == item['transcript']
        assert store.get(item_id)['transcript'] == ''
        queued.append(item_id)
    monkeypatch.setattr(engine, 'enqueue', enqueue)
    result = client.post(f"/api/items/{item['id']}/retranscribe")
    assert result.status_code == 200 and queued == [item['id']]


def test_restore_version_preserves_current_version_clears_error_and_draft():
    item = timed_item()
    store.update_with_snapshot(item['id'], '编辑文稿前', transcript='改后的文本', segments=[])
    version_id = store.versions(item['id'])[0]['id']
    store.update(item['id'], status='error', error='识别失败', phase='处理失败', progress=12)
    client.put(f"/api/drafts/{item['id']}", json={'text': '过期草稿'})
    result = client.post(f"/api/items/{item['id']}/versions/{version_id}/restore")
    assert result.status_code == 200, result.text
    restored = result.json()
    assert restored['transcript'] == item['transcript'] and restored['segments'] == item['segments']
    assert restored['status'] == 'done' and restored['progress'] == 100 and restored['error'] == ''
    assert client.get(f"/api/drafts/{item['id']}").json() is None
    newest = store.versions(item['id'])[0]
    assert newest['reason'] == '恢复历史版本前'
    assert store.version(item['id'], newest['id'])['transcript'] == '改后的文本'


def test_restoring_after_empty_failed_retranscription_can_be_undone(monkeypatch):
    item = timed_item()
    monkeypatch.setattr(engine, 'enqueue', lambda item_id: None)
    assert client.post(f"/api/items/{item['id']}/retranscribe").status_code == 200
    original_version = store.versions(item['id'])[0]['id']
    store.update(item['id'], status='error', phase='处理失败', error='识别失败')
    assert store.get(item['id'])['transcript'] == ''

    recovered = client.post(f"/api/items/{item['id']}/versions/{original_version}/restore")
    assert recovered.status_code == 200 and recovered.json()['transcript'] == item['transcript']
    assert recovered.json()['status'] == 'done' and recovered.json()['error'] == ''
    empty_version = store.versions(item['id'])[0]
    assert empty_version['reason'] == '恢复历史版本前'
    assert empty_version['characters'] == 0 and empty_version['segment_count'] == 0

    undone = client.post(f"/api/items/{item['id']}/versions/{empty_version['id']}/restore")
    assert undone.status_code == 200
    assert undone.json()['transcript'] == '' and undone.json()['segments'] == []
    assert undone.json()['status'] == 'paused' and undone.json()['progress'] == 0
    assert undone.json()['error'] == '' and undone.json()['phase'] == '已恢复空文稿，可重新转录'
    newest = store.versions(item['id'])[0]
    assert store.version(item['id'], newest['id'])['transcript'] == item['transcript']


@pytest.mark.parametrize('busy_state', ['pending', 'processing', 'queued'])
def test_restore_version_is_blocked_during_processing(busy_state):
    item = timed_item()
    store.update_with_snapshot(item['id'], '编辑文稿前', transcript='changed', segments=[])
    version_id = store.versions(item['id'])[0]['id']
    if busy_state == 'pending':
        engine.pending.add(item['id'])
    else:
        store.update(item['id'], status=busy_state)
    assert client.post(f"/api/items/{item['id']}/versions/{version_id}/restore").status_code == 400
    assert store.get(item['id'])['transcript'] == 'changed'
    assert len(store.versions(item['id'])) == 1


def test_versions_cannot_be_restored_across_items():
    first, second = timed_item(), timed_item()
    store.update_with_snapshot(first['id'], '编辑文稿前', transcript='changed', segments=[])
    version_id = store.versions(first['id'])[0]['id']
    assert client.get(f"/api/items/{second['id']}/versions/{version_id}").status_code == 404
    assert client.post(f"/api/items/{second['id']}/versions/{version_id}/restore").status_code == 404
    assert store.get(second['id']) == second


def test_trash_list_is_lightweight_and_batch_restore_does_not_enqueue(monkeypatch):
    first, second = timed_item(), timed_item()
    store.update(first['id'], deleted=1)
    store.update(second['id'], deleted=1, status='queued')
    def unexpected_enqueue(*args):
        pytest.fail('Restoring trash must never start tasks')
    monkeypatch.setattr(engine, 'enqueue', unexpected_enqueue)
    entries = client.get('/api/trash').json()
    assert {e['id'] for e in entries} == {first['id'], second['id']}
    assert all('transcript' not in e and 'segments' not in e and 'deleted_at' in e for e in entries)
    result = client.post('/api/trash/restore', json={'ids': [first['id'], second['id'], first['id']]})
    assert result.status_code == 200 and result.json()['count'] == 2
    assert store.get(first['id'])['status'] == 'done'
    assert store.get(second['id'])['status'] == 'paused'
    assert client.get('/api/trash').json() == []


def test_invalid_restore_is_atomic_and_never_creates_unknown_items():
    item = timed_item()
    store.update(item['id'], deleted=1)
    result = client.post('/api/trash/restore', json={'ids': [item['id'], 'does-not-exist']})
    assert result.status_code == 400 and store.get(item['id']) is None
    assert client.post('/api/items/does-not-exist/restore').status_code == 400
    assert store.get('does-not-exist') is None
    assert client.post('/api/trash/restore', json={'ids': []}).status_code == 422
    assert client.post(f"/api/items/{item['id']}/restore").status_code == 200
    assert client.post('/api/trash/restore', json={'ids': [item['id']]}).status_code == 400


def test_voice_output_records_source_and_generation_settings(monkeypatch):
    source = timed_item()
    (store.DATA / 'outputs').mkdir()
    def synthesize(args, **kwargs):
        request = json.loads(Path(args[-1]).read_text(encoding='utf-8-sig'))
        Path(request['output']).write_bytes(b'fixture-wave')
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(engine, 'run_command', synthesize)
    monkeypatch.setattr(engine, 'media_info', lambda path: 2.5)
    result = client.post('/api/voice', json={'text': '配音正文', 'voice': 'test-voice', 'rate': 2,
                                            'source_id': source['id']})
    assert result.status_code == 200, result.text
    assert result.json()['metadata'] == {'source_id': source['id'], 'voice': 'test-voice', 'rate': 2}
    assert client.post('/api/voice', json={'text': '正文', 'source_id': 'does-not-exist'}).status_code == 404
