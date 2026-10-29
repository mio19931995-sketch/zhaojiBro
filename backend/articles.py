"""Public single-article adapters; browser captures use the same archive pipeline."""
from html import unescape
import re
import time
from urllib.parse import urlsplit, urlunsplit, urljoin

import httpx
from . import wechat

NAMES = {'zhihu': '知乎', 'toutiao': '今日头条', 'xiaohongshu': '小红书'}
HOSTS = {'zhihu': {'www.zhihu.com', 'zhuanlan.zhihu.com', 'zhihu.com'},
         'toutiao': {'www.toutiao.com', 'toutiao.com', 'm.toutiao.com'},
         'xiaohongshu': {'www.xiaohongshu.com', 'xiaohongshu.com', 'xhslink.com', 'www.xhslink.com'}}
CDNS = {'zhihu': ('zhimg.com', 'static.zhihu.com'), 'toutiao': ('byteimg.com', 'pstatp.com', 'toutiaoimg.com', 'toutiaostatic.com', 'lf3-cdn-tos.bytegoofy.com', 'bytedance.com', 'bytedance.net'),
        'xiaohongshu': ('xhscdn.com', 'xiaohongshu.com')}


def platform(url):
    try: host = urlsplit(url).hostname
    except ValueError: return ''
    return next((key for key, hosts in HOSTS.items() if host in hosts), '')


def normalize_url(url):
    url = unescape(url.strip()).rstrip('。，；！、）】')
    key = platform(url)
    parsed = urlsplit(url)
    if not key or parsed.scheme not in ('https', 'http') or parsed.username or parsed.password or parsed.port not in (None, 80, 443):
        raise ValueError('请使用知乎、今日头条或小红书的完整文章分享链接')
    patterns = {'zhihu': r'/(?:p/\d+|question/\d+/answer/\d+|tardis/(?:zm/ans|jm/art)/\d+)/?',
                'toutiao': r'/(?:article|i)/?\d+/?',
                'xiaohongshu': r'/(?:explore|discovery/item)/[a-fA-F0-9]{24}/?'}
    short = key == 'xiaohongshu' and parsed.hostname in ('xhslink.com', 'www.xhslink.com')
    if not (re.fullmatch(patterns[key], parsed.path) or short and re.fullmatch(r'/(?:a/|o/)?[A-Za-z0-9]+', parsed.path)):
        raise ValueError(f'请使用{NAMES[key]}单篇文章/回答/图文链接，暂不支持主页、搜索页或列表页')
    # Signed share parameters (especially xsec_token) must survive unchanged.
    return urlunsplit(('https', parsed.hostname, parsed.path, parsed.query, ''))


def content_id(url):
    p = urlsplit(normalize_url(url))
    if p.hostname in ('xhslink.com', 'www.xhslink.com'): return ''
    return p.path.rstrip('/').split('/')[-1].removeprefix('i')


def read_url(url, purpose, check, source_url):
    from . import engine
    key = platform(source_url)
    cap = {'article': 8, 'style': 2, 'asset': 16}[purpose] * 1024 * 1024
    deadline = time.monotonic() + 40
    try:
        with httpx.Client(timeout=httpx.Timeout(15, connect=10), follow_redirects=False,
                          headers={'User-Agent': wechat.USER_AGENT, 'Referer': source_url}) as client:
            for _ in range(5):
                check()
                p = urlsplit(url)
                allowed = p.hostname in HOSTS[key] or purpose != 'article' and any(p.hostname == domain or (p.hostname or '').endswith('.' + domain) for domain in CDNS[key])
                if not allowed or p.scheme not in ('https', 'http') or p.username or p.password or p.port not in (None, 80, 443):
                    raise ValueError('文章资源地址不受支持')
                engine.validate_url(url)
                with client.stream('GET', url) as response:
                    if response.status_code in (301, 302, 303, 307, 308):
                        url = urljoin(url, response.headers.get('location', ''))
                        continue
                    if response.status_code >= 400:
                        raise ValueError(f'{NAMES[key]}访问受限（HTTP {response.status_code}），请在平台窗口打开文章并完成必要的登录或验证')
                    data = bytearray()
                    for part in response.iter_bytes(65536):
                        check()
                        data.extend(part)
                        if len(data) > cap: raise ValueError('文章资源过大，未保存不完整的文件')
                        if time.monotonic() > deadline: raise ValueError('文章资源访问超时，请重试')
                    return bytes(data), response.headers.get('content-type', '')
            raise ValueError('文章跳转次数过多')
    except httpx.HTTPError as exc:
        raise ValueError(f'{NAMES[key]}网络访问失败，请重试或在平台窗口打开') from exc


