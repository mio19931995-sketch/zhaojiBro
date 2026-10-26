"""Real runtime loop using offline model responses, never production data."""
import asyncio
import copy
import json
import threading
import time

import httpx
import pytest

from tests.test_ai_workspace import client, workspace, source, send, finished
from tests.test_ai_context import configure, seed_history, summary
from backend import ai_workspace as ai, ai_runtime as runtime, ai_tools, ai_skills, store


def call(identity, name, arguments):
    return {'id': identity, 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(arguments)}}


def tools_reply(monkeypatch, responses, inspect=None):
    seen = []
    async def respond(_, base, headers, model, messages, schemas, max_output):
        seen.append(copy.deepcopy(messages))
        if inspect: inspect(messages, schemas, max_output)
        assert responses, 'Unexpected extra model request'
        return responses.pop(0)
    monkeypatch.setattr(runtime, 'request_tools', respond)
    return seen


def test_search_read_loop_and_historical_tool_evidence(client, monkeypatch):
    external = source('引用资料不会被修改')
    work = workspace(client)
    configure(client, work, tools_enabled=True, allowed_tools=['search_documents', 'read_document'])
    responses = [
        {'content': '', 'tool_calls': [call('search', 'search_documents', {'query': '引用资料'})]},
        {'content': '', 'tool_calls': [call('read', 'read_document', {'document_id': external['id'], 'offset': 0})]},
        {'content': '处理后的正文', 'tool_calls': []},
    ]
    seen = tools_reply(monkeypatch, responses)
    send(client, work)
    value = finished(client, work)
    assert len(seen) == 3
    assert seen[1][-2]['role'] == 'assistant' and seen[1][-2]['tool_calls'][0]['id'] == 'search'
    assert seen[1][-1]['role'] == 'tool' and seen[1][-1]['tool_call_id'] == 'search'
    assert '引用资料不会被修改' in seen[2][-1]['content']
    assert value['run']['tool_steps'] == 2 and all(event['state'] == 'completed' for event in value['run']['events'])
    assert store.get(external['id'])['transcript'] == external['transcript'] and value['text'] == work['text']
    assert sum(m['role'] == 'tool' for m in value['messages']) == 2
    configure(client, work)
    captured = []
    async def plain(*args): captured.extend(args[-1]); return '下一轮正文'
    monkeypatch.setattr(ai, 'request_completion', plain)
    send(client, work)
    finished(client, work)
    assert not any(m['role'] == 'tool' for m in captured)
    assert any('历史工具记录' in m['content'] for m in captured)


def test_create_document_is_reviewable_not_a_manuscript_replacement(client, monkeypatch):
    original = source('原稿保持')
    work = workspace(client, original)
    configure(client, work, tools_enabled=True, allowed_tools=['create_document'])
    tools_reply(monkeypatch, [{'content': '', 'tool_calls': [call('draft', 'create_document', {'title': '准备另存', 'markdown': '新文稿内容'})]}])
    run = send(client, work)
    value = finished(client, work)
    assert value['run']['non_edit_result'] and value['messages'][-1]['non_edit_result']
    assert value['run']['actions'][0]['state'] == 'pending'
    assert not value['run']['proposals'] and store.get(original['id'])['transcript'] == original['transcript']
    result = client.post(f"/api/ai/workspaces/{work['id']}/results/{run['id']}/apply", json={'revision': 0})
    assert result.status_code == 400 and '工具状态' in result.json()['detail']
    # Proposals endpoint remains a well-formed empty plan, not an uninitialized run error.
    assert client.get(f"/api/ai/workspaces/{work['id']}/results/{run['id']}/proposals").status_code == 200
    action = value['run']['actions'][0]
    response = client.post(f"/api/ai/workspaces/{work['id']}/runs/{run['id']}/actions/{action['id']}/confirm", json={'revision': 0})
    assert response.status_code == 200, response.text
    assert store.get(response.json()['document_id'])['transcript'] == '新文稿内容'
    assert store.get(original['id'])['transcript'] == original['transcript']


@pytest.mark.parametrize('bad', [
    call('bad', 'shell', {'command': 'never execute'}),
    call('same', 'read_targets', {'extra': True}),
    call('bad', 'read_document', {'document_id': 'unseen', 'offset': True}),
    {'id': 'bad', 'type': 'function', 'function': {'name': 'read_targets', 'arguments': '{invalid'}},
    {'id': 'bad', 'type': 'function', 'function': {'name': 'read_document', 'arguments': '{"document_id":"a","document_id":"b","offset":0}'}},
    {'id': 'bad', 'type': 'other', 'function': {'name': 'read_targets', 'arguments': '{}'}},
])
def test_invalid_batch_executes_none_even_with_valid_first_call(client, monkeypatch, bad):
    work = workspace(client)
    configure(client, work, tools_enabled=True, allowed_tools=['read_targets', 'read_document'])
    tools_reply(monkeypatch, [{'content': '', 'tool_calls': [call('same', 'read_targets', {}), bad]}])
    executed = []
    monkeypatch.setattr(ai_tools, 'execute', lambda *args: executed.append(args))
    send(client, work)
    value = finished(client, work, 'error')
    assert not executed and not value['run']['events']


