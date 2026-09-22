"""Read-only material access and explicit handoff boundaries."""
import json
import os
import tempfile

os.environ['LULU_DATA_DIR'] = tempfile.mkdtemp(prefix='lulu-codex-tests-')

import pytest
from fastapi.testclient import TestClient
from backend import store, codex_bridge as bridge, assets
from backend.main import app

client = TestClient(app, headers={'X-Lulu-Client': 'desktop'})


@pytest.fixture(autouse=True)
def database(monkeypatch, tmp_path):
    monkeypatch.setattr(store, 'DATA', tmp_path)
    monkeypatch.setattr(store, 'DB', tmp_path / 'library.sqlite3')
    monkeypatch.setattr(bridge, 'data_dir', lambda: tmp_path)
    store.init()


def test_readers_do_not_pause_jobs_or_expose_secrets(tmp_path):
    path = tmp_path / '中文视频.mp4'
    path.write_bytes(b'fixture')
    pending = store.add('正在下载', status='processing')
    item = store.add('100% 测试', status='done', transcript='😀中文正文', media_path=str(path),
                     metadata={'direct_url': 'never-expose-token', 'cookies': 'never-expose-cookie'})
    store.save_settings({'llm_api_key_enc': 'never-expose-key'})
    listing = bridge.list_assets({'query': '%'})
    assert [i['id'] for i in listing['items']] == [item['id']]
    detail = bridge.asset({'item_id': item['id']})
    assert detail['has_video_file'] and detail['media_path'] == str(path.resolve())
    result = json.dumps(detail) + json.dumps(listing)
    assert 'never-expose' not in result
    assert store.get(pending['id'])['status'] == 'processing'
    with bridge.connection() as con:
        with pytest.raises(Exception):
            con.execute("UPDATE items SET status='paused'")


def test_deleted_and_missing_media_are_not_usable(tmp_path):
    item = store.add('已移走文件', status='done', media_path=str(tmp_path / 'missing.mp4'))
    assert not bridge.asset({'item_id': item['id']})['has_media']
    with pytest.raises(ValueError, match='没有可用'):
        bridge.video_frame({'item_id': item['id']})
    store.update(item['id'], deleted=1)
    assert bridge.list_assets({})['total'] == 0
    with pytest.raises(ValueError, match='回收站'):
        bridge.asset({'item_id': item['id']})


def test_transcript_pagination_includes_all_unicode_and_segments():
    text = '😀汉字' * 200
    segments = [{'start': i, 'end': i + 1, 'text': str(i)} for i in range(205)]
    item = store.add('分页', status='done', transcript=text, segments=segments)
    first = bridge.transcript({'item_id': item['id'], 'text_limit': 300})
    second = bridge.transcript({'item_id': item['id'], 'text_offset': first['next_text_offset'], 'segment_offset': 100})
    last = bridge.transcript({'item_id': item['id'], 'segment_offset': 200})
    assert first['text'] + second['text'] == text
    assert first['segments'] + second['segments'] + last['segments'] == segments
    assert second['next_text_offset'] is None and last['next_segment_offset'] is None


def test_handoff_is_explicit_and_rejects_deleted_item():
    assert not bridge.current_asset({})['selected']
    item = store.add('交给 Codex', status='done', transcript='原文')
    response = client.post('/api/codex/select', json={'item_id': item['id']})
    assert response.status_code == 200 and item['id'] in response.json()['prompt']
    assert bridge.current_asset({})['asset']['id'] == item['id']
    store.update(item['id'], deleted=1)
    assert client.post('/api/codex/select', json={'item_id': item['id']}).status_code == 400
    with pytest.raises(ValueError):
        bridge.current_asset({})


def test_cache_override_retains_video_but_respects_cover_only():
    item = {'metadata': {'options': {'mode': 'audio', 'keep': False}}}
    assert assets.options(item)['mode'] == 'audio'
    store.save_settings({'codex_keep_video': 'true'})
    assert assets.options(item)['mode'] == 'video' and assets.options(item)['keep']
    item['metadata']['options']['media'] = False
    assert assets.options(item)['mode'] == 'audio'
    store.save_settings({'codex_keep_video': 'false'})
    assert not assets.options(item)['keep']


@pytest.mark.parametrize('bad', ['../secrets', '', 'a' * 31, 'a' * 33])
def test_invalid_asset_id_rejected(bad):
    with pytest.raises(ValueError):
        bridge.asset({'item_id': bad})
