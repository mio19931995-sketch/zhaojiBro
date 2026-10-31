"""Public article extraction, network boundaries, history and existing handoffs."""
import base64
import os
from pathlib import Path
import tempfile

import httpx
import pytest

os.environ.setdefault('LULU_DATA_DIR', tempfile.mkdtemp(prefix='lulu-wechat-tests-'))
from fastapi.testclient import TestClient
from backend.main import app
from backend import assets, codex_bridge, engine, store, wechat

URL = 'https://mp.weixin.qq.com/s/synthetic_article'
HTML = (Path(__file__).parent / 'fixtures' / 'wechat_article.html').read_text(encoding='utf-8')
PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aMuoAAAAASUVORK5CYII=')


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(store, 'DATA', tmp_path)
    monkeypatch.setattr(store, 'DB', tmp_path / 'library.sqlite3')
    monkeypatch.setenv('LULU_DATA_DIR', str(tmp_path))
    for part in ('media', 'documents', 'models', 'outputs', 'trash'):
        (tmp_path / part).mkdir()
    store.init()
    return TestClient(app, headers={'X-Lulu-Client': 'desktop'})


def article_network(monkeypatch, html=HTML):
    requests = []
    def read(url, purpose, check_cancel):
        check_cancel()
        requests.append((url, purpose))
        return (PNG, 'image/png') if purpose in ('cover', 'asset') else (html.encode(), 'text/html; charset=utf-8')
    monkeypatch.setattr(wechat, '_read_url', read)
    return requests


def test_parser_preserves_paragraphs_inline_text_source_links_and_provenance():
    value = wechat.parse_article(HTML)
    assert value['title'] == '测试公众号文章' and value['author'] == '测试作者'
    assert value['account'] == '测试公众号' and value['published_at'] == '2026-10-27 13:21'
    assert '繁體中文，& English。\n\n第二段\n下一行。' in value['text']
    assert 'https://example.com/資料?x=1&y=2' in value['text']
    assert 'code line 1\n  indented line 2' in value['text']
    assert not any(word in value['text'] for word in ('隐藏广告', 'do_not_execute', '推荐广告', '.ad{}'))


@pytest.mark.parametrize('html,message', [
    ('<p>环境异常，请完成验证</p>', '微信要求安全验证'),
    ('<p>该内容已被删除</p>', '已删除'),
    ('<div id="js_content"><img src="x"></div>', '未找到可提取'),
    ('<div>登录后查看文章正文</div>', '未找到可提取'),
    ('<div id="js_content">正文</div>', '未找到公众号文章标题'),
])
def test_unavailable_and_image_only_pages_fail_without_fake_transcripts(html, message):
    with pytest.raises(ValueError, match=message):
        wechat.parse_article(html)


def test_long_urls_keep_signature_query_and_short_urls_drop_only_share_fragment():
    assert wechat.normalize_url('http://mp.weixin.qq.com/s/abc_123#rd。') == 'https://mp.weixin.qq.com/s/abc_123'
    value = 'https://mp.weixin.qq.com/s?__biz=MzA%3D%3D&amp;mid=123&amp;idx=1&amp;sn=AbCD#wechat_redirect'
    assert wechat.normalize_url(value) == 'https://mp.weixin.qq.com/s?__biz=MzA%3D%3D&mid=123&idx=1&sn=AbCD'


@pytest.mark.parametrize('url', [
    'https://mp.weixin.qq.com/', 'https://mp.weixin.qq.com/s',
    'https://mp.weixin.qq.com.evil.example/s/abc', 'https://mp.weixin.qq.com@localhost/s/abc',
    'https://user@mp.weixin.qq.com/s/abc', 'file://mp.weixin.qq.com/s/abc', 'https://mp.weixin.qq.com:81/s/abc',
])
def test_only_supported_public_article_links_are_accepted(url):
    with pytest.raises(ValueError):
        wechat.normalize_url(url)


