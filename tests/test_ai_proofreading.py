"""Proofreading gates, publication boundaries, frozen settings and tool batches."""
import asyncio
import json
import time

import pytest

from tests.test_ai_workspace import client, workspace, source, finished
from tests.test_ai_context import configure
from backend import ai_proofreading as proof, ai_workspace as ai, ai_runtime, engine, integrations, store


def start(client, work, **fields):
    return client.post(f"/api/ai/workspaces/{work['id']}/runs", json={
        'revision': work['revision'], 'instruction': '校正错字，保留数字和链接，只返回正文。',
        'preset': 'correct', **fields})


def respond(monkeypatch, text):
    seen = []
    async def complete(*args):
        seen.append(args[-1])
        return text
    monkeypatch.setattr(ai, 'request_completion', complete)
    return seen


@pytest.mark.parametrize('before,after', [
    ('售价199元。', '售价299元。'), ('199元，120克。', '120元，199克。'),
    ('199元，199元。', '199元。'), ('199元。', '199元，199元。'),
    ('2026-10-26，12:30，-1.25%。', '2026-10-26，12:30，1.25%。'),
    ('价格1,000元', '价格1000元'), ('https://example.test/a?x=1', 'https://example.test/a?x=2'),
    ('https://example.test/A', 'https://example.test/a'),
    ('https://example.test/a https://example.test/b', 'https://example.test/b https://example.test/a'),
])
def test_protected_values_order_and_multiplicity(before, after):
    with pytest.raises(proof.Rejected) as failure:
        proof.validate(before, after)
    assert failure.value.report['reason'] == 'protected_tokens'


def test_small_corrections_simplified_prose_and_url_spelling():
    before = '這是一份測式文稿，售價199元。詳情：https://example.test/資料?項=2。'
    after = engine.to_simplified(before).replace('测式', '测试')
    assert 'https://example.test/資料?項=2' in after and '这是一份测试' in after
    assert proof.validate(before, after, engine.to_simplified)['state'] == 'passed'


@pytest.mark.parametrize('before,after,reason', [
    ('原文内容' * 10, '原文内容', 'length'),
    ('原文内容', '原文内容' * 10, 'length'),
    ('甲乙丙丁戊己', '春夏秋冬风雨', 'similarity'),
])
def test_large_rewrites_are_not_proofreading(before, after, reason):
    with pytest.raises(proof.Rejected) as failure:
        proof.validate(before, after)
    assert failure.value.report['reason'] == reason


def test_length_thresholds_and_repetitive_work_are_bounded():
    assert proof.validate('abcdefghij' * 2, 'abcdefghijabc')['length_ratio'] == .65
    assert proof.validate('abcdefghij' * 2, 'abcdefghij' * 2 + 'abcdefg')['length_ratio'] == 1.35
    started = time.monotonic()
    assert proof.validate('相同' * 25000, '相同' * 25000)['similarity'] == 1
    assert proof.validate('这是一份测式文稿。' * 5000, '这是一份测试文稿。' * 5000)['similarity'] >= .65
    with pytest.raises(proof.Rejected) as failure:
        proof.validate('甲' * 5000 + '乙' * 5000, '乙' * 5000 + '甲' * 5000)
    assert failure.value.report['reason'] == 'similarity_budget' and time.monotonic() - started < 2


def test_chunk_boundaries_keep_numbers_links_and_original_newlines():
    original = '字' * 4998 + '199.25%\n' + '字' * 4990 + 'https://example.test/完整?x=123\n尾段'
    parts = list(proof.parts(original))
    assert ''.join(parts) == original and all('199.25%' in p or '199' not in p for p in parts)
    assert any('https://example.test/完整?x=123' in p for p in parts)
    assert proof.join_parts(parts, [p.strip() for p in parts]) == original


def test_rejected_reply_is_read_only_and_cannot_publish_or_save(client, monkeypatch):
    item = source('这份产品售价199元。')
    work = workspace(client, item)
    seen = respond(monkeypatch, '这份产品售价299元。')
    job = start(client, work).json()
    value = finished(client, work, 'error')
    run = value['run']
    assert run['proofreading_check']['reason'] == 'protected_tokens'
    assert run['rejected_result'] == '这份产品售价299元。' and not run['result']
    assert not run.get('proposals') and not run.get('actions')
    assert all(m['role'] != 'assistant' for m in value['messages'])
    assert value['text'] == item['transcript'] == store.get(item['id'])['transcript']
    assert len(seen) == 1 and proof.SYSTEM in seen[0][0]['content']
    endpoint = f"/api/ai/workspaces/{work['id']}/results/{job['id']}"
    assert client.post(endpoint + '/save-output').status_code == 400
    assert client.post(endpoint + '/apply', json={'revision': 0}).status_code == 400
    assert not client.get(endpoint + '/proposals').json().get('proposals')


def test_valid_correction_applies_and_keeps_history_and_urls(client, monkeypatch):
    item = source('这是一份测式文稿，售价199元。https://example.test/資料')
    work = workspace(client, item)
    corrected = item['transcript'].replace('测式', '测试')
    respond(monkeypatch, corrected)
    job = start(client, work).json()
    run = finished(client, work)['run']
    assert run['proofreading_check']['state'] == 'passed' and run['result'] == corrected
    assert client.post(f"/api/ai/workspaces/{work['id']}/results/{job['id']}/apply", json={'revision': 0}).status_code == 200
    engine.simplify_saved_content()
    assert store.get(item['id'])['transcript'] == corrected
    assert store.versions(item['id'])


