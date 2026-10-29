"""Read-only article snapshots: original markup/styles plus local image/font files."""
from concurrent.futures import ThreadPoolExecutor
from html import escape
import base64
import hashlib
import json
from pathlib import Path
import re
import tempfile
import threading
import time
from urllib.parse import quote, unquote_to_bytes, urldefrag, urljoin
from xml.etree.ElementTree import Element
import uuid

import tinycss2
import cssselect2
from . import wechat

MAX_RESOURCES = 240
MAX_TOTAL_BYTES = 128 * 1024 * 1024
MAX_STYLES = 40
ALLOWED_TAGS = set(('div span p section article main header footer aside h1 h2 h3 h4 h5 h6 '
                    'a em strong b i u s strike del ins sup sub small big font center mark '
                    'blockquote pre code ol ul li dl dt dd table thead tbody tfoot tr th td col colgroup '
                    'figure figcaption picture img br hr ruby rt rp details summary '
                    'svg g path rect circle ellipse line polygon polyline text tspan defs '
                    'lineargradient radialgradient stop clippath mask pattern use symbol '
                    'foreignobject switch').split())
DROP = {'script', 'noscript', 'iframe', 'object', 'embed', 'form', 'input', 'button', 'textarea',
        'select', 'option', 'link', 'meta', 'base', 'template', 'video', 'audio', 'source', 'track',
        'animate', 'animatetransform', 'set'}


class PageParser(wechat.ArticleParser):
    targets = {'img-content', 'js_content', 'lulu-article-root'}

    def __init__(self):
        super().__init__()
        self.outer = []
        self.ancestors = {}
        self.styles = []
        self.in_style = None
        self.body_attrs = {}
        self.html_attrs = {}

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == 'html':
            self.html_attrs = values
        if tag == 'body':
            self.body_attrs = values
        if values.get('id') in self.targets and not self.stack:
            self.ancestors[values['id']] = list(self.outer)
        if tag == 'style':
            self.in_style = []
        if tag == 'link' and 'stylesheet' in (values.get('rel') or '').lower():
            self.styles.append(('link', values.get('href') or '', values.get('media') or ''))
        super().handle_starttag(tag, attrs)
        if tag not in self.void:
            self.outer.append((tag, values))

    def handle_data(self, data):
        if self.in_style is not None:
            self.in_style.append(data)
        super().handle_data(data)

    def handle_endtag(self, tag):
        if tag == 'style' and self.in_style is not None:
            self.styles.append(('inline', ''.join(self.in_style), ''))
            self.in_style = None
        super().handle_endtag(tag)
        for index in range(len(self.outer) - 1, -1, -1):
            if self.outer[index][0] == tag:
                del self.outer[index:]
                break


def binary_type(content, content_type):
    if content.startswith(b'\xff\xd8\xff'): return 'jpg', 'image/jpeg'
    if content.startswith(b'\x89PNG\r\n\x1a\n'): return 'png', 'image/png'
    if content.startswith((b'GIF87a', b'GIF89a')): return 'gif', 'image/gif'
    if content[:4] == b'RIFF' and content[8:12] == b'WEBP': return 'webp', 'image/webp'
    if content[4:8] == b'ftyp' and content[8:12] in (b'avif', b'avis'): return 'avif', 'image/avif'
    if content[:4] == b'wOF2': return 'woff2', 'font/woff2'
    if content[:4] == b'wOFF': return 'woff', 'font/woff'
    if content[:4] == b'OTTO': return 'otf', 'font/otf'
    if content[:4] == b'\x00\x01\x00\x00': return 'ttf', 'font/ttf'
    if 'image/svg+xml' in content_type and b'<svg' in content[:1024].lower(): return 'svg', 'image/svg+xml'
    raise ValueError('图片或字体格式不受支持')


