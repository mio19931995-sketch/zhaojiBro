"""Acquisition options shared by single-link and collection jobs."""
import html
import json
import re
import shutil
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx
from . import store


def options(item):
    cfg = store.settings()
    result = {'mode': cfg['download_mode'], 'keep': cfg['keep_media'] == 'true',
              'cover': cfg['download_cover'] == 'true', 'subtitles': cfg['prefer_subtitles'] == 'true',
              'directory': cfg['save_directory'], 'media': True}
    result.update(item['metadata'].get('options', {}))
    if item['metadata'].get('download_only') or item['metadata'].get('action') == 'assets':
        result['keep'] = True
    return result


def validate_options(value):
    if not isinstance(value, dict) or set(value) - {'mode','keep','cover','subtitles','directory','media'}:
        raise ValueError('无效的下载选项')
    if value.get('mode', 'audio') not in ('audio','video'):
        raise ValueError('请选择仅音频或音视频')
    for k in ('keep','cover','subtitles','media'):
        if k in value and not isinstance(value[k], bool):
            raise ValueError('无效的下载开关')
    directory = value.get('directory', '')
    if not isinstance(directory, str) or (directory and not Path(directory).is_dir()):
        raise ValueError('保存文件夹不存在，请重新选择')
    return value


def safe_name(title):
    return re.sub(r'[\x00-\x1f\\/:*?"<>|]', '_', title).strip(' .')[:70] or '素材'


def save_copy(path, item, opts, category):
    if not opts.get('directory'):
        return str(path)
    destination = Path(opts['directory']) / f'{safe_name(item["title"])}-{item["id"][:8]}-{category}{path.suffix}'
    if path.resolve() != destination.resolve():
        shutil.copy2(path, destination)
    return str(destination)


def read_subtitle(path):
    from . import engine
    text = path.read_text(encoding='utf-8-sig')
    if path.suffix == '.json3':
        data = json.loads(text)
        return [{'start': e['tStartMs']/1000, 'end': (e['tStartMs']+e.get('dDurationMs', 0))/1000,
                 'text': ''.join(s.get('utf8','') for s in e.get('segs',[])).strip()}
                for e in data.get('events',[]) if e.get('segs') and ''.join(s.get('utf8','') for s in e['segs']).strip()]
    # VTT can omit the hours component. Normalize it before using the SRT parser.
    text = re.sub(r'(?m)^(\d{2}:\d{2}[.,]\d{3})(\s*-->\s*)(\d{2}:\d{2}[.,]\d{3})', r'00:\1\g<2>00:\3', text)
    text = re.sub(r'(--> [\d:.,]+)[^\n]*', r'\1', text)
    segments = engine.parse_srt(text)
    for s in segments:
        s['text'] = html.unescape(re.sub('<[^>]+>', '', s['text']))
    return segments


def direct_download(url, path, item_id, label):
    from . import engine
    for attempt in range(3):
        engine.check_cancel(item_id)
        try:
            return _direct_download_once(url, path, item_id, label)
        except httpx.TransportError:
            if attempt == 2:
                raise ValueError('素材连接中断，重试后仍未恢复。请检查网络并重新下载。') from None
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code not in (500,502,503,504) or attempt == 2:
                raise
        store.update(item_id, phase=f'{label} · 连接中断，正在重试 {attempt+1}/2')
        time.sleep(attempt+1)


