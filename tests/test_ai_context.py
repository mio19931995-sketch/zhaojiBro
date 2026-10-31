"""Offline, complete-turn compaction and append-only history regression tests."""
import asyncio
import copy
import json
import time

import pytest

from tests.test_ai_workspace import client, workspace, send, finished, reply
from backend import ai_workspace as ai, ai_context as context, store


def seed_history(work, count=6, width=450):
    work['messages'] = [{'id': f'm-{i}-{role}', 'run_id': f'old-{i}', 'role': role,
                         'text': f'{role}-{i}保留事实{name}' + ('资料' * width)}
                        for i in range(count) for role, name in [('user', '目标'), ('assistant', '建议')]]
    store.object_put('ai_workspace', work['id'], work)
    return copy.deepcopy(work['messages'])


def configure(client, work, **changes):
    cfg = {**context.AgentConfig().model_dump(), **changes}
    result = client.put(f"/api/ai/workspaces/{work['id']}/agent-config", json=cfg)
    assert result.status_code == 200, result.text
    return result.json()


def summary():
    return json.dumps({'goals': ['保留事实'], 'constraints': ['不得改数字'], 'decisions': ['逐项审核'], 'pending': ['继续处理']}, ensure_ascii=False)


def wait_compact(client, work, expected='done'):
    for _ in range(400):
        value = client.get(f"/api/ai/workspaces/{work['id']}").json()
        if value['compact_run']['status'] not in ai.ACTIVE:
            assert value['compact_run']['status'] == expected, value['compact_run']
            return value
        time.sleep(.01)
    pytest.fail('Compaction did not finish')


def test_auto_compaction_batches_complete_turns_and_keeps_all_log(client, monkeypatch):
    work = workspace(client)
    original = seed_history(work, width=300)
    configure(client, work, context_window=8192, compact_threshold=.5)
    seen, batches = [], []
    async def complete(_, base, headers, model, messages):
        assert ai.request_max_output.get() == 2048
        if messages[0]['content'] == context.SUMMARY_SYSTEM:
            material = json.loads(messages[1]['content'])
            batches.append(material)
            assert material['messages'][0]['role'] == 'user'
            assert material['messages'][-1]['role'] == 'assistant'
            return summary()
        seen.extend(messages)
        return '正文处理完成'
    monkeypatch.setattr(ai, 'request_completion', complete)
    send(client, work)
    result = finished(client, work)
    checkpoint = result['context']['checkpoint']
    assert checkpoint['through_count'] == 8 and checkpoint['batches'] > 1
    assert result['messages'][:len(original)] == original
    assert len(result['messages']) == len(original) + 2
    assert '较早交流摘要' in seen[1]['content']
    for item in original[-4:]:
        assert any(item['text'] == message['content'] for message in seen)
    assert not any(message['content'] == original[0]['text'] for message in seen)
    assert result['text'] == work['text'] and result['revision'] == work['revision']
    assert ai.request_max_output.get() == 8192


def test_manual_compact_preserves_chat_result_and_pending_proposals(client, monkeypatch):
    work = workspace(client)
    original = seed_history(work, width=120)
    reply(monkeypatch, '新的工作区内容')
    run = send(client, work)
    completed = finished(client, work)
    async def complete(*args): return summary()
    monkeypatch.setattr(ai, 'request_completion', complete)
    response = client.post(f"/api/ai/workspaces/{work['id']}/compact", json={'revision': 0})
    assert response.status_code == 200 and response.json()['mode'] == 'compact'
    result = wait_compact(client, work)
    assert result['run_id'] == run['id'] and result['run']['id'] == run['id']
    assert result['messages'] == completed['messages']
    assert result['revision'] == 0 and result['text'] == work['text']
    plan = client.get(f"/api/ai/workspaces/{work['id']}/results/{run['id']}/proposals").json()
    assert plan['can_apply'] and plan['proposals']


@pytest.mark.parametrize('bad', ['not json', '{"goals":[]}', '{"goals":[false],"constraints":[],"decisions":[],"pending":[]}'])
def test_failed_summary_never_replaces_log_or_checkpoint(client, monkeypatch, bad):
    work = workspace(client)
    original = seed_history(work)
    reply(monkeypatch, bad)
    assert client.post(f"/api/ai/workspaces/{work['id']}/compact", json={'revision': 0}).status_code == 200
    value = wait_compact(client, work, 'error')
    assert value['messages'] == original and value['context']['checkpoint'] is None
    assert value['text'] == work['text']