def test_step_limit_is_total_calls_and_repeated_ids_are_rejected(client, monkeypatch):
    work = workspace(client)
    configure(client, work, tools_enabled=True, allowed_tools=['read_targets'])
    seen = tools_reply(monkeypatch, [{'content': '', 'tool_calls': [call(f'c-{i}', 'read_targets', {})]} for i in range(9)])
    send(client, work)
    value = finished(client, work, 'error')
    assert value['run']['tool_steps'] == 8 and len(value['run']['events']) == 8 and len(seen) == 9
    assert '8 步' in value['run']['error']
    work = workspace(client)
    configure(client, work, tools_enabled=True, allowed_tools=['read_targets'])
    tools_reply(monkeypatch, [{'content': '', 'tool_calls': [call('repeat', 'read_targets', {})]}] * 2)
    send(client, work)
    value = finished(client, work, 'error')
    assert value['run']['tool_steps'] == 1 and '重复' in value['run']['error']


@pytest.mark.parametrize('grant_extra', [False, True])
def test_current_permission_revocation_and_frozen_scope_block_execution(client, monkeypatch, grant_extra):
    work = workspace(client)
    configure(client, work, tools_enabled=True, allowed_tools=['read_targets'])
    started, released = threading.Event(), threading.Event()
    async def delayed(*args):
        started.set()
        while not released.is_set(): await asyncio.sleep(.01)
        return {'content': '', 'tool_calls': [call('call', 'list_tasks' if grant_extra else 'read_targets', {})]}
    monkeypatch.setattr(runtime, 'request_tools', delayed)
    send(client, work)
    assert started.wait(2)
    configure(client, work, tools_enabled=grant_extra, allowed_tools=['read_targets', 'list_tasks'] if grant_extra else [])
    released.set()
    value = finished(client, work, 'error')
    assert not value['run']['events'] and '权限' in value['run']['error']


def test_explicit_skill_without_tools_and_disable_during_model_request(client, monkeypatch):
    work = workspace(client)
    sent = []
    async def complete(*args): sent.extend(args[-1]); return '处理后的正文'
    monkeypatch.setattr(ai, 'request_completion', complete)
    endpoint = f"/api/ai/workspaces/{work['id']}/runs"
    assert client.post(endpoint, json={'revision': 0, 'instruction': '/lulu-polish 保留数字'}).status_code == 200
    value = finished(client, work)
    assert value['run']['skill'] == 'lulu-polish' and value['run']['loaded_skills'] == ['lulu-polish']
    assert '用户本轮选择的技能方法' in sent[0]['content']
    started, released = threading.Event(), threading.Event()
    async def delayed(*args):
        started.set()
        while not released.is_set(): await asyncio.sleep(.01)
        return '不应发布的新结果'
    monkeypatch.setattr(ai, 'request_completion', delayed)
    assert client.post(endpoint, json={'revision': 0, 'instruction': '继续', 'skill': 'lulu-polish'}).status_code == 200
    assert started.wait(2)
    assert client.patch('/api/ai/skills/lulu-polish', json={'enabled': False}).status_code == 200
    released.set()
    value = finished(client, work, 'error')
    assert '技能已停用' in value['run']['error']
    assert not any(message['text'] == '不应发布的新结果' for message in value['messages'])
    assert client.post(endpoint, json={'revision': 0, 'instruction': '/lulu-polish 改稿'}).status_code == 400


def test_cancel_pending_model_discards_late_calls(client, monkeypatch):
    work = workspace(client)
    configure(client, work, tools_enabled=True, allowed_tools=['create_document'])
    async def delayed(*args): await asyncio.sleep(10); return {'content': '', 'tool_calls': [call('late', 'create_document', {'title': '晚到', 'markdown': '不能写入'})]}
    monkeypatch.setattr(runtime, 'request_tools', delayed)
    send(client, work)
    assert client.post(f"/api/ai/workspaces/{work['id']}/cancel").status_code == 200
    value = finished(client, work, 'cancelled')
    assert not value['run']['events'] and not value['run']['actions']


