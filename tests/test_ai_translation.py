"""Brand protection correctness, actual workflow guards and persistence, offline."""
import asyncio
import json

import pytest

from tests.test_ai_workspace import client, workspace, source, finished
from tests.test_ai_context import configure
from backend import ai_translation as tr, ai_workspace as ai, ai_runtime, store, engine, integrations


def start(client, work, **fields):
    response = client.post(f"/api/ai/workspaces/{work['id']}/runs", json={
        'revision': 0, 'instruction': '翻译为英文，保留品牌原文。', 'skill': 'lulu-translate', **fields})
    assert response.status_code == 200, response.text
    return response.json()


def mock_translation(monkeypatch, brands, outputs=None):
    seen = []
    async def respond(*args):
        messages = args[-1]
        seen.append(messages)
        if messages[0]['content'] == tr.DETECT_SYSTEM:
            text = json.loads(messages[-1]['content'])['text']
            return json.dumps({'brands': [name for name in brands if name in text]}, ensure_ascii=False)
        tokens = tr.TOKEN.findall(messages[-1]['content'])
        return outputs.pop(0)(tokens) if outputs else 'English: ' + ' / '.join(tokens)
    monkeypatch.setattr(ai, 'request_completion', respond)
    return seen


def test_longest_names_repeats_quotes_and_original_case_are_lossless():
    text = '松果X1、松果、松果X1、米家 Pro、A+"B"。'
    guard = tr.BrandLock(text, ['松果', '松果X1', '米家 Pro', 'A+"B"'])
    masked = guard.mask(text)
    assert guard.mask(masked) == masked and len(tr.TOKEN.findall(masked)) == 5
    assert guard.restore(masked, text) == text
    assert tr.BrandLock('Professional', ['Pro']).terms == []
    assert '松果' not in masked and '米家' not in masked


@pytest.mark.parametrize('operation', [
    lambda text: text.replace(tr.TOKEN.search(text)[0], '', 1),
    lambda text: text + tr.TOKEN.search(text)[0],
    lambda text: text.replace('_000_END', '_999_END'),
    lambda text: text.replace('BRANDLOCK_', 'BRANDLOCK_ '),
    lambda text: 'Pinecone X1',
])
def test_missing_added_corrupt_or_translated_markers_are_rejected(operation):
    guard = tr.BrandLock('松果X1', ['松果X1'])
    with pytest.raises(ValueError):
        guard.restore(operation(guard.mask('松果X1')), '松果X1')


def test_detected_names_must_be_exact_source_substrings():
    for raw in ('{}', '[]', '{"brands":["Pinecone X1"]}', '{"brands":[3]}', '{"brands":"松果X1"}', '{"brands":["松果X1"],"brands":[]}'):
        with pytest.raises(ValueError):
            tr.parse_inventory(raw, '松果X1重120克')
    assert tr.parse_inventory('```json\n{"brands":["松果X1"]}\n```', '松果X1重120克') == ['松果X1']


def test_brand_is_not_split_at_translation_or_inventory_boundary():
    text = '字' * 4998 + '松果X1' + '尾' * 40
    pieces = list(tr.parts(text, ['松果X1']))
    assert ''.join(pieces) == text and pieces[1].startswith('松果X1')
    assert any('松果X1' in window for window in tr.inventory_windows(text))


def test_ascii_brand_is_not_a_substring_replacement_inside_other_words():
    text = 'Pro and Professional, A and Apple, iPhone and iPhone16.'
    guard = tr.BrandLock(text, ['Pro', 'A', 'iPhone'])
    masked = guard.mask(text)
    assert len(tr.TOKEN.findall(masked)) == 3
    assert 'Professional' in masked and 'Apple' in masked and 'iPhone16' in masked
    assert guard.restore(masked, text) == text


