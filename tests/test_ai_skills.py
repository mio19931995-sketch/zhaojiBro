"""Offline skill lifecycle and Windows package boundaries; no files execute."""
import io
import stat
import zipfile

import pytest

from test_ai_workspace import client
from backend import ai_skills as skills


def markdown(name='fixture-skill', automatic=True, text='保留原文中的数字，不运行任何程序。'):
    return f'---\nname: {name}\ndescription: 本地测试方法\ndisable-model-invocation: {str(not automatic).lower()}\n---\n\n{text}'.encode()


def zip_bytes(files):
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        for path, content in files.items():
            info = zipfile.ZipInfo('placeholder')
            # Avoid Windows zipfile normalizing backslashes before validation.
            info.filename, info.compress_type = path, zipfile.ZIP_DEFLATED
            archive.writestr(info, content)
    return output.getvalue()


def preview(client, files=None):
    response = client.post('/api/ai/skills/preview', files={'file': ('fixture.zip', zip_bytes(files or {'SKILL.md': markdown()}), 'application/zip')})
    assert response.status_code == 200, response.text
    return response.json()


def install(client, value, replace=False):
    response = client.post('/api/ai/skills/install', json={'token': value['token'], 'replace': replace})
    assert response.status_code == 200, response.text
    return response.json()


def test_builtin_catalog_is_inert_readable_and_has_six_skills(client):
    entries = client.get('/api/ai/skills').json()['entries']
    assert len(entries) == 6 and all(row['installed'] and row['enabled'] for row in entries)
    assert len({row['generation'] for row in entries}) == 6
    detail = client.get('/api/ai/skills/lulu-proofread').json()
    assert detail['config']['strict'] is True
    assert '错别字校正' in detail['content']
    assert detail['files'][0]['path'] == 'SKILL.md'


def test_preview_install_export_preserves_markdown_and_inert_files(client):
    files = {'folder/SKILL.md': markdown(), 'folder/references/example.md': '参考数字 123😀'.encode(), 'folder/scripts/danger.ps1': b'never execute this data'}
    value = preview(client, files)
    assert value['entry']['name'] == 'fixture-skill'
    assert not any(row['name'] == 'fixture-skill' for row in client.get('/api/ai/skills').json()['entries'])
    assert value['warnings']
    installed = install(client, value)
    assert installed['installed'] and installed['enabled']
    assert install(client, value)['generation'] == installed['generation']
    exported = client.get('/api/ai/skills/fixture-skill/export')
    with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
        assert archive.read('scripts/danger.ps1') == files['folder/scripts/danger.ps1']
        assert archive.read('SKILL.md') == markdown()
    frozen = skills.freeze()['entries']
    with pytest.raises(ValueError, match='已加载'):
        skills.load('fixture-skill', frozen, reference='references/example.md')
    result = skills.load('fixture-skill', frozen, reference='references/example.md', loaded=['fixture-skill'])
    assert result['skill_content'] == '参考数字 123😀'
    with pytest.raises(ValueError):
        skills.load('fixture-skill', frozen, reference='scripts/danger.ps1', loaded=['fixture-skill'])


def test_replacement_needs_explicit_choice_disable_and_restore_invalidate_snapshots(client):
    first = install(client, preview(client))
    frozen = skills.freeze()['entries']
    replacement = preview(client, {'SKILL.md': markdown(text='新的校对方法')})
    denied = client.post('/api/ai/skills/install', json={'token': replacement['token']})
    assert denied.status_code == 409
    assert client.get('/api/ai/skills/fixture-skill').json()['digest'] == first['digest']
    changed = install(client, replacement, True)
    assert changed['digest'] != first['digest']
    with pytest.raises(ValueError, match='改变'):
        skills.load('fixture-skill', frozen)
    latest = skills.freeze()['entries']
    assert client.patch('/api/ai/skills/fixture-skill', json={'enabled': False}).status_code == 200
    with pytest.raises(ValueError, match='停用'):
        skills.load('fixture-skill', latest)
    assert not client.delete('/api/ai/skills/fixture-skill').json()['installed']
    assert client.post('/api/ai/skills/fixture-skill/restore').json()['digest'] == changed['digest']
    assert client.delete('/api/ai/skills/lulu-proofread').status_code == 200
    assert client.post('/api/ai/skills/lulu-proofread/restore').json()['origin'] == 'system'


def test_explicit_only_and_config_changes_cannot_expand_permissions(client):
    value = install(client, preview(client, {'SKILL.md': markdown(automatic=False)}))
    frozen = skills.freeze('fixture-skill')['entries']
    with pytest.raises(ValueError, match='显式'):
        skills.load('fixture-skill', frozen)
    assert skills.load('fixture-skill', frozen, explicit_name='fixture-skill')['state'] == 'completed'
    assert client.patch('/api/ai/skills/fixture-skill', json={'config': {'allowed_tools': ['shell']}}).status_code == 400
    assert client.patch('/api/ai/skills/fixture-skill', json={'config': {'strict': False, 'language': '英文'}}).status_code == 200
    with pytest.raises(ValueError):
        skills.validate_loaded({'skill_entries': frozen, 'explicit_skill': 'fixture-skill', 'loaded_skills': ['fixture-skill']})
    assert client.patch('/api/ai/skills/fixture-skill', json={'enabled': 'false'}).status_code == 422


