"""Isolated, deterministic checks for model contracts and destructive edit boundaries."""
import json
import os
import tempfile

os.environ['LULU_DATA_DIR'] = tempfile.mkdtemp(prefix='lulu-review-tests-')

import httpx
import pytest
from fastapi.testclient import TestClient
from backend import store, review, secrets, integrations
from backend.main import app

client = TestClient(app, headers={'X-Lulu-Client': 'desktop'})
RealClient = httpx.Client


@pytest.fixture(autouse=True)
def database(monkeypatch, tmp_path):
    monkeypatch.setattr(store, 'DATA', tmp_path)
    monkeypatch.setattr(store, 'DB', tmp_path / 'test.sqlite3')
    store.init()
    store.save_settings({'jev_api_key_enc': secrets.seal('jev-test-only'),
                         'llm_url': 'https://text.example/v1', 'llm_model': 'test-text'})


def mock_models(monkeypatch, choice='problem', confidence=.95, malformed=False):
    requests = []
    def handler(request):
        payload = json.loads(request.content)
        requests.append((request, payload))
        if str(request.url) == review.ENDPOINT:
            answers = {}
            for name, question in payload['questions'].items():
                selected = 'english' if name == 'language' else choice
                criteria = question['criteria']
                answers[name] = {'type': 'choice', 'choice': selected, 'confidence': confidence,
                    'probabilities': {key: 1 if key == selected else 0 for key in criteria}}
            return httpx.Response(200, json={'model': review.MODEL, 'answers': {} if malformed else answers})
        return httpx.Response(200, json={'choices': [{'message': {'content': '修正后的句子。'}, 'finish_reason': 'stop'}]})
    monkeypatch.setattr(review.httpx, 'Client', lambda **kw: RealClient(transport=httpx.MockTransport(handler), **kw))
    return requests


def document(timed=False):
    return store.add('测试口播', transcript='原来的句子。\n第二句。', status='done',
        segments=[{'start': 0, 'end': 2, 'text': '原来的句子。'}, {'start': 2, 'end': 4, 'text': '第二句。'}] if timed else [])


