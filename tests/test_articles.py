"""Platform identity, browser capture, resource isolation and text history."""
import pytest
from tests.test_wechat import client, PNG
from backend import articles, article_archive, engine, store

URLS = ['https://zhuanlan.zhihu.com/p/123456', 'https://www.toutiao.com/article/123456/',
        'https://www.xiaohongshu.com/explore/1234567890abcdef12345678?xsec_token=keep%2Bthis%3D&xsec_source=pc_share']
HTML = '<html><head><meta name="lulu:title" content="原文标题"><meta name="lulu:author" content="作者"></head><body><div id="lulu-article-root"><p style="font-size:23px">繁體正文</p><img src="https://pic.zhimg.com/sample.png"></div></body></html>'


@pytest.mark.parametrize('url', URLS)
def test_platform_capture_archive_refresh_and_history(client, monkeypatch, url):
    monkeypatch.setattr(engine, 'validate_url', lambda url: None)
    monkeypatch.setattr(articles, 'read_url', lambda *args: (PNG, 'image/png'))
    item = client.post('/api/links', json={'urls':[url]}).json()[0]
    assert item['kind'] == 'article' and item['source_url'] == url
    with pytest.raises(ValueError, match='平台窗口'): engine.process(item['id'])
    capture = {'url': url, 'html': HTML}
    assert client.post(f"/api/items/{item['id']}/article-capture", json=capture).status_code == 200
    engine.process(item['id'])
    result = store.get(item['id'])
    assert result['transcript'] == '繁体正文' and result['metadata']['platform'] == articles.NAMES[articles.platform(url)]
    assert result['metadata']['article_view']['saved_images'] == 1
    assert not result['media_path'] and not result['segments']
    store.update(item['id'], title='用户标题', transcript='用户修改')
    client.post(f"/api/items/{item['id']}/article-capture", json=capture | {'view_only':True})
    engine.process(item['id'])
    assert store.get(item['id'])['title'] == '用户标题' and store.get(item['id'])['transcript'] == '用户修改'
    client.post(f"/api/items/{item['id']}/article-capture", json=capture)
    engine.process(item['id'])
    assert any(store.version(item['id'], v['id'])['transcript'] == '用户修改' for v in store.versions(item['id']))


@pytest.mark.parametrize('url', ['https://www.zhihu.com/', 'https://www.zhihu.com/question/123', 'https://www.toutiao.com/video/123/',
                               'https://www.xiaohongshu.com/explore', 'https://user@zhuanlan.zhihu.com/p/1', 'https://zhuanlan.zhihu.com.evil.example/p/1'])
def test_only_single_article_links(url):
    with pytest.raises(ValueError): articles.normalize_url(url)


def test_captures_must_match_task_and_have_body(client, monkeypatch):
    monkeypatch.setattr(engine, 'validate_url', lambda url: None)
    item = client.post('/api/links', json={'urls':[URLS[0]]}).json()[0]
    endpoint = f"/api/items/{item['id']}/article-capture"
    assert client.post(endpoint, json={'url':URLS[1], 'html':HTML}).status_code == 400
    assert client.post(endpoint, json={'url':URLS[0].replace('123456','88888'), 'html':HTML}).status_code == 400
    assert client.post(endpoint, json={'url':URLS[0], 'html':'<h1>登录</h1>'}).status_code == 400
    assert client.post(endpoint, json={'url':URLS[0], 'html':HTML}, headers={'origin':'https://www.zhihu.com'}).status_code == 403


def test_xhs_short_urls_keep_signature_and_video_retains_old_pipeline(client, monkeypatch):
    assert articles.normalize_url('http://xhslink.com/a/AbC123') == 'https://xhslink.com/a/AbC123'
    monkeypatch.setattr(engine, 'validate_url', lambda url: None)
    item = client.post('/api/links', json={'urls':['https://xhslink.com/a/AbC123']}).json()[0]
    response = client.post(f"/api/items/{item['id']}/article-capture", json={'url':URLS[2], 'media':True})
    assert response.status_code == 200
    assert store.get(item['id'])['kind'] == 'media'


@pytest.mark.parametrize('url', ['http://127.0.0.1/secret', 'https://evil.example/a.png', 'https://pic.zhimg.com.evil.example/a.png'])
def test_resource_domain_boundary(client, url):
    with pytest.raises(ValueError, match='地址不受支持'):
        articles.read_url(url, 'asset', lambda: None, URLS[0])