def test_later_batch_failure_does_not_commit_partial_summary(client, monkeypatch):
    work = workspace(client)
    original = seed_history(work, width=400)
    configure(client, work, context_window=8192)
    batches = []
    async def complete(*args):
        batches.append(args[-1])
        return summary() if len(batches) == 1 else '格式错误'
    monkeypatch.setattr(ai, 'request_completion', complete)
    client.post(f"/api/ai/workspaces/{work['id']}/compact", json={'revision': 0})
    value = wait_compact(client, work, 'error')
    assert len(batches) == 2 and value['context']['checkpoint'] is None
    assert value['messages'] == original


def test_cancel_and_restart_leave_context_untouched(client, monkeypatch):
    work = workspace(client)
    original = seed_history(work)
    async def wait(*args): await asyncio.sleep(10); return summary()
    monkeypatch.setattr(ai, 'request_completion', wait)
    client.post(f"/api/ai/workspaces/{work['id']}/compact", json={'revision': 0})
    time.sleep(.05)
    assert client.post(f"/api/ai/workspaces/{work['id']}/cancel").json()['compact_run']['status'] == 'cancelled'
    value = wait_compact(client, work, 'cancelled')
    assert value['messages'] == original and value['context']['checkpoint'] is None
    interrupted = value['compact_run']
    interrupted['status'] = 'running'
    store.object_put('ai_run', interrupted['id'], interrupted)
    ai.recover()
    value = wait_compact(client, work, 'interrupted')
    assert value['messages'] == original and value['context']['checkpoint'] is None


def test_context_compare_and_swap_does_not_override_other_checkpoint(client, monkeypatch):
    work = workspace(client)
    seed_history(work, width=100)
    async def changed(*args):
        value = store.object_get('ai_workspace', work['id'])
        value['context_checkpoint'] = {'concurrent': 'preserve me'}
        store.object_put('ai_workspace', work['id'], value)
        return summary()
    monkeypatch.setattr(ai, 'request_completion', changed)
    client.post(f"/api/ai/workspaces/{work['id']}/compact", json={'revision': 0})
    value = wait_compact(client, work, 'error')
    assert '已变化' in value['compact_run']['error']
    assert store.object_get('ai_workspace', work['id'])['context_checkpoint'] == {'concurrent': 'preserve me'}


def test_uncompressible_complete_turn_and_disabled_auto_never_truncate(client, monkeypatch):
    work = workspace(client)
    original = seed_history(work, width=3000)
    configure(client, work, context_window=8192, auto_compact=False)
    calls = []
    async def forbidden(*args): calls.append(args); return summary()
    monkeypatch.setattr(ai, 'request_completion', forbidden)
    send(client, work)
    result = finished(client, work, 'error')
    assert not calls and result['messages'][:len(original)] == original
    client.post(f"/api/ai/workspaces/{work['id']}/compact", json={'revision': 0})
    result = wait_compact(client, work, 'error')
    assert '完整交流' in result['compact_run']['error'] and not calls


def test_invalid_checkpoint_replays_full_history_and_tool_records_as_evidence():
    messages = [{'role': 'user', 'text': '查找资料'}, {'role': 'tool', 'text': '{"state":"needs_input"}'},
                {'role': 'status', 'text': '用户确认：已另存文稿'}, {'role': 'assistant', 'text': '完成'}]
    projected = context.projection(messages, {'through_count': 99})
    assert len(projected) == 4 and all(m['role'] in ('user', 'assistant') for m in projected)
    assert '真实工具状态' in projected[2]['content'] and '已另存' in projected[2]['content']


@pytest.mark.parametrize('change', [{'context_window': 8191}, {'context_window': True}, {'allowed_tools': ['shell']}, {'allowed_tools': ['read_targets', 'read_targets']}, {'surprise': True}])
def test_invalid_agent_config_is_rejected(client, change):
    work = workspace(client)
    response = client.put(f"/api/ai/workspaces/{work['id']}/agent-config", json={**context.AgentConfig().model_dump(), **change})
    assert response.status_code in (400, 422)