def check(item, **body):
    response = client.post(f"/api/items/{item['id']}/review", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def action(item, report, name):
    return client.post(f"/api/items/{item['id']}/review/{name}", json={'report_id': report['id'], 'issue_id': report['issues'][0]['id']})


def test_jev_key_is_independent_and_redacted(monkeypatch):
    calls = mock_models(monkeypatch)
    settings = client.get('/api/state').json()['settings']
    assert settings['has_jev_key'] and 'jev_api_key_enc' not in settings
    response = client.post('/api/jev/test', json={'api_key': 'temporary-key'})
    assert response.status_code == 200
    assert calls[0][0].headers['authorization'] == 'Bearer temporary-key'
    assert secrets.unseal(store.settings()['jev_api_key_enc']) == 'jev-test-only'
    bad = client.post('/api/jev/test', json={'api_key': {'secret': 'never-echo-me'}})
    assert bad.status_code == 422 and 'never-echo-me' not in bad.text
    client.put('/api/jev/settings', json={'api_key': 'replacement'})
    assert store.settings()['llm_model'] == 'test-text'


def test_no_key_never_uses_text_provider_key(monkeypatch):
    mock_models(monkeypatch)
    store.save_settings({'jev_api_key_enc': '', 'llm_api_key_enc': secrets.seal('text-only')})
    response = client.post('/api/jev/test', json={})
    assert response.status_code == 400 and 'TypeSafe' in response.text


def test_missing_original_does_not_claim_fidelity(monkeypatch):
    calls = mock_models(monkeypatch, choice='ok')
    report = check(document())
    assert report['original'] == '' and report['issues'] == []
    assert all(key.endswith('_clarity') for key in calls[0][1]['questions'])


def test_low_confidence_is_manual_review(monkeypatch):
    mock_models(monkeypatch, choice='ok', confidence=.3)
    item = document()
    report = check(item)
    assert all(i['uncertain'] and i['label'] == '待人工核对' for i in report['issues'])
    assert action(item, report, 'suggest').status_code == 400


def test_broken_response_does_not_replace_saved_report(monkeypatch):
    item = document()
    mock_models(monkeypatch)
    report = check(item)
    mock_models(monkeypatch, malformed=True)
    response = client.post(f"/api/items/{item['id']}/review", json={})
    assert response.status_code == 400
    assert store.object_get('review', item['id'])['id'] == report['id']


def test_apply_keeps_original_version_and_timing(monkeypatch):
    mock_models(monkeypatch)
    item = document(timed=True)
    report = check(item)
    assert action(item, report, 'suggest').status_code == 200
    assert store.get(item['id'])['transcript'] == item['transcript']
    assert action(item, report, 'apply').status_code == 200
    saved = store.get(item['id'])
    assert saved['transcript'] == '修正后的句子。\n第二句。'
    assert saved['segments'][0] == {'start': 0, 'end': 2, 'text': '修正后的句子。'}
    version = store.version(item['id'], store.versions(item['id'])[0]['id'])
    assert version['transcript'] == item['transcript'] and version['segments'] == item['segments']
    assert client.get(f"/api/items/{item['id']}/review").json()['report']['stale']
    assert action(item, report, 'apply').status_code == 400
    fresh = check(saved)
    assert fresh['id'] != report['id'] and not fresh['stale']


@pytest.mark.parametrize('change', ['edit', 'draft', 'delete'])
def test_stale_or_unsaved_edits_are_never_overwritten(monkeypatch, change):
    mock_models(monkeypatch)
    item = document()
    report = check(item)
    assert action(item, report, 'suggest').status_code == 200
    if change == 'edit':
        store.update(item['id'], transcript='用户的新内容')
    elif change == 'delete':
        store.update(item['id'], deleted=1)
    else:
        with store.connection() as con:
            con.execute('INSERT INTO drafts VALUES (?,?)', (item['id'], '{}'))
    assert action(item, report, 'apply').status_code == 400
    assert not store.versions(item['id'])


def test_actual_processing_input_is_snapshotted(monkeypatch):
    monkeypatch.setattr(integrations, 'process_text', lambda *args: '改写结果')
    source = document()
    output = client.post('/api/process-text', json={'text': '编辑后的实际原文', 'source_id': source['id']}).json()
    store.update(source['id'], transcript='后来修改的原文')
    assert store.object_get('text_origin', output['id'])['text'] == '编辑后的实际原文'
    mock_models(monkeypatch)
    report = check(output)
    assert report['original'] == '编辑后的实际原文'
    assert any(i['dimension'] == 'fidelity' for i in report['issues'])


def test_explicit_original_snapshot_and_long_document(monkeypatch):
    calls = mock_models(monkeypatch)
    item = document()
    source = store.add('对照原稿', transcript='对照资料', status='done')
    report = check(item, source_id=source['id'])
    store.update(source['id'], transcript='后来改过')
    assert report['original'] == '对照资料'
    before = len(calls)
    store.update(item['id'], transcript='中' * 8000)
    response = client.post(f"/api/items/{item['id']}/review", json={})
    assert response.status_code == 200 and len(calls) > before
    sent = [p for _, p in calls[before:]]
    assert ''.join(part['text'] for p in sent for part in p['state']['paragraphs']) == '中' * 8000
    assert all(len(json.dumps(p, ensure_ascii=False).encode()) < 32000 for p in sent)


def test_more_than_80_paragraphs_are_all_checked(monkeypatch):
    calls = mock_models(monkeypatch, choice='ok')
    text = '\n'.join(f'第{i}行。' for i in range(101))
    item = store.add('长文稿', transcript=text, status='done')
    report = check(item)
    assert report['checked_paragraphs'] == 101 and not report['issues']
    observed = [part for _, p in calls for part in p['state']['paragraphs']]
    assert len(observed) == 101
    assert all(text[p['start']:p['end']] == p['text'] for p in observed)
    progress = client.get(f"/api/items/{item['id']}/review/progress").json()
    assert progress['status'] == 'done' and progress['completed'] == progress['total'] == 26


def test_original_tail_is_checked_and_unsupported_is_aggregated(monkeypatch):
    item = document()
    original = '资料' * 2500 + '末尾依据'
    store.object_put('text_origin', item['id'], {'text': original})
    seen = []
    def evaluate(state, questions):
        seen.append(state['original'])
        return {'model': 'test', 'answers': {name: {'choice': ('ok' if state['original'].endswith('末尾依据') else 'unsupported') if name.endswith('fidelity') else 'ok', 'confidence': .95} for name in questions}}
    monkeypatch.setattr(review, 'evaluate', evaluate)
    report = check(item)
    assert ''.join(seen) == original
    assert not report['issues']
    assert report['source_chunks'] == 3


def test_conflicting_source_evidence_requires_manual_review():
    assert review.merge_fidelity([{'choice': 'ok', 'confidence': .95}, {'choice': 'problem', 'confidence': .95}])['choice'] == 'uncertain'
    assert review.merge_fidelity([{'choice': 'unsupported', 'confidence': .95}, {'choice': 'uncertain', 'confidence': .3}])['choice'] == 'uncertain'


def test_split_caption_correction_preserves_surrounding_text(monkeypatch):
    mock_models(monkeypatch)
    text = '甲' * 1300
    item = store.add('超长字幕', transcript=text, status='done', segments=[{'start': 0, 'end': 20, 'text': text}])
    report = check(item)
    issue = report['issues'][0]
    assert issue['end'] == 600
    assert action(item, report, 'suggest').status_code == 200
    assert action(item, report, 'apply').status_code == 200
    result = store.get(item['id'])
    assert result['transcript'] == '修正后的句子。' + '甲' * 700
    assert result['segments'][0] == {'start': 0, 'end': 20, 'text': result['transcript']}


def test_failed_batch_preserves_last_complete_report(monkeypatch):
    mock_models(monkeypatch)
    item = document()
    old = check(item)
    store.update(item['id'], transcript='甲' * 9000)
    count = 0
    def fail(state, questions):
        nonlocal count
        count += 1
        if count == 2:
            raise ValueError('模拟超时')
        return {'model': 'test', 'answers': {name: {'choice': 'ok', 'confidence': .95} for name in questions}}
    monkeypatch.setattr(review, 'evaluate', fail)
    response = client.post(f"/api/items/{item['id']}/review", json={})
    assert response.status_code == 400
    assert store.object_get('review', item['id'])['id'] == old['id']
    assert store.object_get('review_progress', item['id'])['status'] == 'error'
    assert item['id'] not in review.running