class Builder:
    def __init__(self, folder, item_id, version, url, check, reader=None):
        self.folder, self.url, self.check = folder, url, check
        self.reader = reader or wechat._read_url
        self.prefix = f'/api/items/{item_id}/article-assets/{version}/'
        self.cache, self.files, self.failed, self.image_urls = {}, {}, {}, set()
        self.styles, self.failed_styles = {}, set()
        self.raw_styles = {}
        self.elements = []
        self.selectors = {}
        self.codepoints = set()
        self.font_families = None
        self.lock = threading.Lock()
        self.total = 0
        self.deadline = time.monotonic() + 180

    def check_budget(self):
        self.check()
        if time.monotonic() > self.deadline:
            raise ValueError('保存原文资源超时，可稍后刷新图文')

    def download(self, url, purpose):
        for attempt in range(2):
            self.check_budget()
            try:
                return self.reader(url, purpose, self.check_budget)
            except ValueError as exc:
                # Retry a transient transport failure once; never retry access controls.
                if attempt or not isinstance(exc.__cause__, wechat.httpx.TransportError): raise

    def resource(self, value, base=None):
        if value.startswith('#'):
            return value
        url, fragment = urldefrag(value) if value.startswith('data:') else urldefrag(urljoin(base or self.url, value))
        with self.lock:
            if url in self.cache:
                result = self.cache[url]
                return result + ('#' + quote(fragment, safe='') if result and fragment else '')
            self.cache[url] = ''
            allowed = len(self.cache) <= MAX_RESOURCES
        try:
            self.check_budget()
            if not allowed:
                raise ValueError('原文资源数量超过单篇保存上限')
            if url.startswith('data:'):
                match = re.fullmatch(r'data:((?:image|font|application)/[\w.+-]+)((?:;[^,]*)?),(.*)', url, re.S)
                if not match or len(match[3]) > 24 * 1024 * 1024:
                    raise ValueError('图片数据无效或过大')
                content = base64.b64decode(re.sub(rb'\s+', b'', unquote_to_bytes(match[3])), validate=True) if ';base64' in match[2].lower() else unquote_to_bytes(match[3])
                mime = match[1]
            else:
                content, mime = self.download(url, 'asset')
            extension, mime = binary_type(content, mime)
            with self.lock:
                if self.total + len(content) > MAX_TOTAL_BYTES:
                    raise ValueError('原文资源总大小超过单篇保存上限')
                self.total += len(content)
            filename = hashlib.sha256(url.encode()).hexdigest() + '.' + extension
            (self.folder / filename).write_bytes(content)
            with self.lock:
                self.files[filename] = {'mime': mime, 'url': url if not url.startswith('data:') else '内嵌图片', 'bytes': len(content)}
                self.cache[url] = self.prefix + filename
            return self.cache[url] + ('#' + quote(fragment, safe='') if fragment else '')
        except InterruptedError:
            raise
        except (ValueError, OSError) as exc:
            with self.lock:
                self.failed[url] = str(exc)
            return ''

    def css_tokens(self, tokens, base, fetch=True):
        result = []
        for token in tokens:
            if token.type == 'error':
                continue
            value = None
            if token.type == 'url':
                value = token.value
            elif token.type == 'function' and token.lower_name == 'url':
                values = [v for v in token.arguments if v.type not in ('whitespace', 'comment')]
                if len(values) == 1 and values[0].type == 'string':
                    value = values[0].value
            if value is not None:
                target = (self.resource(value, base) if fetch else '') or 'data:,'
                token = tinycss2.parse_component_value_list('url("' + target.replace('"', '%22') + '")')[0]
            else:
                for key in ('content', 'arguments'):
                    if getattr(token, key, None) is not None:
                        setattr(token, key, self.css_tokens(getattr(token, key), base, fetch))
            result.append(token)
        return result

    def selector_used(self, tokens):
        selector = tinycss2.serialize(tokens)
        if selector not in self.selectors:
            try:
                matcher = cssselect2.Matcher()
                for compiled in cssselect2.compile_selector_list(tokens):
                    # Interactive states may match when the user hovers/selects text.
                    if compiled.never_matches:
                        self.selectors[selector] = True
                        return True
                    matcher.add_selector(compiled, True)
                self.selectors[selector] = any(matcher.match(element) for element in self.elements)
            except cssselect2.SelectorError:
                self.selectors[selector] = True
        return self.selectors[selector]

    def css(self, source, base, depth=0):
        parts = []
        for rule in tinycss2.parse_stylesheet(source, skip_comments=True):
            if rule.type == 'error':
                continue
            if rule.type == 'at-rule' and rule.lower_at_keyword == 'font-face' and rule.content is not None:
                declarations = tinycss2.parse_declaration_list(rule.content, skip_comments=True, skip_whitespace=True)
                values = {d.lower_name: d for d in declarations if d.type == 'declaration'}
                family = tinycss2.serialize(values['font-family'].value).strip().strip('\"\'').casefold() if 'font-family' in values else ''
                if self.font_families is not None and family and family not in self.font_families:
                    continue
                ranges = [t for t in values['unicode-range'].value if t.type == 'unicode-range'] if 'unicode-range' in values else []
                if ranges and not any(t.start <= cp <= t.end for t in ranges for cp in self.codepoints):
                    continue
                # Electron supports WOFF2; don't download every fallback format of
                # the same font subset. Keep local() candidates and one web source.
                if 'src' in values:
                    groups = [[]]
                    for token in values['src'].value:
                        if token.type == 'literal' and token.value == ',': groups.append([])
                        else: groups[-1].append(token)
                    remote = [g for g in groups if any(t.type == 'url' or t.type == 'function' and t.lower_name == 'url' for t in g)]
                    if remote:
                        chosen = next((g for g in remote if 'woff2' in tinycss2.serialize(g).lower()), remote[0])
                        local = [g for g in groups if any(t.type == 'function' and t.lower_name == 'local' for t in g)]
                        values['src'].value = tinycss2.parse_component_value_list(','.join(tinycss2.serialize(g) for g in [*local, chosen]))
                rule.content = tinycss2.parse_component_value_list(tinycss2.serialize(declarations))
            if rule.type == 'at-rule' and rule.lower_at_keyword == 'import':
                values = [v for v in rule.prelude if v.type not in ('whitespace', 'comment')]
                if not values: continue
                first = values[0]
                value = first.value if first.type in ('url', 'string') else (
                    first.arguments[0].value if first.type == 'function' and first.lower_name == 'url' and
                    len(first.arguments) == 1 and first.arguments[0].type == 'string' else '')
                imported = self.stylesheet(urljoin(base, value), depth + 1) if value and depth < 4 else ''
                media = tinycss2.serialize(values[1:]).strip()
                parts.append(f'@media {media}{{{imported}}}' if media else imported)
                continue
            if rule.type == 'at-rule' and rule.content is not None and rule.lower_at_keyword in ('media', 'supports', 'layer', 'container', 'document', 'scope'):
                content = self.css(tinycss2.serialize(rule.content), base, depth)
                parts.append('@' + rule.at_keyword + tinycss2.serialize(rule.prelude) + '{' + content + '}')
                continue
            used = rule.type != 'qualified-rule' or self.selector_used(rule.prelude)
            for key in ('prelude', 'content'):
                if getattr(rule, key, None) is not None:
                    setattr(rule, key, self.css_tokens(getattr(rule, key), base, used))
            parts.append(rule.serialize())
        return ''.join(parts)

    def fetch_style(self, url):
        try:
            self.check_budget()
            self.raw_styles[url] = self.download(url, 'style')
        except InterruptedError:
            raise
        except (ValueError, OSError):
            self.raw_styles[url] = None

    def stylesheet(self, url, depth=0):
        if url in self.styles: return self.styles[url]
        self.styles[url] = ''
        try:
            self.check_budget()
            if len(self.styles) > MAX_STYLES: raise ValueError('样式文件过多')
            if url not in self.raw_styles: self.fetch_style(url)
            if self.raw_styles[url] is None: raise ValueError('样式文件获取失败')
            content, mime = self.raw_styles[url]
            if 'text/css' not in mime.lower(): raise ValueError('样式文件无效')
            self.styles[url] = self.css(content.decode('utf-8-sig'), url, depth)
        except InterruptedError:
            raise
        except (ValueError, OSError):
            self.failed_styles.add(url)
        return self.styles[url]

    def attrs(self, attrs):
        result = []
        for name, value in attrs.items():
            if value is None or name.startswith('on') or name in ('src', 'srcset', 'data-src', 'href', 'xlink:href', 'action', 'formaction', 'srcdoc', 'nonce', 'integrity', 'autofocus', 'is', 'loading', 'decoding', 'ping', 'background'):
                continue
            if not re.fullmatch(r'[a-zA-Z_:][\w:.-]*', name): continue
            if name == 'style': value = tinycss2.serialize(self.css_tokens(tinycss2.parse_component_value_list(value), self.url))
            result.append(f'{name}="{escape(value, quote=True)}"')
        return ' ' + ' '.join(result) if result else ''

    def markup(self, node, article):
        if isinstance(node, str): return escape(node)
        if node.tag in DROP or node.tag == 'style': return ''
        if node.tag not in ALLOWED_TAGS:
            return ''.join(self.markup(child, article) for child in node.children)
        attrs = self.attrs(node.attrs)
        if node.tag == 'img':
            original = node.attrs.get('data-src') or node.attrs.get('src') or ''
            source = self.resource(original) if original else ''
            if not source:
                return '<span class="lulu-image-missing">原图暂未保存，可刷新图文或查看在线原页</span>'
            return f'<img{attrs} src="{escape(source, quote=True)}" loading="eager" decoding="async">'
        href = node.attrs.get('href') or node.attrs.get('xlink:href') or ''
        if href.startswith('#'): attrs += f' href="{escape(href, quote=True)}"'
        if node.tag in wechat.ArticleParser.void: return f'<{node.tag}{attrs}>'
        body = ''.join(self.markup(child, article) for child in node.children)
        if node.attrs.get('id') == 'publish_time' and not body.strip(): body = escape(article['published_at'])
        return f'<{node.tag}{attrs}>{body}</{node.tag}>'