def parse_capture(html):
    parser = wechat.ArticleParser()
    parser.targets = {'lulu-article-root', 'lulu-title', 'lulu-author', 'lulu-date'}
    parser.feed(html)
    root = parser.roots.get('lulu-article-root')
    if not root: raise ValueError('没有取得已打开的文章正文，请在平台窗口加载完整文章后重试')
    # Header metadata is supplied through meta tags, outside the body snapshot.
    title = parser.meta.get('lulu:title', '').strip()
    if not title or len(title) > 500: raise ValueError('文章标题缺失或过长')
    text = wechat._plain(root)
    return {'title': title, 'text': text, 'author': parser.meta.get('lulu:author', ''), 'account': '',
            'published_at': parser.meta.get('lulu:date', ''), 'cover_url': ''}


def prepare_capture(item, url, html, view_only=False, media=False):
    from . import store
    url = normalize_url(url)
    if platform(item['source_url']) != platform(url) or content_id(item['source_url']) and content_id(item['source_url']) != content_id(url):
        raise ValueError('平台窗口中的文章与当前任务不一致，请重新打开原链接')
    metadata = dict(item['metadata'])
    if media:
        metadata.pop('source_kind', None)
        store.update(item['id'], kind='media', source_url=url, metadata=metadata)
        return
    parse_capture(html)
    folder = store.DATA / 'article-inputs'
    folder.mkdir(exist_ok=True)
    target = folder / (item['id'] + '.html')
    target.write_text(html, encoding='utf-8')
    metadata.update(source_kind=platform(url) + '_article', article_view_only=view_only, article_capture=True)
    store.update(item['id'], kind='article', source_url=url, metadata=metadata)


def extract(item):
    from . import article_archive, engine, store
    key = platform(item['source_url'])
    check = lambda: engine.check_cancel(item['id'])
    path = store.DATA / 'article-inputs' / (item['id'] + '.html')
    if not item['metadata'].get('article_capture') or not path.is_file():
        raise ValueError(f'请点击任务的「在平台窗口抓取」，在{NAMES[key]}正常打开文章后保存图文')
    html = path.read_text(encoding='utf-8')
    article = parse_capture(html)
    store.update(item['id'], phase='正在保存原文图片与排版', progress=40)
    reader = lambda url, purpose, cancel: read_url(url, purpose, cancel, item['source_url'])
    view = article_archive.create(html, item['id'], article, item['source_url'], check, reader=reader)
    metadata = dict(item['metadata'])
    view_only = metadata.pop('article_view_only', False)
    metadata.pop('article_capture', None)
    metadata.update(article_view=view, platform=NAMES[key], download_only=False, action='article')
    with engine.job_lock:
        check()
        if view_only:
            store.update(item['id'], metadata=metadata, status='done', progress=100, error='', phase='原文图文已保存')
        else:
            metadata.update(author=article['author'], account='', published_at=article['published_at'], transcript_source=key + '_article')
            text = engine.to_simplified(article['text'])
            (store.DATA / 'documents' / (item['id'] + '.txt')).write_text(text, encoding='utf-8')
            store.update_with_snapshot(item['id'], '重新提取文章前', title=engine.to_simplified(article['title']), transcript=text,
                                       segments=[], media_path='', duration=0, metadata=metadata,
                                       status='done', progress=100, error='', phase='文章图文已提取')
        path.unlink(missing_ok=True)
