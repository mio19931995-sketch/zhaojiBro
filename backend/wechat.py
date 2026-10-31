"""Extract publicly visible WeChat article text without a browser or an ASR model."""
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from html import unescape
from html.parser import HTMLParser
import re
import time
from urllib.parse import parse_qs, urljoin, urlsplit, urlunsplit

import httpx

HOST = 'mp.weixin.qq.com'
MAX_HTML_BYTES = 8 * 1024 * 1024
MAX_COVER_BYTES = 4 * 1024 * 1024
USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130.0.0.0 Safari/537.36'


def is_wechat_url(url):
    try:
        return urlsplit(url).hostname == HOST
    except ValueError:
        return False


def normalize_url(url):
    url = unescape(url.strip()).rstrip('。，；！、）】')
    parsed = urlsplit(url)
    if (parsed.hostname != HOST or parsed.scheme not in ('http', 'https') or
            parsed.username or parsed.password or parsed.port not in (None, 80, 443)):
        raise ValueError('请使用 mp.weixin.qq.com 的公众号文章分享链接')
    query = parse_qs(parsed.query)
    if not (re.fullmatch(r'/s/[A-Za-z0-9_-]{1,128}', parsed.path) or
            parsed.path == '/s' and query.get('__biz') and (query.get('mid') or query.get('appmsgid'))):
        raise ValueError('请在微信公众号文章中选择「复制链接」，目前支持单篇文章链接，不支持公众号主页')
    return urlunsplit(('https', HOST, parsed.path, parsed.query, ''))


def _allowed_url(url, purpose):
    parsed = urlsplit(url)
    if parsed.scheme not in ('https', 'http') or parsed.username or parsed.password or parsed.port not in (None, 80, 443):
        return False
    host = parsed.hostname or ''
    return (host == HOST or purpose in ('cover', 'asset', 'style') and
            (host.endswith('.qpic.cn') or host.endswith('.qlogo.cn') or
             host in ('res.wx.qq.com', 'res2.wx.qq.com', 'wxappdl.qq.com')))


def _read_url(url, purpose, check_cancel):
    from . import engine
    limit = {'article': MAX_HTML_BYTES, 'cover': MAX_COVER_BYTES, 'style': 2 * 1024 * 1024,
             'asset': 16 * 1024 * 1024}[purpose]
    deadline = time.monotonic() + 45
    try:
        with httpx.Client(timeout=httpx.Timeout(15, connect=10), follow_redirects=False,
                          headers={'User-Agent': USER_AGENT, 'Referer': 'https://mp.weixin.qq.com/'}) as client:
            for _ in range(5):
                check_cancel()
                if time.monotonic() > deadline:
                    raise ValueError('公众号访问超时，请稍后重试')
                if not _allowed_url(url, purpose):
                    raise ValueError('公众号页面跳转到不支持的地址，请打开原文确认链接')
                engine.validate_url(url)
                with client.stream('GET', url) as response:
                    if response.status_code in (301, 302, 303, 307, 308):
                        location = response.headers.get('location')
                        if not location:
                            raise ValueError('公众号返回了无效跳转，请从微信重新复制文章链接')
                        url = urljoin(url, location)
                        continue
                    if response.status_code in (401, 403, 429):
                        raise ValueError('微信限制了访问，请在微信中打开原文，完成必要的验证后稍后重试')
                    if response.status_code >= 400:
                        raise ValueError(f'公众号文章访问失败（HTTP {response.status_code}），请先确认原文能正常打开')
                    content = bytearray()
                    for part in response.iter_bytes(64 * 1024):
                        check_cancel()
                        if time.monotonic() > deadline:
                            raise ValueError('公众号访问超时，请稍后重试')
                        content.extend(part)
                        if len(content) > limit:
                            raise ValueError('公众号页面或封面过大，未保存不完整的内容')
                    check_cancel()
                    return bytes(content), response.headers.get('content-type', '')
            raise ValueError('公众号跳转次数过多，请从微信重新复制文章链接')
    except httpx.HTTPError as exc:
        raise ValueError('公众号访问失败，请检查网络并确认原文可访问后重试') from exc


def fetch_html(url, check_cancel=lambda: None):
    content, content_type = _read_url(normalize_url(url), 'article', check_cancel)
    if content_type and not any(t in content_type.lower() for t in ('text/html', 'application/xhtml+xml')):
        raise ValueError('链接返回的不是公众号文章网页，请重新复制文章链接')
    try:
        return content.decode('utf-8-sig')
    except UnicodeError as exc:
        raise ValueError('公众号页面编码异常，未保存不完整的内容') from exc


@dataclass
class Node:
    tag: str
    attrs: dict
    children: list = field(default_factory=list)


class ArticleParser(HTMLParser):
    targets = {'js_content', 'activity-name', 'js_name', 'publish_time', 'js_author_name'}
    void = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.roots = {}
        self.meta = {}
        self.stack = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'meta':
            name = attrs.get('property') or attrs.get('name')
            if name:
                self.meta.setdefault(name.lower(), attrs.get('content') or '')
        node = Node(tag, attrs)
        if self.stack:
            if len(self.stack) >= 256:
                raise ValueError('公众号正文结构过于复杂，未保存不完整的内容')
            self.stack[-1].children.append(node)
        elif attrs.get('id') in self.targets and attrs['id'] not in self.roots:
            self.roots[attrs['id']] = node
        else:
            return
        if tag not in self.void:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.void:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        if self.stack:
            self.stack[-1].children.append(data)


BLOCKS = {'p', 'div', 'section', 'article', 'blockquote', 'ul', 'ol', 'li', 'pre', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'tr'}
SKIP = {'script', 'style', 'noscript', 'iframe', 'video', 'audio', 'button', 'form', 'svg', 'template'}