def create(html, item_id, article, url, check, reader=None):
    from . import store
    parser = PageParser()
    parser.feed(html)
    parser.close()
    target_id = 'lulu-article-root' if 'lulu-article-root' in parser.roots else 'img-content' if 'img-content' in parser.roots else 'js_content'
    root = parser.roots.get(target_id)
    if not root: raise ValueError('原文没有可保存的正文结构')
    version = uuid.uuid4().hex
    parent = store.DATA / 'articles' / item_id
    parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='building-', dir=parent) as temporary:
        builder = Builder(Path(temporary), item_id, version, url, check, reader)
        def tree(node):
            element = Element(node.tag, {key: value or '' for key, value in node.attrs.items()})
            for child in node.children:
                if isinstance(child, wechat.Node): element.append(tree(child))
                else: element.text = (element.text or '') + child
            return element
        document = tree(root)
        for tag, attrs in reversed(parser.ancestors.get(target_id, [])):
            if tag in ('div', 'section', 'main', 'article'):
                ancestor = Element(tag, {key: value or '' for key, value in attrs.items()})
                ancestor.append(document)
                document = ancestor
        body_tree = Element('body', {key: value or '' for key, value in parser.body_attrs.items()})
        body_tree.append(document)
        html_tree = Element('html', {key: value or '' for key, value in parser.html_attrs.items()})
        html_tree.append(body_tree)
        builder.elements = list(cssselect2.ElementWrapper.from_html_root(html_tree).iter_subtree())
        builder.codepoints = {ord(char) for element in document.iter() for char in (element.text or '')}
        if target_id == 'lulu-article-root':
            # Browser snapshots include each element's computed font-family.
            builder.font_families = set()
            for element in document.iter():
                for declaration in tinycss2.parse_declaration_list(element.get('style', ''), skip_comments=True, skip_whitespace=True):
                    if declaration.type == 'declaration' and declaration.lower_name == 'font-family':
                        builder.font_families.update(part.strip().strip('\"\'').casefold() for part in tinycss2.serialize(declaration.value).split(','))
        def images(node):
            if not isinstance(node, wechat.Node): return
            if node.tag == 'img':
                value = node.attrs.get('data-src') or node.attrs.get('src')
                if value: builder.image_urls.add(urldefrag(urljoin(url, value))[0])
            for child in node.children: images(child)
        images(root)
        # Prioritize actual article images before decorative stylesheet resources.
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(builder.resource, sorted(builder.image_urls)))
            urls = list(dict.fromkeys(urljoin(url, value) for kind, value, _ in parser.styles if kind == 'link'))[:MAX_STYLES]
            list(pool.map(builder.fetch_style, urls))
        styles = []
        for kind, value, media in parser.styles:
            css = builder.stylesheet(urljoin(url, value)) if kind == 'link' else builder.css(value, url)
            styles.append(f'@media {media}{{{css}}}' if media else css)
        body = builder.markup(root, article)
        for tag, attrs in reversed(parser.ancestors.get(target_id, [])):
            if tag in ('div', 'section', 'main', 'article'):
                body = f'<{tag}{builder.attrs(attrs)}>{body}</{tag}>'
        if target_id == 'js_content':
            body = f'<header><h1>{escape(article["title"])}</h1><p>{escape(article["account"])} {escape(article["author"])}</p></header>' + body
        styles.append('#js_content{visibility:visible!important;opacity:1!important}.lulu-image-missing{display:block;padding:24px;color:#666;background:#eee}')
        css = '\n'.join(styles).replace('<', '\\00003c')
        output = f'<!doctype html><html{builder.attrs(parser.html_attrs)}><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">' + f'<title>{escape(article["title"])}</title><style>{css}</style></head><body{builder.attrs(parser.body_attrs)}>{body}</body></html>'
        saved = sum(bool(builder.cache.get(value)) for value in builder.image_urls)
        info = {'version': version, 'image_count': len(builder.image_urls), 'saved_images': saved,
                'missing_images': len(builder.image_urls) - saved, 'missing_styles': len(builder.failed_styles),
                'missing_resources': len(builder.failed), 'saved_at': time.time()}
        manifest = info | {'files': builder.files, 'source_url': url,
                           'images': [builder.cache.get(value, '') for value in sorted(builder.image_urls)],
                           'failures': [reason for reason in builder.failed.values()],
                           'failed_resources': [{'url': value if not value.startswith('data:') else '内嵌资源', 'reason': reason} for value, reason in builder.failed.items()]}
        check()
        (Path(temporary) / 'index.html').write_text(output, encoding='utf-8')
        (Path(temporary) / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False), encoding='utf-8')
        # Windows indexers/scanners can briefly hold newly written image files.
        # Publish only a complete snapshot, with a bounded retry for that lock.
        for attempt in range(6):
            check()
            try:
                Path(temporary).rename(parent / version)
                break
            except PermissionError:
                if attempt == 5:
                    raise ValueError('原文文件正被其他程序占用，请稍后重试；已有图文和文稿已保留')
                time.sleep(0.1 * (attempt + 1))
    return info


def location(item, version=None):
    from . import store
    version = version or item['metadata'].get('article_view', {}).get('version', '')
    if not re.fullmatch('[a-f0-9]{32}', item['id']) or not re.fullmatch('[a-f0-9]{32}', version): raise ValueError('尚未保存原文图文，请先加载原文')
    return store.DATA / 'articles' / item['id'] / version


def manifest(item, version=None):
    path = location(item, version) / 'manifest.json'
    if not path.is_file(): raise ValueError('原文图文文件缺失，请刷新图文')
    return json.loads(path.read_text(encoding='utf-8'))
