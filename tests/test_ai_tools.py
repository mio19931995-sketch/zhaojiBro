"""Closed tool scope and transactionally confirmed writes, without model calls."""
import time
import uuid

import pytest

from test_ai_workspace import client, source, workspace
from backend import ai_tools as tools, ai_skills as skills, ai_workspace as ai, store


def run_for(client, item=None, text='当前正文'):
    work = workspace(client, item, text)
    config = {'tools_enabled': True, 'allowed_tools': list(tools.BY_NAME)}
    work['agent_config'] = config
    identity = uuid.uuid4().hex
    run = {'id': identity, 'workspace_id': work['id'], 'status': 'done', 'title': work['title'], 'input_text': work['text'], 'base_text': work['text'],
           'draft_revision': work['revision'], 'source_id': work['source_id'], 'source_hash': work['source_hash'], 'save_generation': 0,
           'source_revision': item['updated_at'] if item else None, 'agent_config': config, 'references': [], 'actions': [], 'events': [],
           'skill_entries': skills.freeze()['entries'], 'loaded_skills': [], 'explicit_skill': '', 'created_at': time.time(), 'result': '', 'phase': '完成', 'scope': 'document'}
    work['run_id'] = identity
    store.object_put('ai_workspace', work['id'], work)
    store.object_put('ai_run', identity, run)
    return work, run


def call(identity, **arguments):
    return {'id': uuid.uuid4().hex, 'name': identity, 'arguments': arguments}


def action_url(work, run, action, verb='confirm'):
    return f"/api/ai/workspaces/{work['id']}/runs/{run['id']}/actions/{action['id']}/{verb}"


def test_read_current_scope_and_reference_cannot_escape_selected_text(client):
    original = source('不可见前文\n可见选区😀\n不可见后文')
    work, run = run_for(client, original)
    run.update(input_text='可见选区😀', scope='selection')
    other = source('参考未选内容\n参考片段')
    run['references'] = [{'id': 'ref', 'title': other['title'], 'source_id': other['id'], 'text': '参考片段'}]
    target = tools.execute(call('read_targets'), run)
    assert target['text'] == '可见选区😀'
    assert target['references'][0]['text'] == '参考片段'
    assert tools.execute(call('read_document', document_id=original['id'], offset=0), run)['text'] == '可见选区😀'
    assert tools.execute(call('read_document', document_id=other['id'], offset=0), run)['text'] == '参考片段'
    unknown = source('未搜索文稿')
    with pytest.raises(ValueError, match='先搜索'):
        tools.execute(call('read_document', document_id=unknown['id'], offset=0), run)


def test_search_then_read_paginates_without_omission_and_rejects_changed_source(client):
    work, run = run_for(client)
    text = '分页资料😀' * 6000
    item = source(text)
    hidden = source('分页资料被删除')
    store.update(hidden['id'], deleted=1)
    rows = tools.execute(call('search_documents', query='分页资料'), run)['documents']
    assert [row['id'] for row in rows] == [item['id']]
    chunks, offset = [], 0
    while offset is not None:
        part = tools.execute(call('read_document', document_id=item['id'], offset=offset), run)
        chunks.append(part['text'])
        offset = part['next_offset']
    assert ''.join(chunks) == text and len(chunks) > 1
    store.update(item['id'], transcript='更新正文')
    with pytest.raises(ValueError, match='变化'):
        tools.execute(call('read_document', document_id=item['id'], offset=0), run)


def test_create_document_requires_confirmation_and_is_idempotent(client):
    original = source('原文保持')
    work, run = run_for(client, original)
    result = tools.execute(call('create_document', title='模型准备的新稿', markdown='這是新稿'), run)
    assert result['state'] == 'needs_input'
    assert store.get(original['id'])['transcript'] == '原文保持'
    with store.connection() as con:
        assert con.execute('SELECT count(*) FROM items').fetchone()[0] == 1
    store.object_put('ai_run', run['id'], run)
    action = run['actions'][0]
    response = client.post(action_url(work, run, action), json={'revision': 0})
    assert response.status_code == 200, response.text
    saved = response.json()['document_id']
    assert store.get(saved)['transcript'] == '这是新稿'
    duplicate = client.post(action_url(work, run, action), json={'revision': 0})
    assert duplicate.status_code == 200 and duplicate.json()['document_id'] == saved
    assert duplicate.json()['idempotent'] is True
    assert store.get(original['id'])['transcript'] == '原文保持'
    assert client.get(f"/api/ai/workspaces/{work['id']}").json()['messages'][-1]['role'] == 'status'


@pytest.mark.parametrize('change', ['draft', 'title', 'source', 'cancel', 'new_run', 'tools', 'skill'])
def test_old_write_action_cannot_be_confirmed_after_relevant_change(client, change):
    original = source('不可覆盖正文')
    work, run = run_for(client, original)
    tools.execute(call('load_skill', name='lulu-summary'), run)
    tools.execute(call('create_document', title='新稿', markdown='新的结果'), run)
    if change == 'draft': work.update(revision=1, text='手动编辑')
    if change == 'title': work['title'] = '新标题'
    if change == 'source': store.update(original['id'], transcript='来源已变化')
    if change == 'cancel': run['status'] = 'cancelled'
    if change == 'new_run': work['run_id'] = 'newer'
    if change == 'tools': work['agent_config'] = {'tools_enabled': False, 'allowed_tools': []}
    if change == 'skill': client.patch('/api/ai/skills/lulu-summary', json={'enabled': False})
    store.object_put('ai_workspace', work['id'], work)
    store.object_put('ai_run', run['id'], run)
    response = client.post(action_url(work, run, run['actions'][0]), json={'revision': work['revision']})
    assert response.status_code in {400, 409}, response.text
    with store.connection() as con:
        assert con.execute('SELECT count(*) FROM items').fetchone()[0] == 1