def test_translation_restores_names_before_preview_save_and_startup(client, monkeypatch):
    original = source('臺積電和松果X1合作。松果X1保持售价。')
    work = workspace(client, original)
    seen = mock_translation(monkeypatch, ['臺積電', '松果X1'])
    job = start(client, work)
    value = finished(client, work)
    result = value['run']['result']
    assert '臺積電' in result and result.count('松果X1') == 2 and 'BRANDLOCK_' not in result
    assert '臺積電' not in json.dumps(seen[-1], ensure_ascii=False)
    assert value['run']['translation_brands'] == ['臺積電', '松果X1']
    assert store.get(original['id'])['transcript'] == original['transcript']
    response = client.post(f"/api/ai/workspaces/{work['id']}/results/{job['id']}/save-output")
    assert response.status_code == 200
    saved_id = response.json()['item_id']
    assert store.get(saved_id)['metadata']['protected_brands'] == ['臺積電', '松果X1']
    engine.simplify_saved_content()
    assert store.get(saved_id)['transcript'] == result


def test_changed_marker_retries_once_then_publishes_only_valid_result(client, monkeypatch):
    work = workspace(client, text='松果X1的介绍')
    seen = mock_translation(monkeypatch, ['松果X1'], [lambda tokens: 'Pinecone X1', lambda tokens: tokens[0] + ' introduction'])
    start(client, work)
    value = finished(client, work)
    assert value['run']['result'] == '松果X1 introduction' and len(seen) == 3
    assert tr.RETRY_SYSTEM in seen[-1][0]['content']
    assert sum(m['role'] == 'assistant' for m in value['messages']) == 1


def test_two_bad_outputs_publish_no_result_or_proposals(client, monkeypatch):
    work = workspace(client, text='松果X1的介绍')
    seen = mock_translation(monkeypatch, ['松果X1'], [lambda _: 'Songguo X1'] * 2)
    job = start(client, work)
    value = finished(client, work, 'error')
    assert len(seen) == 3 and not value['run']['result'] and not value['run'].get('proposals')
    assert not any(m['role'] == 'assistant' for m in value['messages'])
    assert client.post(f"/api/ai/workspaces/{work['id']}/results/{job['id']}/save-output").status_code == 400


def test_fixed_brand_preference_survives_empty_inventory_and_is_version_guarded(client, monkeypatch):
    assert client.patch('/api/ai/skills/lulu-translate', json={'config': {'protected_terms': ['罕见牌']}}).status_code == 200
    work = workspace(client, text='罕见牌与普通苹果')
    mock_translation(monkeypatch, [])
    start(client, work, skill='', preset='translate')
    result = finished(client, work)['run']
    assert result['result'] == 'English: 罕见牌' and result['translation_brands'] == ['罕见牌']
    assert 'lulu-translate' in result['loaded_skills']
    for value in ('name', [''], ['x' * 81], ['a'] * 65, [3]):
        assert client.patch('/api/ai/skills/lulu-translate', json={'config': {'protected_terms': value}}).status_code == 400


def test_selection_translation_protects_only_selected_brand(client, monkeypatch):
    text = '开头未选品牌。松果X1与米家 Pro。结尾未选。'
    work = workspace(client, text=text)
    selected = '松果X1与米家 Pro。'
    offset = len(text[:text.index(selected)].encode())
    mock_translation(monkeypatch, ['松果X1', '米家 Pro'])
    start(client, work, selection={'start': offset, 'end': offset + len(selected.encode()), 'text': selected, 'offset_unit': 'utf8', 'revision': 0})
    value = finished(client, work)
    assert value['text'] == text and value['run']['scope'] == 'selection'
    assert value['run']['result'] == 'English: 松果X1 / 米家 Pro'


def test_cancel_during_inventory_leaves_source_and_result_intact(client, monkeypatch):
    work = workspace(client, text='松果X1')
    async def delayed(*args):
        await asyncio.sleep(10)
        return '{"brands":["松果X1"]}'
    monkeypatch.setattr(ai, 'request_completion', delayed)
    start(client, work)
    assert client.post(f"/api/ai/workspaces/{work['id']}/cancel").status_code == 200
    value = finished(client, work, 'cancelled')
    assert not value['run']['result'] and value['text'] == work['text']


