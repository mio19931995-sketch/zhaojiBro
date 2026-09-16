"""One-at-a-time local media pipeline with resumable jobs and genuine progress."""
import json
import queue
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

from . import store

MODELS = {
    'tiny': {'name': 'Whisper Tiny', 'size': '约 75 MB', 'note': '快速试用，适合短音频'},
    'base': {'name': 'Whisper Base', 'size': '约 145 MB', 'note': '轻量本地转录，适合这台电脑'},
    'small': {'name': 'Whisper Small', 'size': '约 485 MB', 'note': '更好的中文识别，CPU 用时更长'},
    'large-v3-turbo': {'name': 'Whisper Large v3 Turbo', 'size': '约 1.6 GB', 'note': '高质量转录，建议独立显卡'},
}
model_state = {}
model_lock = threading.Lock()
jobs = queue.Queue()
pending = set()
cancelled = set()
job_lock = threading.RLock()
worker_started = False
thumbnail_lock = threading.Lock()


def run_command(args, **kwargs):
    return subprocess.run(args, capture_output=True, text=True, encoding='utf-8', errors='replace',
                          creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), **kwargs)


def media_info(path):
    probe = shutil.which('ffprobe')
    if not probe:
        return 0
    try:
        result = run_command([probe, '-v', 'error', '-show_entries', 'format=duration', '-of', 'json', str(path)], timeout=30)
        return float(json.loads(result.stdout).get('format', {}).get('duration', 0))
    except (ValueError, subprocess.TimeoutExpired):
        return 0


def model_dir(name):
    if name not in MODELS:
        raise ValueError('不支持的模型')
    return store.DATA / 'models' / name


def installed(name):
    p = model_dir(name)
    return (p / 'model.bin').is_file() and (p / 'config.json').is_file() and (p / '.ready').is_file()


def all_models():
    return [dict(id=k, **v, installed=installed(k), **model_state.get(k, {'state': 'idle', 'message': ''})) for k, v in MODELS.items()]


def install_model(name):
    model_dir(name)
    with model_lock:
        if model_state.get(name, {}).get('state') == 'downloading':
            return
        model_state[name] = {'state': 'downloading', 'message': '正在下载并校验模型文件…'}

    def download():
        try:
            from faster_whisper.utils import download_model
            path = download_model(name, output_dir=str(model_dir(name)))
            # Actually load weights before marking a download usable.
            from faster_whisper import WhisperModel
            model = WhisperModel(path, device='cpu', compute_type='int8', cpu_threads=2)
            del model
            (model_dir(name) / '.ready').write_text('verified', encoding='utf-8')
            model_state[name] = {'state': 'ready', 'message': '已校验，可以离线使用'}
        except Exception as exc:
            model_state[name] = {'state': 'error', 'message': str(exc)[:600]}
    threading.Thread(target=download, daemon=True).start()


def check_cancel(item_id):
    if item_id in cancelled:
        raise InterruptedError('已暂停，可继续处理')


def enqueue(item_id):
    with job_lock:
        item = store.get(item_id)
        if not item:
            raise ValueError('找不到任务')
        if item_id in pending:
            if item_id in cancelled:
                raise ValueError('正在结束当前处理段，请等待显示「已暂停，可以继续」后再开始')
            return
        pending.add(item_id)
        cancelled.discard(item_id)
        store.update(item_id, status='queued', phase='已加入队列', progress=0, error='')
        jobs.put(item_id)


def pause(item_id):
    with job_lock:
        cancelled.add(item_id)
        store.update(item_id, status='paused', phase='正在暂停，等待当前处理段结束')


def yt_options():
    cfg = store.settings()
    opts = {'quiet': True, 'no_warnings': True, 'socket_timeout': 25, 'retries': 2,
            'noplaylist': True, 'restrictfilenames': True}
    if cfg.get('cookies_path') and Path(cfg['cookies_path']).is_file():
        opts['cookiefile'] = cfg['cookies_path']
    return opts


