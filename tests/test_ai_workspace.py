"""AI workspace state, cancellation and protected writes with offline model replies."""
import asyncio
from contextlib import asynccontextmanager
import os
import tempfile
import time

os.environ['LULU_DATA_DIR'] = tempfile.mkdtemp(prefix='lulu-ai-workspace-')

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from backend import ai_workspace as ai, store, engine, integrations


@asynccontextmanager
async def lifespan(app):
    ai.recover()
    yield
    await ai.shutdown()


app = FastAPI(lifespan=lifespan)
app.include_router(ai.router)


@app.exception_handler(ValueError)
async def value_error(request, error):
    return JSONResponse({'detail': str(error)}, status_code=400)


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(store, 'DATA', tmp_path)
    monkeypatch.setattr(store, 'DB', tmp_path / 'library.sqlite3')
    monkeypatch.setattr(ai, 'tasks', {})
    monkeypatch.setattr(engine, 'pending', set())
    store.init()
    store.save_settings({'llm_url': 'https://example.test/v1', 'llm_model': 'offline-test-model'})
    monkeypatch.setattr(integrations, 'llm_headers', lambda *args: {'Authorization': 'Bearer NEVER_EXPOSE'})
    with TestClient(app) as value:
        yield value


def source(text='原始文稿'):
    return store.add('来源标题', kind='document', transcript=text, status='done', progress=100)


def workspace(client, item=None, text='待处理文稿'):
    result = client.post('/api/ai/workspaces', json={'source_id': item['id']} if item else {'text': text, 'title': '手动稿'})
    assert result.status_code == 200, result.text
    return result.json()


def reply(monkeypatch, text):
    async def complete(*args): return text
    monkeypatch.setattr(ai, 'request_completion', complete)


def send(client, work, revision=None):
    result = client.post(f"/api/ai/workspaces/{work['id']}/runs", json={'revision': work['revision'] if revision is None else revision, 'instruction': '请校正全文', 'preset': 'correct'})
    assert result.status_code == 200, result.text
    return result.json()


def finished(client, work, expected='done'):
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        value = client.get(f"/api/ai/workspaces/{work['id']}").json()
        if value['run']['status'] not in ai.ACTIVE:
            assert value['run']['status'] == expected, value
            return value
        time.sleep(.01)
    pytest.fail('Offline job did not finish')


def test_open_and_autosave_make_no_model_request_and_detect_stale_revision(client, monkeypatch):
    async def forbidden(*args): pytest.fail('Opening and drafting must not call a model')
    monkeypatch.setattr(ai, 'request_completion', forbidden)
    item = source()
    work = workspace(client, item)
    assert work['text'] == item['transcript']
    endpoint = f"/api/ai/workspaces/{work['id']}/draft"
    body = {'title': '工作区草稿', 'text': '修正後的文稿', 'revision': 0}
    result = client.put(endpoint, json=body)
    assert result.status_code == 200 and result.json()['revision'] == 1
    assert store.get(item['id'])['transcript'] == '原始文稿'
    assert client.put(endpoint, json=body).status_code == 409
    read = client.get(f"/api/ai/workspaces/{work['id']}").json()
    assert read['text'] == '修正后的文稿' and not read['messages']
    result = client.put(f"/api/ai/workspaces/{work['id']}/composer", json={'instruction': '之后继续校对', 'preset': 'correct'})
    assert result.status_code == 200
    read = client.get(f"/api/ai/workspaces/{work['id']}").json()
    assert read['instruction'] == '之后继续校对' and read['revision'] == 1


def test_legacy_editor_draft_is_restored_atomically_without_overwriting_source(client):
    item = source('已经保存的正文')
    old = {'title': '上次未提交的标题', 'text': '上次未提交的正文', 'instruction': '保留全部数字', 'preset': 'outline'}
    result = client.post('/api/ai/workspaces', json={'source_id': item['id'], 'draft': old})
    assert result.status_code == 200
    work = client.get(f"/api/ai/workspaces/{result.json()['id']}").json()
    assert all(work[key] == value for key, value in old.items())
    assert work['source_id'] == item['id'] and store.get(item['id'])['transcript'] == '已经保存的正文'