def test_two_same_tool_failures_stop_with_truthful_status(client, monkeypatch):
    work = workspace(client)
    configure(client, work, tools_enabled=True, allowed_tools=['read_document'])
    seen = tools_reply(monkeypatch, [{'content': '', 'tool_calls': [call(f'guess-{i}', 'read_document', {'document_id': 'not-discovered', 'offset': 0})]} for i in range(2)])
    send(client, work)
    value = finished(client, work)
    assert len(seen) == 2 and value['run']['non_edit_result'] and not value['run']['proposals']
    assert all(event['state'] == 'error' for event in value['run']['events'])
    assert '先搜索资料' in value['run']['result']


def test_large_tool_result_is_not_silently_cut(client, monkeypatch):
    work = workspace(client)
    configure(client, work, context_window=8192, tools_enabled=True, allowed_tools=['read_targets'])
    seen = tools_reply(monkeypatch, [{'content': '', 'tool_calls': [call('large', 'read_targets', {})]}])
    monkeypatch.setattr(ai_tools, 'execute', lambda *args: {'state': 'completed', 'text': '字' * 5000})
    send(client, work)
    value = finished(client, work, 'error')
    assert len(seen) == 1 and '内容未截断' in value['run']['error']
    assert value['run']['events'][0]['result']['text'] == '字' * 5000


def test_tool_growth_auto_compacts_older_history_and_preserves_live_exchange(client, monkeypatch):
    work = workspace(client)
    original = seed_history(work, count=5, width=100)
    configure(client, work, context_window=8192, compact_threshold=.75, tools_enabled=True, allowed_tools=['read_targets'])
    seen = tools_reply(monkeypatch, [{'content': '', 'tool_calls': [call('growth', 'read_targets', {})]},
                                      {'content': '完整最终正文', 'tool_calls': []}])
    monkeypatch.setattr(ai_tools, 'execute', lambda *args: {'state': 'completed', 'text': '资料' * 450})
    summaries = []
    async def compress(*args): summaries.append(args[-1]); return summary()
    monkeypatch.setattr(ai, 'request_completion', compress)
    send(client, work)
    value = finished(client, work)
    assert len(seen) == 2 and summaries
    assert not any('较早交流摘要' in m['content'] for m in seen[0] if m.get('content'))
    assert any('较早交流摘要' in m['content'] for m in seen[1] if m.get('content'))
    assert seen[1][-2]['tool_calls'][0]['id'] == 'growth'
    assert seen[1][-1]['role'] == 'tool' and seen[1][-1]['tool_call_id'] == 'growth'
    assert json.loads(seen[1][-1]['content'])['text'] == '资料' * 450
    assert value['messages'][:len(original)] == original and value['context']['checkpoint']['through_count'] == 6
    assert [event['kind'] for event in value['run']['events']] == ['tool', 'compaction']


def test_later_document_part_can_trigger_automatic_compaction(client, monkeypatch):
    work = workspace(client)
    seed_history(work, count=5, width=150)
    configure(client, work, context_window=16384, compact_threshold=.5)
    monkeypatch.setattr(ai.integrations, 'chunks', lambda *args: iter(['短片段', '后续完整片段' * 330]))
    seen, summaries = [], []
    async def complete(*args):
        messages = args[-1]
        if messages[0]['content'].startswith('把对话资料整理为紧凑摘要'):
            summaries.append(messages); return summary()
        seen.append(messages); return '正文结果'
    monkeypatch.setattr(ai, 'request_completion', complete)
    send(client, work)
    value = finished(client, work)
    assert len(seen) == 2 and summaries
    assert not any('较早交流摘要' in m['content'] for m in seen[0])
    assert any('较早交流摘要' in m['content'] for m in seen[1])
    assert value['run']['result'] == '正文结果\n\n正文结果'


@pytest.mark.parametrize('status,payload,expected', [
    (400, {}, '关闭工具'),
    (200, {'choices': [{'finish_reason': 'length', 'message': {'content': 'partial'}}]}, '长度限制'),
    (200, {'choices': []}, '无效'),
    (200, {'choices': [{'message': {'content': {'invalid': True}, 'tool_calls': [call('bad-content', 'read_targets', {})]}}]}, '有效工具'),
])
def test_real_tool_response_parser_rejects_incompatible_or_partial_reply(status, payload, expected):
    class Client:
        async def post(self, *args, **kwargs):
            assert kwargs['json']['max_tokens'] == 2048
            return httpx.Response(status, json=payload)
    with pytest.raises(ValueError, match=expected):
        asyncio.run(runtime.request_tools(Client(), 'https://example.test/v1', {}, 'offline', [], [], 2048))