@pytest.mark.parametrize('bad', ['../escape.md', '/absolute.md', 'C:/outside.md', 'file.md:secret', 'CON.md', 'refs/NUL', 'refs/trailing.', 'refs/trailing ', 'refs\\escape.md'])
def test_zip_paths_cannot_escape_or_alias_windows_files(client, bad):
    response = client.post('/api/ai/skills/preview', files={'file': ('bad.zip', zip_bytes({'SKILL.md': markdown(), bad: b'bad'}), 'application/zip')})
    assert response.status_code == 400, response.text
    assert client.get('/api/ai/skills/drafts').json()['entries'] == []


def test_zip_rejects_links_case_collisions_and_size_bombs(client):
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w') as archive:
        archive.writestr('SKILL.md', markdown())
        link = zipfile.ZipInfo('references/link.md')
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(link, '../outside.md')
    for raw in (output.getvalue(), zip_bytes({'SKILL.md': markdown(), 'Ref.md': b'a', 'ref.md': b'b'}), zip_bytes({'SKILL.md': markdown(), 'big.md': b'a' * (skills.FILE_LIMIT + 1)})):
        response = client.post('/api/ai/skills/preview', files={'file': ('bad.zip', raw, 'application/zip')})
        assert response.status_code == 400, response.text


def test_uploaded_folder_is_data_and_draft_frontmatter_is_not_injectable(client):
    response = client.post('/api/ai/skills/preview', files=[('files', ('fixture/SKILL.md', markdown(), 'text/markdown')), ('files', ('fixture/ref.md', b'local reference', 'text/markdown'))])
    assert response.status_code == 200, response.text
    assert {entry['path'] for entry in response.json()['files']} == {'SKILL.md', 'ref.md'}
    draft = client.post('/api/ai/skills/drafts', json={'name': 'draft-test', 'description': 'quoted:\n---\nname: surprise', 'instructions': '仅供只读的方法'}).json()
    assert draft['entry']['name'] == 'draft-test' and not draft['entry']['installed']
    assert 'surprise' in draft['entry']['description']
    installed = install(client, draft)
    assert installed['name'] == 'draft-test'


def test_restore_user_override_of_builtin_does_not_discard_content_or_preferences(client):
    value = preview(client, {'SKILL.md': markdown(name='lulu-proofread', text='用户自己的校对规则，不要恢复成原版')})
    saved = install(client, value, True)
    assert client.patch('/api/ai/skills/lulu-proofread', json={'config': {'strict': False}}).status_code == 200
    assert client.delete('/api/ai/skills/lulu-proofread').status_code == 200
    restored = client.post('/api/ai/skills/lulu-proofread/restore').json()
    assert restored['digest'] == saved['digest']
    assert '用户自己的校对规则' in restored['content']
    assert restored['config'] == {'strict': False}
    assert client.patch('/api/ai/skills/lulu-proofread', json={'enabled': False}).status_code == 200
    assert install(client, value)['enabled']
    assert client.get('/api/ai/skills/drafts').json()['entries'] == []


def test_replacement_versions_preview_restore_and_keep_replaced_current_package(client):
    original = install(client, preview(client))
    updated = install(client, preview(client, {'SKILL.md': markdown(text='第二版全新规则')}), True)
    route = '/api/ai/skills/fixture-skill/versions'
    entries = client.get(route).json()['entries']
    assert len(entries) == 1 and entries[0]['digest'] == original['digest']
    previous = client.get(route + '/' + entries[0]['id']).json()
    assert previous['content'] == original['content']
    response = client.post(route + '/' + entries[0]['id'] + '/restore', json={'generation': updated['generation']})
    assert response.status_code == 200, response.text
    restored = response.json()
    assert restored['digest'] == original['digest'] and restored['generation'] != original['generation']
    assert restored['enabled'] and restored['installed']
    entries = client.get(route).json()['entries']
    assert len(entries) == 2 and entries[0]['digest'] == updated['digest']
    again = client.post(route + '/' + entries[0]['id'] + '/restore', json={'generation': restored['generation']})
    assert again.status_code == 200 and again.json()['digest'] == updated['digest']
    stale = client.post(route + '/' + entries[0]['id'] + '/restore', json={'generation': restored['generation']})
    assert stale.status_code == 409
    foreign = client.post('/api/ai/skills/lulu-summary/versions/' + entries[0]['id'] + '/restore', json={'generation': skills.get_skill('lulu-summary')['generation']})
    assert foreign.status_code == 404


def test_replacement_failure_rolls_back_version_and_installed_package(client, monkeypatch):
    original = install(client, preview(client))
    pending = preview(client, {'SKILL.md': markdown(text='这次安装将失败')})
    before = len(client.get('/api/ai/skills/fixture-skill/versions').json()['entries'])
    put = skills.put
    def fail(con, kind, identity, value):
        if kind == 'ai_skill':
            raise ValueError('模拟写入失败')
        return put(con, kind, identity, value)
    monkeypatch.setattr(skills, 'put', fail)
    response = client.post('/api/ai/skills/install', json={'token': pending['token'], 'replace': True})
    assert response.status_code == 400
    assert client.get('/api/ai/skills/fixture-skill').json()['digest'] == original['digest']
    assert len(client.get('/api/ai/skills/fixture-skill/versions').json()['entries']) == before