def test_full_long_text_processed_without_auto_overwrite_or_secret_leak(client, monkeypatch):
    seen = []
    async def complete(client, base, headers, model, messages):
        assert headers['Authorization'] == 'Bearer NEVER_EXPOSE'
        seen.append(messages[-1]['content'])
        return f'這是第 {len(seen)} 段'
    monkeypatch.setattr(ai, 'request_completion', complete)
    item = source('前' * 5100 + '最后一句完整保留。')
    work = workspace(client, item)
    send(client, work)
    result = finished(client, work)
    assert len(seen) == 2 and '最后一句完整保留。' in seen[-1]
    assert result['run']['result'] == '这是第 1 段\n\n这是第 2 段'
    assert store.get(item['id'])['transcript'] == item['transcript']
    assert result['messages'][-1]['role'] == 'assistant'
    assert 'NEVER_EXPOSE' not in str(result)


def test_apply_preserves_timestamps_and_history(client, monkeypatch):
    item = source('第一句\nSecond sentence.')
    segments = [{'start': 0, 'end': 2, 'text': '第一句'}, {'start': 3, 'end': 5, 'text': 'Second sentence.'}]
    store.update(item['id'], segments=segments)
    work = workspace(client, item)
    reply(monkeypatch, '校正第一句\nCorrected second sentence.')
    run = send(client, work)
    finished(client, work)
    applied = client.post(f"/api/ai/workspaces/{work['id']}/results/{run['id']}/apply", json={'revision': 0})
    assert applied.status_code == 200, applied.text
    updated = store.get(item['id'])
    assert updated['transcript'].startswith('校正第一句')
    assert [(s['start'], s['end']) for s in updated['segments']] == [(0, 2), (3, 5)]
    history = store.versions(item['id'])
    assert len(history) == 1
    assert store.version(item['id'], history[0]['id'])['transcript'] == item['transcript']
    assert client.get(f"/api/ai/workspaces/{work['id']}").json()['text'] == updated['transcript']


@pytest.mark.parametrize('changed', ['source', 'title', 'draft'])
def test_result_cannot_overwrite_changed_source_or_draft(client, monkeypatch, changed):
    item = source()
    work = workspace(client, item)
    reply(monkeypatch, 'AI 新内容')
    run = send(client, work)
    finished(client, work)
    revision = 0
    if changed == 'source': store.update(item['id'], transcript='其他窗口的修改')
    elif changed == 'title': store.update(item['id'], title='其他窗口改了标题')
    else:
        client.put(f"/api/ai/workspaces/{work['id']}/draft", json={'title': work['title'], 'text': '稍后编辑的草稿', 'revision': 0})
        revision = 1
    before = store.get(item['id'])
    response = client.post(f"/api/ai/workspaces/{work['id']}/results/{run['id']}/apply", json={'revision': revision})
    assert response.status_code == 409
    assert store.get(item['id']) == before and store.versions(item['id']) == []


def test_structural_edit_requires_separate_output_and_save_is_idempotent(client, monkeypatch):
    item = source('第一句\n第二句')
    store.update(item['id'], segments=[{'start': 0, 'end': 1, 'text': '第一句'}, {'start': 1, 'end': 2, 'text': '第二句'}])
    work = workspace(client, item)
    reply(monkeypatch, '合并后的长句。')
    run = send(client, work)
    finished(client, work)
    path = f"/api/ai/workspaces/{work['id']}/results/{run['id']}"
    rejected = client.post(path + '/apply', json={'revision': 0})
    assert rejected.status_code == 400 and '时间轴' in rejected.json()['detail']
    first = client.post(path + '/save-output').json()
    second = client.post(path + '/save-output').json()
    assert first == second
    assert store.get(first['item_id'])['kind'] == 'output'
    assert store.get(first['item_id'])['transcript'] == '合并后的长句。'
    assert store.object_get('text_origin', first['item_id'])['text'] == item['transcript']
    assert store.get(item['id'])['transcript'] == item['transcript']


