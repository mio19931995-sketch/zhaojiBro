"""Original reading preserves resources/styles without running source-page code."""
import re

import pytest
from tests.test_wechat import client, HTML, PNG, URL, article_network
from backend import article_archive, engine, store, wechat


def make_article():
    return store.add('旧标题', kind='article', source_url=URL, transcript='用户编辑的简体正文', status='done',
                     metadata={'options': {'cover': False}})


def test_snapshot_styles_images_and_security_headers(client, monkeypatch):
    html = HTML.replace('</head>', '<link rel="stylesheet" href="https://res.wx.qq.com/article.css"></head>')
    html = html.replace('</pre>', '</pre><iframe src="http://localhost/private"></iframe><a href="javascript:alert(1)" onclick="alert(1)">链接</a>')
    calls = []
    def read(url, purpose, check):
        calls.append((url, purpose))
        if purpose == 'style':
            return b'@import "inner.css" screen; .photo{background:url(https://mmbiz.qpic.cn/inline.png)}', 'text/css'
        if purpose == 'asset': return PNG, 'image/png'
        return html.encode(), 'text/html'
    monkeypatch.setattr(wechat, '_read_url', read)
    item = make_article()
    engine.process(item['id'])
    item = store.get(item['id'])
    view = item['metadata']['article_view']
    assert view['saved_images'] == view['image_count'] == 1 and not view['missing_images']
    response = client.get(f"/api/items/{item['id']}/article-view/{view['version']}")
    assert response.status_code == 200
    assert '繁體' in response.text and '繁体' in item['transcript']
    assert 'font-size:21px' in response.text and '@media screen' in response.text
    assert not any(value in response.text for value in ('<script', '<iframe', 'onclick=', 'javascript:'))
    assert "script-src 'none'" in response.headers['content-security-policy']
    assert "frame-ancestors 'self'" in response.headers['content-security-policy']
    assert "frame-ancestors 'none'" in client.get('/api/health').headers['content-security-policy']
    images = re.findall(r'<img[^>]+src="([^"]+)"', response.text)
    assert len(images) == 1
    resource = client.get(images[0])
    assert resource.content == PNG and resource.headers['content-type'] == 'image/png'
    assert "sandbox" in resource.headers['content-security-policy']
    assert sum(url.endswith('inline.png') for url, _ in calls) == 1
    assert client.get(images[0].rsplit('/', 1)[0] + '/manifest.json').status_code == 404
    assert client.get(f"/api/items/{item['id']}/article-view/invalid").status_code == 400
    assert client.post(f"/api/items/{item['id']}/article-refresh", headers={'origin': 'https://evil.example'}).status_code == 403


def test_loading_old_article_keeps_user_edits_and_failed_refresh_keeps_snapshot(client, monkeypatch):
    article_network(monkeypatch)
    item = make_article()
    queued = []
    monkeypatch.setattr(engine, 'enqueue', queued.append)
    assert client.post(f"/api/items/{item['id']}/article-refresh").status_code == 200
    assert queued == [item['id']]
    engine.process(item['id'])
    result = store.get(item['id'])
    assert result['transcript'] == item['transcript'] and result['title'] == item['title']
    assert not store.versions(item['id']) and 'article_view_only' not in result['metadata']
    snapshot = result['metadata']['article_view']
    article_network(monkeypatch, '<p>环境异常，请完成验证</p>')
    client.post(f"/api/items/{item['id']}/article-refresh")
    with pytest.raises(ValueError, match='安全验证'): engine.process(item['id'])
    assert store.get(item['id'])['metadata']['article_view'] == snapshot
    assert store.get(item['id'])['transcript'] == item['transcript']
    # Explicit re-extraction clears a prior failed view-only request.
    article_network(monkeypatch)
    client.post(f"/api/items/{item['id']}/retranscribe")
    engine.process(item['id'])
    assert store.get(item['id'])['transcript'] != item['transcript']
    assert store.version(item['id'], store.versions(item['id'])[0]['id'])['transcript'] == item['transcript']