def _direct_download_once(url, path, item_id, label):
    from . import engine
    engine.validate_url(url)
    # This session only contains cookies explicitly imported or obtained in Lulu's login window.
    import http.cookiejar
    jar = http.cookiejar.MozillaCookieJar()
    cookie_path = store.settings().get('cookies_path')
    if cookie_path and Path(cookie_path).is_file():
        jar.load(cookie_path, ignore_discard=True, ignore_expires=False)
    partial = path.with_suffix(path.suffix + '.part')
    with httpx.Client(timeout=60, cookies=jar, follow_redirects=False, headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://www.douyin.com/'}) as client:
        for _ in range(8):
            with client.stream('GET', url) as response:
                if response.is_redirect:
                    url = str(response.url.join(response.headers['location']))
                    engine.validate_url(url)
                    continue
                response.raise_for_status()
                total = int(response.headers.get('content-length',0)); done = 0
                with partial.open('wb') as out:
                    for chunk in response.iter_bytes(1024*512):
                        engine.check_cancel(item_id); out.write(chunk); done += len(chunk)
                        store.update(item_id, phase=label, progress=min(25,25*done/total) if total else 0)
                partial.replace(path)
                return
    raise ValueError('素材链接重定向过多，请重新采集')


def acquire(item):
    from . import engine
    import yt_dlp
    opts = options(item); validate_options(opts)
    item_id = item['id']; metadata = dict(item['metadata'])
    directory = store.DATA / 'media' / item_id
    directory.mkdir(exist_ok=True)
    asset_only = metadata.get('action') == 'assets' or metadata.get('download_only')
    need_media = opts['media'] if asset_only else True
    path = Path(item['media_path']) if item['media_path'] else None
    if path and not path.is_file(): path = None
    if path and opts['mode']=='video' and path.suffix.lower() in ('.mp3','.m4a','.wav','.flac','.ogg','.aac'):
        path = None
    subtitle_segments = []
    if metadata.get('provider') == 'douyin' and metadata.get('direct_url'):
        if need_media and not path:
            path = directory / 'source.mp4'
            direct_download(metadata['direct_url'], path, item_id, '正在下载作品')
        if opts['cover'] and metadata.get('cover_url'):
            cover = directory / 'cover.jpg'
            direct_download(metadata['cover_url'], cover, item_id, '正在下载封面')
            metadata['cover_path'] = save_copy(cover, item, opts, '封面')
    else:
        engine.validate_url(item['source_url'])
        def hook(info):
            engine.check_cancel(item_id)
            total = info.get('total_bytes') or info.get('total_bytes_estimate') or 0
            store.update(item_id, phase='正在下载素材', progress=min(25,25*info.get('downloaded_bytes',0)/total) if total else 0)
        common = engine.yt_options() | {'outtmpl': str(directory / 'source.%(ext)s'),
            'format': 'bestaudio/best' if opts['mode']=='audio' else 'bestvideo*+bestaudio/best',
            'merge_output_format':'mp4', 'progress_hooks':[hook], 'writethumbnail':opts['cover']}
        # Subtitle downloads precede media. A usable subtitle can avoid speech recognition entirely.
        if opts['subtitles'] and not asset_only:
            info = {}
            try:
                with yt_dlp.YoutubeDL(common | {'skip_download':True,'writesubtitles':True,'writeautomaticsub':True,
                        'subtitleslangs':['zh.*','ai-zh.*','en.*'], 'subtitlesformat':'srt/vtt/json3/best'}) as ydl:
                    info = ydl.extract_info(item['source_url'], download=True)
            except yt_dlp.utils.DownloadError:
                metadata['subtitle_warning'] = '字幕获取未成功，已改为下载媒体并识别语音'
            for sub in sorted(directory.iterdir(), key=lambda p: ('zh' not in p.name,p.name)):
                if sub.suffix in ('.srt','.vtt','.json3'):
                    try:
                        subtitle_segments = read_subtitle(sub)
                    except (ValueError, UnicodeError, KeyError, TypeError):
                        metadata['subtitle_warning'] = '字幕文件无法读取，已改为下载媒体并识别语音'
                        continue
                    if subtitle_segments: break
        if need_media and not path and not (subtitle_segments and not opts['keep']):
            with yt_dlp.YoutubeDL(common) as ydl:
                info = ydl.extract_info(item['source_url'], download=True)
                proposed = Path(ydl.prepare_filename(info))
            path = proposed if proposed.is_file() else next((p for p in directory.iterdir() if p.suffix.lower() in engine.MEDIA_SUFFIXES), None)
            if not path: raise ValueError('未获取到音视频文件')
        elif not subtitle_segments:
            with yt_dlp.YoutubeDL(common | {'skip_download':True}) as ydl:
                info = ydl.extract_info(item['source_url'], download=True)
        item['title'] = info.get('title') or item['title']
        item['duration'] = info.get('duration') or item['duration']
        formats = info.get('requested_downloads') or info.get('requested_formats') or []
        metadata.update({'author': info.get('uploader') or metadata.get('author',''),
            'date': info.get('upload_date') or metadata.get('date',''), 'likes':info.get('like_count'),
            'views':info.get('view_count'), 'comments':info.get('comment_count'),
            'direct_url': info.get('url') or (formats[0].get('url','') if formats else ''), 'cover_url':info.get('thumbnail','')})
        if opts['cover']:
            cover = next((p for p in directory.iterdir() if p.suffix.lower() in ('.jpg','.jpeg','.webp','.png')),None)
            if cover: metadata['cover_path'] = save_copy(cover,item,opts,'封面')
            else: metadata['cover_warning'] = '平台没有提供可下载封面'
    if path and need_media:
        if opts['mode']=='audio' and path.suffix.lower() in ('.mp4','.webm','.mkv','.mov'):
            audio = directory / 'audio.m4a'
            result = engine.run_command([shutil.which('ffmpeg') or 'ffmpeg','-y','-hide_banner','-loglevel','error','-i',str(path),'-vn','-c:a','aac',str(audio)],timeout=900)
            if result.returncode: raise ValueError('提取音频失败：'+result.stderr[-300:])
            path = audio
        metadata['temporary_media'] = not opts['keep']
        if opts['keep']: path = Path(save_copy(path,item,opts,'音频' if opts['mode']=='audio' else '视频'))
    if subtitle_segments:
        metadata['transcript_source'] = '平台字幕'
        store.update(item_id, transcript='\n'.join(s['text'] for s in subtitle_segments), segments=subtitle_segments)
    store.update(item_id,title=item['title'],duration=item['duration'] or (engine.media_info(path) if path else 0),
                 media_path=str(path) if path else '',metadata=metadata,progress=25)
    return store.get(item_id), bool(subtitle_segments)


def finish(item_id):
    """Remove only Lulu-owned download cache after a verified transcript, never a local import."""
    item = store.get(item_id)
    if not item or not item['source_url'] or not item['transcript'] or not item['metadata'].get('temporary_media'):
        return
    owned = (store.DATA / 'media' / item_id).resolve()
    path = Path(item['media_path']).resolve() if item['media_path'] else None
    if path and path.parent == owned and path.is_file():
        path.unlink()
    elif path and path.parent != owned:
        return
    # Original video used to extract a temporary audio is also an owned download.
    for candidate in owned.glob('source.*'):
        if candidate.suffix.lower() in {'.mp3','.wav','.m4a','.mp4','.webm','.mkv','.mov'} and candidate.resolve().parent == owned:
            candidate.unlink(missing_ok=True)
    store.update(item_id, media_path='')