def _render(node, pre=False):
    if isinstance(node, str):
        return node if pre else re.sub(r'\s+', ' ', node)
    # WeChat initially hides the entire js_content container until its JS runs.
    # Its HTML remains the public article; only hidden descendants are excluded.
    if node.tag in SKIP or node.attrs.get('id') != 'js_content' and (
            'hidden' in node.attrs or re.search(r'(?:display\s*:\s*none|visibility\s*:\s*hidden)', node.attrs.get('style') or '', re.I)):
        return ''
    if node.tag in ('br', 'hr'):
        return '\n'
    value = ''.join(_render(child, pre or node.tag == 'pre') for child in node.children)
    if node.tag == 'a':
        href = node.attrs.get('href') or ''
        if href.startswith(('https://', 'http://')) and href not in value:
            value += f'（{href}）'
    if node.tag in ('td', 'th'):
        return value + '\t'
    if node.tag in BLOCKS:
        return '\n' + value.strip('\n') + '\n'
    return value


def _plain(node):
    if not node:
        return ''
    text = re.sub(r'\n[ \t]+(?=\n)', '\n', _render(node))
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_article(html, allow_images=False):
    parser = ArticleParser()
    parser.feed(html)
    parser.close()
    body = _plain(parser.roots.get('js_content'))
    def has_image(node):
        return isinstance(node, Node) and (node.tag == 'img' and bool(node.attrs.get('data-src') or node.attrs.get('src')) or
                                           any(has_image(child) for child in node.children))
    if not body and not (allow_images and has_image(parser.roots.get('js_content'))):
        if any(word in html for word in ('环境异常', '访问过于频繁', '完成验证', '安全验证', '验证码')):
            raise ValueError('微信要求安全验证，未提取到正文。请在微信中打开原文并完成验证，稍后重试；也可复制正文后在文稿库新建文稿')
        if any(word in html for word in ('内容已被删除', '内容已删除', '内容因违规无法查看', '该内容无法查看')):
            raise ValueError('公众号文章已删除或不可查看，请确认原文仍可访问')
        raise ValueError('未找到可提取的公众号正文。文章可能需要登录、付费，或仅包含图片/视频；本功能提取公开文字，不识别图片、不下载内嵌视频')
    title = _plain(parser.roots.get('activity-name')) or parser.meta.get('og:title', '').strip()
    if not title:
        raise ValueError('未找到公众号文章标题，请确认链接指向单篇文章')
    published = _plain(parser.roots.get('publish_time'))
    if not published:
        match = re.search(r"\bcreate_time\s*:\s*['\"](\d{4}-\d{2}-\d{2}(?: \d{2}:\d{2}(?::\d{2})?)?)['\"]", html)
        if match:
            published = match[1]
        else:
            match = re.search(r"\bvar\s+ct\s*=\s*['\"](\d{10})['\"]", html)
            if match:
                published = datetime.fromtimestamp(int(match[1]), timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M')
    return {'title': title, 'text': body, 'author': parser.meta.get('author') or _plain(parser.roots.get('js_author_name')),
            'account': _plain(parser.roots.get('js_name')), 'published_at': published,
            'cover_url': parser.meta.get('og:image', '')}


def extract(item):
    from . import article_archive, engine, store
    item_id = item['id']
    check = lambda: engine.check_cancel(item_id)
    url = normalize_url(item['source_url'])
    store.update(item_id, phase='正在提取公众号文章', progress=15)
    html = fetch_html(url, check)
    article = parse_article(html, allow_images=True)
    check()
    metadata = dict(item['metadata'])
    view_only = metadata.pop('article_view_only', False)
    store.update(item_id, phase='正在保存原文图片与排版', progress=40)
    metadata['article_view'] = article_archive.create(html, item_id, article, url, check)
    if view_only:
        with engine.job_lock:
            check()
            store.update(item_id, metadata=metadata, status='done', progress=100, error='', phase='原文图文已保存')
        return
    metadata.update(source_kind='wechat_article', platform='微信公众号', transcript_source='wechat_article',
                    author=article['author'], account=article['account'], published_at=article['published_at'],
                    date=article['published_at'][:10], download_only=False, action='article',
                    cover_url=article['cover_url'])
    metadata.pop('asr', None)
    metadata.pop('article_cover_warning', None)
    if article['cover_url'] and metadata.get('options', {}).get('cover', store.settings()['download_cover'] == 'true'):
        try:
            content, _ = _read_url(article['cover_url'], 'cover', check)
            extension = ('jpg' if content.startswith(b'\xff\xd8\xff') else
                         'png' if content.startswith(b'\x89PNG\r\n\x1a\n') else
                         'webp' if content[:4] == b'RIFF' and content[8:12] == b'WEBP' else '')
            if not extension:
                raise ValueError('封面不是支持的图片格式')
            cover = store.DATA / 'documents' / f'{item_id}-cover.{extension}'
            check()
            cover.write_bytes(content)
            metadata['cover_path'] = str(cover)
        except InterruptedError:
            raise
        except (ValueError, OSError):
            metadata['article_cover_warning'] = '封面暂未获取，文章正文已保留'
    text = engine.to_simplified(article['text'])
    with engine.job_lock:
        check()
        (store.DATA / 'documents' / f'{item_id}.txt').write_text(text, encoding='utf-8')
        store.update_with_snapshot(item_id, '重新提取公众号文章前', title=engine.to_simplified(article['title']),
                                   kind='article', source_url=url, transcript=text, segments=[], media_path='', duration=0,
                                   metadata=metadata, status='done', phase='公众号文章已提取', progress=100, error='')