def test_disabled_strict_pref_and_other_skills_do_not_force_gates(client, monkeypatch):
    assert client.patch('/api/ai/skills/lulu-proofread', json={'config': {'strict': False}}).status_code == 200
    work = workspace(client, text='售价199元。')
    respond(monkeypatch, '售价299元。')
    assert start(client, work).status_code == 200
    assert not finished(client, work)['run'].get('proofreading_check')
    assert client.patch('/api/ai/skills/lulu-proofread', json={'config': {'strict': True}}).status_code == 200
    work = workspace(client, text='售价199元。')
    assert start(client, work, preset='summary', instruction='概括严格校对的使用技巧。').status_code == 200
    assert not finished(client, work)['run'].get('proofreading_check')


def test_limit_rejects_before_request_but_short_selection_is_allowed(client, monkeypatch):
    work = workspace(client, text='长' * 50001 + '测式199元。')
    seen = respond(monkeypatch, '测试199元。')
    assert start(client, work).status_code == 400 and not seen
    selected = '测式199元。'
    offset = len(('长' * 50001).encode())
    assert start(client, work, selection={'start': offset, 'end': offset + len(selected.encode()),
        'text': selected, 'revision': 0, 'offset_unit': 'utf8'}).status_code == 200
    value = finished(client, work)
    assert value['run']['result'] == '测试199元。' and value['text'] == work['text']


def test_later_segment_failure_does_not_publish_earlier_valid_reply(client, monkeypatch):
    work = workspace(client, text='字' * 4900 + '测式' + '字' * 1000 + '\n价格199元。')
    async def complete(*args):
        part = json.loads(args[-1][-1]['content'].split('：\n', 1)[1])
        return part.replace('测式', '测试').replace('199', '299')
    monkeypatch.setattr(ai, 'request_completion', complete)
    start(client, work)
    value = finished(client, work, 'error')
    assert value['run']['proofreading_check']['part'] == 2
    assert not value['run']['result'] and not any(m['role'] == 'assistant' for m in value['messages'])


@pytest.mark.parametrize('load_in_batch', [False, True])
def test_tool_write_and_same_batch_skill_load_cannot_bypass(client, monkeypatch, load_in_batch):
    work = workspace(client, text='这份产品售价199元。')
    configure(client, work, tools_enabled=True, allowed_tools=['create_document', 'load_skill'])
    def call(identity, name, args):
        return {'id': identity, 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}
    async def tool(*args):
        calls = [call('bad', 'create_document', {'title': '校对结果', 'markdown': '这份产品售价299元。'})]
        if load_in_batch:
            calls.append(call('load', 'load_skill', {'name': 'lulu-proofread'}))
        return {'content': '', 'tool_calls': calls}
    monkeypatch.setattr(ai_runtime, 'request_tools', tool)
    start(client, work, preset='' if load_in_batch else 'correct')
    run = finished(client, work, 'error')['run']
    assert run['proofreading_check']['state'] == 'blocked'
    assert not run['actions'] and not run['events'] and not store.items()


def test_legacy_correct_endpoint_cannot_bypass_protected_values(client, monkeypatch):
    monkeypatch.setattr(integrations, 'llm_post', lambda *args: '价格299元。')
    with pytest.raises(proof.Rejected):
        integrations.process_text('价格199元。', 'correct')


def test_valid_tool_candidate_keeps_passed_report_and_requires_confirmation(client, monkeypatch):
    work = workspace(client, text='这是一份测式文稿，价格199元。')
    configure(client, work, tools_enabled=True, allowed_tools=['create_document'])
    corrected = work['text'].replace('测式', '测试')
    async def tool(*args):
        return {'content': '', 'tool_calls': [{'id': 'good', 'type': 'function', 'function': {
            'name': 'create_document', 'arguments': json.dumps({'title': '校对结果', 'markdown': corrected})}}]}
    monkeypatch.setattr(ai_runtime, 'request_tools', tool)
    start(client, work)
    run = finished(client, work)['run']
    assert run['proofreading_check']['state'] == 'passed' and not store.items()
    action = run['actions'][0]
    assert action['state'] == 'pending'
    response = client.post(f"/api/ai/workspaces/{work['id']}/runs/{run['id']}/actions/{action['id']}/confirm", json={'revision': 0})
    assert response.status_code == 200 and store.get(response.json()['document_id'])['transcript'] == corrected


def test_cancellation_and_changed_preferences_cannot_publish_late_reply(client, monkeypatch):
    async def complete(*args):
        await asyncio.sleep(.12)
        return '测试199元。'
    monkeypatch.setattr(ai, 'request_completion', complete)
    work = workspace(client, text='测式199元。')
    start(client, work)
    time.sleep(.04)
    client.patch('/api/ai/skills/lulu-proofread', json={'config': {'strict': False}})
    value = finished(client, work, 'error')
    assert not value['run']['result'] and value['text'] == work['text']