def mock_http(monkeypatch, handler):
    client_type = httpx.Client
    monkeypatch.setattr(wechat.httpx, 'Client', lambda **kwargs: client_type(transport=httpx.MockTransport(handler), **kwargs))
    monkeypatch.setattr(engine, 'validate_url', lambda url: None)


def test_same_host_redirect_and_no_credentials_in_article_request(monkeypatch):
    visited = []
    def handler(request):
        visited.append(str(request.url))
        assert not request.headers.get('authorization') and not request.headers.get('cookie')
        if len(visited) == 1:
            return httpx.Response(302, headers={'location': '/s/redirected'})
        return httpx.Response(200, text=HTML, headers={'content-type': 'text/html'})
    mock_http(monkeypatch, handler)
    assert wechat.parse_article(wechat.fetch_html(URL))['account'] == '测试公众号'
    assert len(visited) == 2


@pytest.mark.parametrize('target', ['http://127.0.0.1/private', 'https://evil.example/article', 'file:///secret', 'https://user@mp.weixin.qq.com/s/a'])
def test_redirects_cannot_reach_unrelated_hosts_or_local_files(monkeypatch, target):
    visited = []
    def handler(request):
        visited.append(request)
        return httpx.Response(302, headers={'location': target})
    mock_http(monkeypatch, handler)
    with pytest.raises(ValueError, match='不支持的地址'):
        wechat.fetch_html(URL)
    assert len(visited) == 1


@pytest.mark.parametrize('status', [403, 429, 404, 500])
def test_http_errors_are_actionable(monkeypatch, status):
    mock_http(monkeypatch, lambda request: httpx.Response(status))
    with pytest.raises(ValueError, match='微信限制|访问失败'):
        wechat.fetch_html(URL)


def test_large_response_and_cancellation_do_not_return_partial_content(monkeypatch):
    mock_http(monkeypatch, lambda request: httpx.Response(200, text=HTML, headers={'content-type': 'text/html'}))
    monkeypatch.setattr(wechat, 'MAX_HTML_BYTES', 30)
    with pytest.raises(ValueError, match='过大'):
        wechat.fetch_html(URL)
    def cancelled():
        raise InterruptedError('cancelled')
    with pytest.raises(InterruptedError):
        wechat.fetch_html(URL, cancelled)


def test_mixed_link_import_old_error_retry_exports_and_codex(client, monkeypatch):
    network = article_network(monkeypatch)
    monkeypatch.setattr(engine, 'validate_url', lambda url: None)
    monkeypatch.setattr(assets, 'acquire', lambda item: pytest.fail('An article must not reach yt-dlp'))
    monkeypatch.setattr(engine, 'installed', lambda name: pytest.fail('An article must not require ASR'))
    response = client.post('/api/links', json={'urls': [URL, 'https://example.com/video'], 'download_only': True})
    assert response.status_code == 200
    article, video = response.json()
    assert article['kind'] == 'article' and not article['metadata']['download_only']
    assert video['kind'] == 'media' and video['metadata']['download_only']
    legacy = store.add('链接转写 · mp.weixin.qq.com', source_url=URL, status='error', error='Unsupported URL',
                       metadata={'download_only': True, 'action': 'assets', 'options': {'cover': True}})
    queued = []
    monkeypatch.setattr(engine, 'enqueue', queued.append)
    assert client.post(f"/api/items/{legacy['id']}/start").status_code == 200
    assert queued == [legacy['id']]
    engine.process(legacy['id'])
    result = store.get(legacy['id'])
    assert result['kind'] == 'article' and result['status'] == 'done' and result['progress'] == 100
    assert not result['error'] and not result['media_path'] and not result['segments']
    assert result['metadata']['action'] == 'article' and not result['metadata']['download_only']
    assert '繁体中文' in result['transcript'] and 'https://example.com/資料?x=1&y=2' in result['transcript']
    assert Path(result['metadata']['cover_path']).read_bytes() == PNG
    assert client.get(f"/api/items/{result['id']}/cover").status_code == 200
    assert {purpose for _, purpose in network} == {'article', 'cover', 'asset'}
    md = client.get(f"/api/items/{result['id']}/export/md").content.decode('utf-8-sig')
    assert all(value in md for value in ('测试作者', '测试公众号', '2026-10-27 13:21', URL))
    assert client.get(f"/api/items/{result['id']}/export/txt").content.decode('utf-8-sig') == result['transcript']
    assert client.get(f"/api/items/{result['id']}/export/srt").status_code == 400
    prompt = client.post('/api/codex/select', json={'item_id': result['id']}).json()['prompt']
    assert result['id'] in prompt and 'lulu_read_transcript' in prompt and 'lulu_video_frame' not in prompt
    asset = codex_bridge.asset({'item_id': result['id']})
    assert asset['author'] == '测试作者' and not asset['has_media'] and '公众号文字文章' in asset['usage']
    assert codex_bridge.transcript({'item_id': result['id']})['text'] == result['transcript']
    with pytest.raises(ValueError, match='公众号文章是文字素材'):
        codex_bridge.video_frame({'item_id': result['id']})