def test_tool_translation_restores_pending_body_and_confirmed_document(client, monkeypatch):
    work = workspace(client, text='松果X1')
    configure(client, work, tools_enabled=True, allowed_tools=['create_document'])
    mock_translation(monkeypatch, ['松果X1'])
    async def tool(*args):
        code = tr.TOKEN.search(args[4][-1]['content'])[0]
        return {'content': '', 'tool_calls': [{'id': 'translated', 'type': 'function', 'function': {
            'name': 'create_document', 'arguments': json.dumps({'title': code, 'markdown': code + ' introduction'})}}]}
    monkeypatch.setattr(ai_runtime, 'request_tools', tool)
    job = start(client, work)
    value = finished(client, work)
    action = value['run']['actions'][0]
    assert action['preview'] == {'title': '松果X1', 'text': '松果X1 introduction'}
    response = client.post(f"/api/ai/workspaces/{work['id']}/runs/{job['id']}/actions/{action['id']}/confirm", json={'revision': 0})
    assert response.status_code == 200 and store.get(response.json()['document_id'])['transcript'] == '松果X1 introduction'


def test_legacy_translation_uses_same_inventory_and_loss_guard(client, monkeypatch):
    seen = []
    def complete(_, base, headers, body):
        seen.append(body)
        if body['messages'][0]['content'] == tr.DETECT_SYSTEM:
            return '{"brands":["松果X1"]}'
        return tr.TOKEN.search(body['messages'][1]['content'])[0] + ' weighs 120 grams.'
    monkeypatch.setattr(integrations, 'llm_post', complete)
    result = integrations.process_text('松果X1重120克', 'translate')
    assert result == '松果X1 weighs 120 grams.' and result.brands == ['松果X1'] and len(seen) == 2


def test_tool_result_is_remasked_without_altering_json_keys(client, monkeypatch):
    work = workspace(client, text='松果X1')
    configure(client, work, tools_enabled=True, allowed_tools=['read_targets'])
    mock_translation(monkeypatch, ['松果X1'])
    calls = []
    async def tool(*args):
        messages = args[4]
        calls.append(messages)
        if len(calls) == 1:
            return {'content': '', 'tool_calls': [{'id': 'read', 'type': 'function', 'function': {'name': 'read_targets', 'arguments': '{}'}}]}
        assert messages[-1]['role'] == 'tool'
        data = json.loads(messages[-1]['content'])
        assert 'document_id' in data and '松果X1' not in data['text']
        return {'content': data['text'] + ' is the product.', 'tool_calls': []}
    monkeypatch.setattr(ai_runtime, 'request_tools', tool)
    start(client, work)
    assert finished(client, work)['run']['result'] == '松果X1 is the product.'


def test_bad_translation_tool_write_never_creates_an_action(client, monkeypatch):
    work = workspace(client, text='松果X1')
    configure(client, work, tools_enabled=True, allowed_tools=['create_document'])
    mock_translation(monkeypatch, ['松果X1'])
    attempts = []
    async def tool(*args):
        attempts.append(True)
        return {'content': '', 'tool_calls': [{'id': 'invalid', 'type': 'function', 'function': {
            'name': 'create_document', 'arguments': json.dumps({'title': 'translated', 'markdown': 'Pinecone X1'})}}]}
    monkeypatch.setattr(ai_runtime, 'request_tools', tool)
    start(client, work)
    result = finished(client, work, 'error')['run']
    assert len(attempts) == 2 and not result['actions'] and not result['events'] and not store.items()


def test_apply_and_draft_save_keep_brand_spelling_across_startup(client, monkeypatch):
    item = source('臺積電的介紹。')
    work = workspace(client, item)
    mock_translation(monkeypatch, ['臺積電'])
    job = start(client, work)
    result = finished(client, work)['run']['result']
    assert client.post(f"/api/ai/workspaces/{work['id']}/results/{job['id']}/apply", json={'revision': 0}).status_code == 200
    saved = client.put(f"/api/ai/workspaces/{work['id']}/draft", json={'revision': 1, 'title': item['title'], 'text': result + ' 新產品'})
    assert saved.status_code == 200 and '臺積電' in saved.json()['text'] and '新产品' in saved.json()['text']
    assert client.post(f"/api/ai/workspaces/{work['id']}/save-document", json={'revision': 2}).status_code == 200
    engine.simplify_saved_content()
    assert '臺積電' in store.get(item['id'])['transcript']