def validate_url(url):
    value = urlparse(url)
    if value.scheme not in ('https', 'http') or not value.hostname or value.username or value.password:
        raise ValueError('请输入有效的 http 或 https 链接')
    # Remote collection does not need file URLs or local-network services.
    import ipaddress
    import socket
    try:
        for answer in socket.getaddrinfo(value.hostname, value.port or 443):
            if not ipaddress.ip_address(answer[4][0]).is_global:
                raise ValueError('采集链接不能指向本机或内网地址')
    except socket.gaierror as exc:
        raise ValueError('无法解析链接域名，请检查网络') from exc
    return url


def collect_preview(url, limit=20, after='', before=''):
    import yt_dlp
    validate_url(url)
    opts = yt_options() | {'extract_flat': 'in_playlist', 'noplaylist': False, 'playlistend': limit, 'skip_download': True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    entries = list(info.get('entries') or [info])
    result = []
    for i, entry in enumerate(entries[:limit]):
        if not entry:
            continue
        date = entry.get('upload_date') or ''
        if date and ((after and date < after.replace('-', '')) or (before and date > before.replace('-', ''))):
            continue
        source = entry.get('webpage_url') or entry.get('url') or url
        if not source.startswith(('https://', 'http://')) and entry.get('ie_key') == 'Youtube':
            source = 'https://www.youtube.com/watch?v=' + entry['id']
        result.append({'key': entry.get('id', str(i)), 'title': entry.get('title') or '未命名作品',
                       'url': source, 'author': entry.get('uploader') or info.get('uploader') or '',
                       'duration': entry.get('duration') or 0, 'date': date,
                       'views': entry.get('view_count'), 'likes': entry.get('like_count')})
    return {'title': info.get('title') or '主页采集', 'entries': result}


MEDIA_SUFFIXES = {'.mp3','.wav','.m4a','.mp4','.mov','.mkv','.webm','.ogg','.flac','.aac','.wma','.avi','.m4v'}
VIDEO_SUFFIXES = {'.mp4','.mov','.mkv','.webm','.avi','.m4v'}


def ensure_video_thumbnail(item):
    """Return a local cover, extracting one video frame when the source has no cover."""
    existing = item['metadata'].get('cover_path','')
    if existing and Path(existing).is_file():
        return existing
    media = Path(item['media_path']) if item.get('media_path') else None
    ffmpeg = shutil.which('ffmpeg')
    if not media or media.suffix.lower() not in VIDEO_SUFFIXES or not media.is_file() or not ffmpeg:
        return ''
    directory = store.DATA / 'thumbnails'
    target = directory / f"{item['id']}.jpg"
    with thumbnail_lock:
        if target.is_file():
            return str(target)
        directory.mkdir(parents=True, exist_ok=True)
        temporary = directory / f"{item['id']}.tmp.jpg"
        seek = min(2.0, max(0.15, float(item.get('duration') or 5) * 0.08))
        for position in (seek, 0):
            result = run_command([ffmpeg, '-y', '-hide_banner', '-loglevel', 'error', '-ss', str(position), '-i', str(media),
                                  '-frames:v', '1', '-vf', 'scale=480:-2', '-q:v', '3', str(temporary)], timeout=45)
            if result.returncode == 0 and temporary.is_file() and temporary.stat().st_size:
                temporary.replace(target)
                metadata = item['metadata'] | {'cover_path': str(target), 'cover_generated': True}
                store.update(item['id'], metadata=metadata)
                return str(target)
            temporary.unlink(missing_ok=True)
    return ''


def process(item_id):
    check_cancel(item_id)
    item = store.get(item_id)
    if not item:
        return
    store.update(item_id, status='processing', phase='正在准备', error='')
    from . import assets
    subtitles = False
    if item['source_url']:
        item, subtitles = assets.acquire(item)
    check_cancel(item_id)
    if item['metadata'].get('download_only') or item['metadata'].get('action') == 'assets':
        store.update(item_id, status='done', progress=100, phase='采集完成')
        return
    if subtitles:
        path = store.DATA / 'documents' / f'{item_id}.txt'
        path.write_text(item['transcript'], encoding='utf-8')
        store.update(item_id, status='done', progress=100, phase='已提取平台字幕')
        assets.finish(item_id)
        return
    if item['kind'] == 'document':
        store.update(item_id, status='done', progress=100, phase='文稿已保存')
        return
    cfg = store.settings()
    name = item['metadata'].get('model') or cfg['model']
    if not installed(name):
        raise ValueError(f'请先在 AI 大模型页面安装 {MODELS.get(name, {}).get("name", name)}，然后重试')
    from faster_whisper import WhisperModel
    store.update(item_id, phase='正在加载本地模型', progress=28)
    model = WhisperModel(str(model_dir(name)), device='cpu', compute_type='int8', cpu_threads=4)
    check_cancel(item_id)
    store.update(item_id, phase='正在识别语音', progress=30)
    segments, info = model.transcribe(item['media_path'], language=None if cfg['language'] == 'auto' else cfg['language'],
                                     beam_size=5, vad_filter=True, condition_on_previous_text=False)
    result = []
    duration = info.duration or item['duration'] or 1
    for s in segments:
        check_cancel(item_id)
        result.append({'start': round(s.start, 3), 'end': round(s.end, 3), 'text': s.text.strip()})
        store.update(item_id, segments=result, transcript='\n'.join(p['text'] for p in result),
                     progress=min(98, 30 + 68 * s.end / duration), phase=f'正在转录 · {s.end:.0f} / {duration:.0f} 秒')
    check_cancel(item_id)
    transcript = '\n'.join(s['text'] for s in result)
    path = store.DATA / 'documents' / f'{item_id}.txt'
    path.write_text(transcript, encoding='utf-8')
    if path.read_text(encoding='utf-8') != transcript:
        raise RuntimeError('文稿保存校验失败')
    store.update(item_id, transcript=transcript, segments=result, duration=duration,
                 status='done', progress=100, phase='转录完成' if result else '处理完成，未检测到语音')
    assets.finish(item_id)
    del model


def start_worker():
    global worker_started
    if worker_started:
        return
    worker_started = True
    def loop():
        while True:
            item_id = jobs.get()
            try:
                process(item_id)
            except InterruptedError:
                store.update(item_id, status='paused', phase='已暂停，可以继续')
            except Exception as exc:
                if item_id in cancelled:
                    store.update(item_id, status='paused', phase='已暂停，可以继续')
                else:
                    store.update(item_id, status='error', phase='处理失败', error=str(exc)[:1200])
            finally:
                with job_lock:
                    pending.discard(item_id)
                jobs.task_done()
    threading.Thread(target=loop, daemon=True).start()


def stamp(seconds, srt=False):
    ms = round(max(0, seconds) * 1000)
    hh, rest = divmod(ms, 3600000)
    mm, rest = divmod(rest, 60000)
    ss, millis = divmod(rest, 1000)
    return f'{hh:02}:{mm:02}:{ss:02},{millis:03}' if srt else f'{hh:02}:{mm:02}:{ss:02}'


def export_content(item, fmt):
    if fmt == 'srt':
        if not item['segments']:
            raise ValueError('这份文稿没有时间戳，请导出 TXT 或 Markdown')
        return '\n\n'.join(f'{i+1}\n{stamp(s["start"], True)} --> {stamp(s["end"], True)}\n{s["text"]}' for i, s in enumerate(item['segments'])) + '\n'
    if fmt == 'md':
        source = f'\n来源：{item["source_url"]}\n' if item['source_url'] else ''
        body = '\n\n'.join(f'[{stamp(s["start"])}] {s["text"]}' for s in item['segments']) if item['segments'] else item['transcript']
        return f'# {item["title"]}\n{source}\n{body}\n'
    return item['transcript']


def parse_srt(content):
    pattern = re.compile(r'(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*\n(.*?)(?=\n\s*\n|\Z)', re.S)
    result = []
    for m in pattern.finditer(content.replace('\r\n', '\n')):
        nums = [int(v) for v in m.groups()[:8]]
        start = nums[0]*3600+nums[1]*60+nums[2]+nums[3]/1000
        end = nums[4]*3600+nums[5]*60+nums[6]+nums[7]/1000
        if end >= start:
            result.append({'start': start, 'end': end, 'text': m.group(9).strip()})
    return result