def test_dismissed_action_does_not_write_and_cannot_be_confirmed(client):
    work, run = run_for(client)
    tools.execute(call('create_document', title='不应创建', markdown='拒绝正文'), run)
    store.object_put('ai_run', run['id'], run)
    action = run['actions'][0]
    assert client.post(action_url(work, run, action, 'dismiss'), json={'revision': 0}).status_code == 200
    assert client.post(action_url(work, run, action), json={'revision': 0}).status_code == 409
    with store.connection() as con:
        assert con.execute('SELECT count(*) FROM items').fetchone()[0] == 0


def test_skill_draft_and_install_are_separate_confirmed_actions(client):
    work, run = run_for(client)
    tools.execute(call('create_skill_draft', name='made-by-user', description='用户的方法', instructions='先保留数字，再检查正文'), run)
    assert skills.drafts()['entries'] == []
    store.object_put('ai_run', run['id'], run)
    response = client.post(action_url(work, run, run['actions'][0]), json={'revision': 0})
    assert response.status_code == 200, response.text
    token = response.json()['token']
    assert not any(row['name'] == 'made-by-user' for row in skills.listing()['entries'])
    with pytest.raises(ValueError, match='先查看'):
        tools.execute(call('install_skill', name='', token=token), run)
    tools.execute(call('list_skill_drafts'), run)
    tools.execute(call('install_skill', name='', token=token), run)
    # Persist the fresh run from the first confirmation, retaining new actions.
    saved = store.object_get('ai_run', run['id'])
    saved['actions'].append(run['actions'][-1])
    saved['skill_preview_tokens'] = run['skill_preview_tokens']
    store.object_put('ai_run', run['id'], saved)
    response = client.post(action_url(work, saved, saved['actions'][-1]), json={'revision': 0})
    assert response.status_code == 200, response.text
    assert skills.get_skill('made-by-user')['installed']


def test_permissions_unknown_args_bool_integer_and_urls_are_checked(client):
    work, run = run_for(client)
    for value in (call('shell', command='dir'), call([], command='dir'), call('read_targets', path='secret'), call('prepare_collection', url='https://www.douyin.com/user/demo', limit=True)):
        with pytest.raises(ValueError): tools.execute(value, run)
    run['agent_config']['allowed_tools'].remove('search_documents')
    with pytest.raises(ValueError, match='未启用'): tools.execute(call('search_documents', query=''), run)
    for url in ('file:///secret', 'http://127.0.0.1/', 'https://user:password@example.com', 'https://localhost/', 'https://example.com:123/'):
        with pytest.raises(ValueError): tools.execute(call('inspect_link', url=url), run)
    assert tools.execute(call('inspect_link', url='https://www.douyin.com/user/demo'), run)['kind'] == 'homepage'
    assert tools.execute(call('prepare_collection', url='https://www.douyin.com/user/demo', limit=2), run)['state'] == 'needs_input'
    assert tools.execute(call('prepare_voice'), run)['target']['text'] == work['text']
    assert tools.execute(call('prepare_transcription', url=''), run)['state'] == 'needs_input'
    other, _ = run_for(client)
    assert len(tools.execute(call('list_tasks'), run)['tasks']) == 1


def test_install_action_cannot_reverse_a_later_user_disable(client):
    work, run = run_for(client)
    tools.execute(call('install_skill', name='lulu-summary', token=''), run)
    store.object_put('ai_run', run['id'], run)
    assert client.patch('/api/ai/skills/lulu-summary', json={'enabled': False}).status_code == 200
    response = client.post(action_url(work, run, run['actions'][0]), json={'revision': 0})
    assert response.status_code == 409, response.text
    assert not skills.get_skill('lulu-summary')['enabled']


def test_model_authored_documents_and_skill_drafts_freeze_simplified_preview_and_payload(client):
    work, run = run_for(client)
    tools.execute(call('create_document', title='這是標題', markdown='這是正文，保留數字 123。'), run)
    tools.execute(call('create_skill_draft', name='simplified-method', description='處理這份文稿的規則', instructions='請保留數字，檢查標點。'), run)
    document, draft = run['actions']
    assert document['arguments'] == {'title': '这是标题', 'markdown': '这是正文，保留数字 123。'}
    assert document['preview'] == {'title': '这是标题', 'text': '这是正文，保留数字 123。'}
    assert draft['arguments']['description'] == draft['preview']['description'] == '处理这份文稿的规则'
    assert draft['arguments']['instructions'] == '请保留数字，检查标点。'
    assert '请保留数字，检查标点。' in draft['preview']['text']
    store.object_put('ai_run', run['id'], run)
    document_response = client.post(action_url(work, run, document), json={'revision': 0})
    assert document_response.status_code == 200, document_response.text
    created = store.get(document_response.json()['document_id'])
    assert created['title'] == document['preview']['title']
    assert created['transcript'] == document['preview']['text']
    draft_response = client.post(action_url(work, run, draft), json={'revision': 0})
    assert draft_response.status_code == 200, draft_response.text
    token = draft_response.json()['token']
    package = store.object_get('ai_skill_preview', token)['package']
    assert package['content'] == draft['preview']['text']
    assert package['description'] == draft['preview']['description']
    installed = client.post('/api/ai/skills/install', json={'token': token})
    assert installed.status_code == 200, installed.text
    assert installed.json()['content'] == draft['preview']['text']
