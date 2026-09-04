import io
import os
import tempfile
import wave

os.environ['LULU_DATA_DIR'] = tempfile.mkdtemp(prefix='lulu-test-')
from fastapi.testclient import TestClient
from backend.main import app
from backend import store, engine

client = TestClient(app, headers={'X-Lulu-Client': 'desktop'})


def test_import_edit_and_export_timestamped_transcript():
    source = '1\n00:00:01,200 --> 00:00:03,400\n你好，世界。\n\n2\n00:00:04,000 --> 00:00:05,800\n第二句话。\n'
    result = client.post('/api/import', files={'files': ('测试.srt', source.encode('utf-8-sig'), 'text/plain')})
    assert result.status_code == 200, result.text
    item = result.json()[0]
    assert len(item['segments']) == 2
    assert item['segments'][0]['start'] == 1.2
    item['segments'][0]['text'] = '编辑后的第一句话。'
    assert client.patch('/api/items/' + item['id'], json={'segments': item['segments']}).status_code == 200
    subtitle = client.get(f'/api/items/{item["id"]}/export/srt').content.decode('utf-8-sig')
    assert '00:00:01,200 --> 00:00:03,400' in subtitle
    assert '编辑后的第一句话。' in subtitle
    assert '第二句话。' in client.get(f'/api/items/{item["id"]}').json()['transcript']
    # Plain text edits must not export obsolete timed subtitles.
    result = client.patch('/api/items/' + item['id'], json={'transcript': '改写后的全文'})
    assert result.json()['segments'] == []
    assert client.get(f'/api/items/{item["id"]}/export/srt').status_code == 400
    assert client.get(f'/api/items/{item["id"]}/export/txt').content.decode('utf-8-sig') == '改写后的全文'


def test_invalid_subtitle_does_not_create_a_document():
    before = len(store.items())
    response = client.post('/api/import', files={'files': ('invalid.srt', b'no timestamps', 'text/plain')})
    assert response.status_code == 400
    assert len(store.items()) == before


def test_cross_site_mutation_and_host_are_rejected():
    response = client.post('/api/documents', json={'title': 'test', 'text': 'body'}, headers={'Origin': 'https://example.com'})
    assert response.status_code == 403
    assert client.get('/api/state', headers={'Host': 'evil.test'}).status_code == 403
    raw = TestClient(app)
    assert raw.post('/api/documents', json={'title': 'test', 'text': 'body'}).status_code == 403


def test_secret_is_encrypted_and_not_returned():
    response = client.patch('/api/settings', json={'feishu_secret': 'test-secret-never-plaintext', 'llm_api_key': 'test-key'})
    assert response.status_code == 200
    response = client.get('/api/state')
    assert response.json()['settings']['has_feishu_secret'] is True
    assert 'test-secret-never-plaintext' not in response.text
    assert '_enc' not in response.text
    assert 'test-secret-never-plaintext' not in str(store.settings())


def test_restart_keeps_documents_and_pauses_interrupted_jobs():
    doc = store.add('保留的文稿', transcript='正文', status='done')
    job = store.add('被中断的任务', status='processing', transcript='已识别片段')
    store.init()
    assert store.get(doc['id'])['status'] == 'done'
    assert store.get(doc['id'])['transcript'] == '正文'
    assert store.get(job['id'])['status'] == 'paused'
    assert store.get(job['id'])['transcript'] == '已识别片段'


def test_soft_delete_keeps_source_and_can_restore():
    file = store.DATA / 'media' / 'keep.wav'
    file.write_bytes(b'source')
    item = store.add('source', media_path=str(file))
    assert client.delete('/api/items/' + item['id']).status_code == 200
    assert file.exists()
    assert client.get('/api/items/' + item['id']).status_code == 404
    assert client.post(f'/api/items/{item["id"]}/restore').status_code == 200


def test_csv_does_not_execute_spreadsheet_formulas():
    store.add('=1+2', transcript='@danger')
    text = client.get('/api/export-csv').content.decode('utf-8-sig')
    assert "'=1+2" in text and "'@danger" in text


def test_clip_has_requested_duration_and_preserves_original():
    buffer = io.BytesIO()
    with wave.open(buffer, 'wb') as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(b'\0\0' * 16000 * 3)
    uploaded = client.post('/api/import', files={'files': ('audio.wav', buffer.getvalue(), 'audio/wav')}).json()[0]
    response = client.post('/api/clip', json={'item_id': uploaded['id'], 'start': .5, 'end': 2})
    assert response.status_code == 200, response.text
    clip = response.json()
    assert abs(engine.media_info(clip['media_path']) - 1.5) < .02
    assert abs(engine.media_info(uploaded['media_path']) - 3) < .02


def test_model_and_timestamps_are_validated():
    assert client.post('/api/models/unknown/install').status_code == 400
    assert client.patch('/api/settings', json={'model': '../secret'}).status_code == 400
    item = store.add('x')
    assert client.patch('/api/items/' + item['id'], json={'segments': [{'start': 3, 'end': 1, 'text': 'x'}]}).status_code == 400


def test_draft_survives_database_reopen_without_changing_saved_document():
    doc = store.add('draft-test', transcript='original')
    key = doc['id']
    assert client.put('/api/drafts/' + key, json={'text': 'unsaved draft', 'segments': [], 'tab': 'text'}).status_code == 200
    store.init()
    assert client.get('/api/drafts/' + key).json()['text'] == 'unsaved draft'
    assert store.get(key)['transcript'] == 'original'
    client.delete('/api/drafts/' + key)
    assert client.get('/api/drafts/' + key).json() is None