def test_cancel_request_does_not_create_output_or_overwrite(client, monkeypatch):
    async def slow(*args):
        await asyncio.sleep(30)
        return '不应写入'
    monkeypatch.setattr(ai, 'request_completion', slow)
    work = workspace(client)
    send(client, work)
    response = client.post(f"/api/ai/workspaces/{work['id']}/cancel")
    assert response.status_code == 200 and response.json()['run']['status'] == 'cancelled'
    value = finished(client, work, 'cancelled')
    assert value['run']['result'] == ''
    assert not any(m['role'] == 'assistant' for m in value['messages'])
    assert store.items() == []


def test_cancel_after_completion_reports_completed_result(client, monkeypatch):
    work = workspace(client)
    reply(monkeypatch, '已经完成')
    send(client, work)
    finished(client, work)
    response = client.post(f"/api/ai/workspaces/{work['id']}/cancel").json()
    assert response['run']['status'] == 'done' and response['run']['result'] == '已经完成'


def test_context_uses_last_ten_messages_and_refuses_oversized_context(client, monkeypatch):
    work = workspace(client)
    work['messages'] = [{'role': 'user' if i % 2 == 0 else 'assistant', 'text': f'历史消息{i}'} for i in range(12)]
    store.object_put('ai_workspace', work['id'], work)
    sent = []
    async def complete(client, base, headers, model, messages):
        sent.extend(messages)
        return '完成'
    monkeypatch.setattr(ai, 'request_completion', complete)
    send(client, work)
    finished(client, work)
    assert [message['content'] for message in sent[1:-1]] == [f'历史消息{i}' for i in range(2, 12)]
    value = store.object_get('ai_workspace', work['id'])
    value['messages'] = [{'role': 'assistant', 'text': '长' * 60001}]
    store.object_put('ai_workspace', work['id'], value)
    response = client.post(f"/api/ai/workspaces/{work['id']}/runs", json={'revision': 0, 'instruction': '继续'})
    assert response.status_code == 400 and '新建对话' in response.json()['detail']
    assert len(sent) == 12


def test_manual_document_save_creates_once_then_versions_changes(client):
    work = workspace(client)
    path = f"/api/ai/workspaces/{work['id']}"
    first = client.post(path + '/save-document', json={'revision': 0})
    assert first.status_code == 200
    item_id = first.json()['item_id']
    assert client.post(path + '/save-document', json={'revision': 0}).json()['item_id'] == item_id
    client.put(path + '/draft', json={'revision': 0, 'title': '修改后的标题', 'text': '修改后的文稿'})
    assert client.post(path + '/save-document', json={'revision': 1}).status_code == 200
    assert store.get(item_id)['transcript'] == '修改后的文稿'
    assert len(store.versions(item_id)) == 1


def test_recovery_never_auto_resends_and_failed_job_can_retry(client, monkeypatch):
    async def fail(*args): raise RuntimeError('https://secret.example?key=NEVER_EXPOSE')
    monkeypatch.setattr(ai, 'request_completion', fail)
    work = workspace(client)
    run = send(client, work)
    failed = finished(client, work, 'error')
    assert 'NEVER_EXPOSE' not in str(failed)
    saved = store.object_get('ai_run', run['id'])
    saved['status'] = 'running'
    store.object_put('ai_run', run['id'], saved)
    ai.recover()
    assert store.object_get('ai_run', run['id'])['status'] == 'interrupted'
    reply(monkeypatch, '重试成功')
    send(client, work)
    assert finished(client, work)['run']['result'] == '重试成功'


def test_result_id_cannot_be_used_from_other_conversation(client, monkeypatch):
    first, second = workspace(client), workspace(client)
    reply(monkeypatch, '结果')
    run = send(client, first)
    finished(client, first)
    assert client.post(f"/api/ai/workspaces/{second['id']}/results/{run['id']}/save-output").status_code == 404