def test_image_only_has_saved_original_without_fake_text_or_timing(client, monkeypatch):
    article_network(monkeypatch, '<h1 id="activity-name">图片文章</h1><div id="js_content"><img data-src="https://mmbiz.qpic.cn/only.png"></div>')
    item = make_article()
    engine.process(item['id'])
    result = store.get(item['id'])
    assert not result['transcript'] and not result['segments'] and not result['media_path']
    assert result['metadata']['article_view']['saved_images'] == 1


def test_missing_resource_reported_and_original_not_replaced_on_cancel(client, monkeypatch):
    article_network(monkeypatch)
    item = make_article()
    engine.process(item['id'])
    before = store.get(item['id'])
    def unavailable(url, purpose, check):
        if purpose == 'asset': raise ValueError('资源暂不可用')
        return HTML.encode(), 'text/html'
    monkeypatch.setattr(wechat, '_read_url', unavailable)
    engine.process(item['id'])
    result = store.get(item['id'])
    assert result['transcript'] == before['transcript'] and result['metadata']['article_view']['missing_images'] == 1
    assert '原图暂未保存' in (article_archive.location(result) / 'index.html').read_text(encoding='utf-8')
    def cancelled(url, purpose, check):
        if purpose == 'asset': raise InterruptedError('cancelled')
        return HTML.encode(), 'text/html'
    monkeypatch.setattr(wechat, '_read_url', cancelled)
    with pytest.raises(InterruptedError): engine.process(item['id'])
    assert store.get(item['id'])['metadata']['article_view'] == result['metadata']['article_view']
    assert not list((store.DATA / 'articles' / item['id']).glob('building-*'))


@pytest.mark.parametrize('purpose', ['style', 'asset'])
def test_resource_fetch_does_not_expand_network_access(purpose):
    assert wechat._allowed_url('https://res.wx.qq.com/a.css', purpose)
    assert not wechat._allowed_url('http://127.0.0.1/a.css', purpose)
    assert not wechat._allowed_url('https://res.wx.qq.com.evil.example/a.css', purpose)


def test_pending_article_and_non_article_cannot_start_refresh(client, monkeypatch):
    item = make_article()
    monkeypatch.setattr(engine, 'pending', {item['id']})
    assert client.post(f"/api/items/{item['id']}/article-refresh").status_code == 400
    other = store.add('本地文稿', kind='document', transcript='test')
    assert client.post(f"/api/items/{other['id']}/article-refresh").status_code == 400


def test_only_used_css_resources_are_saved_including_media_and_svg(client, monkeypatch):
    html = HTML.replace('</head>', '''<style>
    .unused-emoji {background:url(https://res.wx.qq.com/unused.png)}
    @media(min-width:1px){#js_content {background-image:url("data:image/svg+xml,%3Csvg%20xmlns='http://www.w3.org/2000/svg'/%3E")}}
    #js_content p {background-image:url(https://mmbiz.qpic.cn/used.png)}
    </style></head>''')
    calls = article_network(monkeypatch, html)
    item = make_article()
    engine.process(item['id'])
    assert not any(url.endswith('unused.png') for url, _ in calls)
    assert any(url.endswith('used.png') for url, _ in calls)
    result = store.get(item['id'])
    assert result['metadata']['article_view']['missing_resources'] == 0
    assert any(f['mime'] == 'image/svg+xml' for f in article_archive.manifest(result)['files'].values())


def test_transient_resource_failure_is_retried_once(client, monkeypatch):
    calls = []
    def read(url, purpose, check):
        if purpose == 'asset':
            calls.append(url)
            if len(calls) == 1:
                try: raise wechat.httpx.ReadTimeout('synthetic timeout')
                except wechat.httpx.ReadTimeout as cause: raise ValueError('网络中断') from cause
            return PNG, 'image/png'
        return HTML.encode(), 'text/html'
    monkeypatch.setattr(wechat, '_read_url', read)
    item = make_article()
    engine.process(item['id'])
    assert len(calls) == 2
    assert store.get(item['id'])['metadata']['article_view']['missing_images'] == 0