def test_reextract_snapshots_success_and_preserves_original_on_failure_or_cancel(client, monkeypatch):
    article_network(monkeypatch)
    item = store.add('旧标题', kind='article', source_url=URL, transcript='我的编辑正文', status='done', metadata={'options': {'cover': True}})
    queued = []
    monkeypatch.setattr(engine, 'enqueue', queued.append)
    assert client.post(f"/api/items/{item['id']}/retranscribe").status_code == 200
    assert queued == [item['id']] and store.get(item['id'])['transcript'] == '我的编辑正文'
    engine.process(item['id'])
    versions = store.versions(item['id'])
    assert len(versions) == 1 and store.version(item['id'], versions[0]['id'])['transcript'] == '我的编辑正文'
    store.restore_version(item['id'], versions[0]['id'])
    assert store.get(item['id'])['transcript'] == '我的编辑正文'
    article_network(monkeypatch, '<p>环境异常，请完成验证</p>')
    with pytest.raises(ValueError, match='安全验证'):
        engine.process(item['id'])
    assert store.get(item['id'])['transcript'] == '我的编辑正文'
    article_network(monkeypatch)
    def cancel(url, purpose, check):
        if purpose == 'cover':
            raise InterruptedError('cancelled')
        return HTML.encode(), 'text/html'
    monkeypatch.setattr(wechat, '_read_url', cancel)
    with pytest.raises(InterruptedError):
        engine.process(item['id'])
    assert store.get(item['id'])['transcript'] == '我的编辑正文'


def test_global_cover_off_video_cache_on_and_cover_failure_are_text_safe(client, monkeypatch):
    requests = article_network(monkeypatch)
    store.save_settings({'download_cover': 'false', 'codex_keep_video': 'true'})
    item = store.add('待提取', source_url=URL)
    engine.process(item['id'])
    assert [entry for entry in requests if entry[1] != 'asset'] == [(URL, 'article')]
    assert not store.get(item['id'])['media_path']
    assert store.settings()['download_cover'] == 'false'
    def cover_error(url, purpose, check):
        if purpose == 'cover':
            raise ValueError('image unavailable')
        return HTML.encode(), 'text/html'
    monkeypatch.setattr(wechat, '_read_url', cover_error)
    store.update(item['id'], metadata={'options': {'cover': True}})
    engine.process(item['id'])
    result = store.get(item['id'])
    assert result['status'] == 'done' and result['transcript'] and result['metadata']['article_cover_warning']


def test_video_acquisition_and_download_only_behavior_are_unchanged(client, monkeypatch):
    item = store.add('视频', source_url='https://example.com/movie', metadata={'download_only': True})
    calls = []
    def acquire(value):
        calls.append(value['id'])
        return value, False
    monkeypatch.setattr(assets, 'acquire', acquire)
    monkeypatch.setattr(wechat, 'extract', lambda item: pytest.fail('Video must keep the existing path'))
    engine.process(item['id'])
    assert calls == [item['id']] and store.get(item['id'])['phase'] == '采集完成'
