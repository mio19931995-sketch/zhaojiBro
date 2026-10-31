"""Windows GGUF ASR adapters. Model weights are NEVER fetched by this module.

The MOSS completeness checks follow Lulu v4.3.1's native adapter (MIT).
Windows uses transcribe.cpp v0.4.0 CPU, not the Apple-only MLX/Metal runtime.
"""
import hashlib
import importlib.metadata
import json
import math
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import wave
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from . import store

VERSION = '0.4.0'
RUNTIME_URL = ('https://github.com/handy-computer/transcribe.cpp/releases/download/v0.4.0/'
               'transcribe-native-0.4.0-windows-x86_64-cpu-vulkan.tar.gz')
RUNTIME_SHA256 = '9d5bd89f720d7979323e73f5220fe50fad5e38a7b0c2525225d453bd09adf2b0'
NATIVE_MODELS = {
    'sensevoice-q8': dict(name='SenseVoice Small · Q8', size='253 MB',
        note='中文、英语、粤语、日语、韩语；轻量 CPU 转录，无精确时间轴。长音频按 25 秒分块。',
        file='SenseVoiceSmall-Q8_0.gguf', bytes=252684608,
        sha256='6c759ee4c9748c9b3f7a5a60ca74f0f7e685fb9d45d1378fce7cfd62f59adf29',
        repo='handy-computer/SenseVoiceSmall-gguf', timestamps=False, speakers=False),
    'whisper-turbo-q4': dict(name='Whisper Turbo · Q4_K_M', size='536 MB',
        note='Windows GGUF 量化版本，支持时间轴；与 Mac MLX 4bit 权重不兼容。',
        file='whisper-large-v3-turbo-Q4_K_M.gguf', bytes=536069696,
        sha256='c6e172d0074f0fa9440b88247854b50643cd4bb58b8515c797bbed16d858c7df',
        repo='handy-computer/whisper-large-v3-turbo-gguf', timestamps=True, speakers=False),
    'moss-q5': dict(name='MOSS · Q5 · 区分发言人', size='700 MB',
        note='中文、英语，输出时间轴和匿名发言人；CPU 运行耗时较长，低内存电脑请选轻量模型。',
        file='MOSS-Transcribe-Diarize-Q5_K_M.gguf', bytes=700314304,
        sha256='cc36d8ad46c66a55125cd9339dc063e62c657d63de60d571442d19aea87e7349',
        repo='handy-computer/moss-transcribe-diarize-gguf', timestamps=True, speakers=True),
}
LEGACY = ('tiny', 'base', 'small', 'large-v3-turbo')
runtime_state = {'state': 'idle', 'message': ''}
runtime_lock = threading.Lock()
runtime_cancel = threading.Event()
_owned_condition = threading.Condition()
_owned_processes = {}
_stopping = threading.Event()
_shutdown_deadline = None
router = APIRouter(prefix='/api/asr')


def supported_platform():
    return os.name == 'nt' and platform.machine().lower() in ('amd64', 'x86_64')


def fingerprint(path):
    stat = Path(path).stat()
    return {'size': stat.st_size, 'mtime_ns': stat.st_mtime_ns}


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def binding_file(name):
    if name not in (*NATIVE_MODELS, *LEGACY):
        raise ValueError('不支持的本地模型')
    return store.DATA / 'models' / name / '.external.json'


def read_json(path):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
    temporary.replace(path)


def checked_binding(name):
    info = read_json(binding_file(name))
    try:
        if info and all(fingerprint(p) == value for p, value in info['files'].items()):
            return info
    except (OSError, KeyError, TypeError):
        pass
    return None


def external_path(name):
    info = checked_binding(name)
    return Path(info['path']) if info else None


def runtime_root():
    return store.DATA / 'runtimes' / ('transcribe-' + VERSION)


def runtime_status():
    info = read_json(runtime_root() / 'binding.json')
    ready = False
    if supported_platform() and info:
        try:
            ready = bool(info['files']) and all(fingerprint(p) == value for p, value in info['files'].items())
        except (OSError, KeyError, TypeError):
            pass
    return dict(runtime_state, ready=ready, version=VERSION,
                library=info.get('library', '') if ready else '',
                supported=supported_platform(), size='18 MB',
                reason='' if supported_platform() else '此原生运行工具仅支持 Windows x64；可继续使用原有 Whisper 模型。')


def discover_runtime():
    try:
        distribution = importlib.metadata.distribution('transcribe-cpp-native')
        for file in distribution.files or []:
            if file.name == 'transcribe.dll':
                path = Path(distribution.locate_file(file)).resolve()
                if path.is_file():
                    return str(path.parent)
    except importlib.metadata.PackageNotFoundError:
        pass
    return ''


def _stop_owned(record, deadline):
    """Stop only our process tree, sharing one wall-clock budget with shutdown."""
    proc, lock = record
    if not lock.acquire(timeout=max(0, deadline - time.monotonic())):
        return
    try:
        if proc.poll() is not None:
            return
        remaining = deadline - time.monotonic()
        # Windows venv python.exe can launch a second Python process. Killing just
        # its launcher leaves native inference and inherited output handles alive.
        if os.name == 'nt' and remaining > 0:
            try:
                subprocess.run(['taskkill', '/PID', str(proc.pid), '/T', '/F'],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               creationflags=subprocess.CREATE_NO_WINDOW, timeout=min(2, remaining))
            except (OSError, subprocess.TimeoutExpired):
                pass
        if proc.poll() is None:
            if os.name != 'nt':
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            else:
                proc.kill()
        remaining = deadline - time.monotonic()
        if remaining > 0:
            try:
                proc.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                pass
    finally:
        lock.release()


def shutdown(timeout=8):
    """Block new native processes and stop existing ones within a shared budget."""
    global _shutdown_deadline
    deadline = time.monotonic() + max(0, min(float(timeout), 8))
    runtime_cancel.set()
    with _owned_condition:
        _shutdown_deadline = deadline
        _stopping.set()
        records = list(_owned_processes.values())
    for record in records:
        _stop_owned(record, deadline)
    with _owned_condition:
        # Let inference threads unwind/close handles, but never wait past budget.
        while _owned_processes and time.monotonic() < deadline:
            _owned_condition.wait(timeout=max(0, deadline - time.monotonic()))
        return {'remaining_processes': sum(record[0].poll() is None for record in _owned_processes.values())}


def reset_shutdown():
    """Called by application startup; permits restarting a test/app lifespan."""
    global _shutdown_deadline
    with _owned_condition:
        if any(record[0].poll() is None for record in _owned_processes.values()):
            raise RuntimeError('上一次本地转录进程尚未退出')
        _owned_processes.clear()
        _shutdown_deadline = None
        _stopping.clear()


def run_process(args, check_cancel=lambda: None, timeout=7200):
    """Owned subprocess with bounded output, cancellation and no shell."""
    with tempfile.TemporaryDirectory(prefix='lulu-asr-process-') as work:
        out, err = Path(work) / 'out', Path(work) / 'err'
        with out.open('wb') as stdout, err.open('wb') as stderr:
            # Atomic with shutdown's snapshot: no native child can start after it.
            with _owned_condition:
                if _stopping.is_set():
                    raise InterruptedError('应用正在退出，本地转录已停止')
                proc = subprocess.Popen([str(arg) for arg in args], stdout=stdout, stderr=stderr,
                                        stdin=subprocess.DEVNULL,
                                        start_new_session=os.name != 'nt',
                                        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                record = (proc, threading.Lock())
                _owned_processes[proc.pid] = record
            deadline = time.monotonic() + timeout
            try:
                while proc.poll() is None:
                    if _stopping.is_set():
                        raise InterruptedError('应用正在退出，本地转录已停止')
                    check_cancel()
                    if time.monotonic() >= deadline:
                        raise ValueError('本地转录超时，原始音视频已保留')
                    if out.stat().st_size > 32 * 1024 * 1024 or err.stat().st_size > 4 * 1024 * 1024:
                        raise ValueError('运行工具输出异常，已停止本次处理')
                    time.sleep(.1)
                if _stopping.is_set():
                    raise InterruptedError('应用正在退出，本地转录已停止')
                check_cancel()
            finally:
                try:
                    _stop_owned(record, _shutdown_deadline if _stopping.is_set() and _shutdown_deadline is not None
                                else time.monotonic() + 8)
                finally:
                    with _owned_condition:
                        if proc.poll() is not None:
                            _owned_processes.pop(proc.pid, None)
                        _owned_condition.notify_all()
        if out.stat().st_size > 32 * 1024 * 1024 or err.stat().st_size > 4 * 1024 * 1024:
            raise ValueError('运行工具输出异常，已停止本次处理')
        output = out.read_text(encoding='utf-8', errors='replace')
        error = err.read_text(encoding='utf-8', errors='replace')
        if proc.returncode or 'compute failed' in error.lower():
            raise ValueError(f'本地运行工具失败（退出码 {proc.returncode}）：{error[-1000:]}')
        return output, error


def bind_runtime(directory):
    if not supported_platform():
        raise ValueError(runtime_status()['reason'])
    root = Path(directory).expanduser().resolve()
    candidates = [root] if root.is_file() else [root / 'transcribe.dll', root / 'bin' / 'transcribe.dll']
    library = next((p for p in candidates if p.is_file()), None)
    if not library or library.name != 'transcribe.dll':
        raise ValueError('请选择含 transcribe.dll 和配套 DLL 的 Windows 运行工具目录')
    with library.open('rb') as stream:
        if stream.read(2) != b'MZ':
            raise ValueError('该文件不是 Windows DLL，不能导入 Mac 运行工具')
    output, _ = run_process([sys.executable, '-X', 'utf8', Path(__file__).with_name('asr_worker.py'),
                            '--library', library, '--probe'], timeout=30)
    verified = json.loads(output)
    if verified.get('version') != VERSION or not verified.get('cpu'):
        raise ValueError('运行工具版本或 CPU 接口不兼容，请使用 transcribe.cpp 0.4.0 Windows 版本')
    files = list(library.parent.glob('*.dll'))
    write_json(runtime_root() / 'binding.json', {'library': str(library),
               'files': {str(p): fingerprint(p) for p in files}, 'verified_abi': verified})
    return runtime_status()


def install_runtime():
    """Explicit runtime-only action; never called on startup or model selection."""
    if not supported_platform():
        raise ValueError(runtime_status()['reason'])
    with runtime_lock:
        if runtime_state['state'] == 'downloading':
            return runtime_status()
        runtime_state.update(state='downloading', message='正在准备运行工具（不下载模型）')
        runtime_cancel.clear()

    def download():
        try:
            existing = discover_runtime()
            if existing:
                bind_runtime(existing)
                runtime_state.update(state='ready', message='已验证并复用已安装的运行工具；未下载任何模型')
                return
            import httpx
            runtime_root().mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(dir=runtime_root(), prefix='prepare-') as work:
                archive = Path(work) / 'runtime.tar.gz'
                count = 0
                with httpx.stream('GET', RUNTIME_URL, follow_redirects=True, timeout=30) as response:
                    response.raise_for_status()
                    with archive.open('wb') as target:
                        for chunk in response.iter_bytes(1024 * 1024):
                            if runtime_cancel.is_set():
                                raise InterruptedError('已取消运行工具下载')
                            count += len(chunk)
                            if count > 32 * 1024 * 1024:
                                raise ValueError('运行工具包大小异常')
                            target.write(chunk)
                if sha256(archive) != RUNTIME_SHA256:
                    raise ValueError('运行工具校验失败，未安装')
                extracted = Path(work) / 'unpacked'
                extracted.mkdir()
                with tarfile.open(archive) as package:
                    members = package.getmembers()
                    for member in members:
                        destination = (extracted / member.name).resolve()
                        if not destination.is_relative_to(extracted.resolve()) or not (member.isfile() or member.isdir()):
                            raise ValueError('运行工具包包含不支持的路径')
                    if sum(m.size for m in members) > 160 * 1024 * 1024:
                        raise ValueError('运行工具解压大小异常')
                    package.extractall(extracted, members=members, filter='data')
                library = next(extracted.rglob('transcribe.dll'), None)
                if not library:
                    raise ValueError('运行工具包缺少 Windows DLL')
                if runtime_cancel.is_set():
                    raise InterruptedError('已取消运行工具下载')
                # Unique directory: no deletion/overwrite of previously imported runtimes.
                installed = runtime_root() / ('installed-' + str(time.time_ns()))
                relative = library.relative_to(extracted)
                extracted.replace(installed)
                bind_runtime(str((installed / relative).parent))
            runtime_state.update(state='ready', message='运行工具接口已验证；模型需单独导入')
        except Exception as exc:
            runtime_state.update(state='idle' if isinstance(exc, InterruptedError) else 'error', message=str(exc)[:600])
    threading.Thread(target=download, daemon=True).start()
    return runtime_status()


def bind_model(name, path):
    root = Path(path).expanduser().resolve()
    if name in NATIVE_MODELS:
        spec = NATIVE_MODELS[name]
        file = root / spec['file'] if root.is_dir() else root
        if not file.is_file() or file.stat().st_size != spec['bytes']:
            raise ValueError('请选择完整的 ' + spec['file'] + '；MLX 或其他 GGUF 格式不能混用')
        with file.open('rb') as stream:
            if stream.read(4) != b'GGUF':
                raise ValueError('模型文件不是 GGUF 格式')
        if sha256(file) != spec['sha256']:
            raise ValueError('模型 SHA-256 校验失败，请核对来源与文件完整性')
        files = [file]
        root = file
    elif name in LEGACY:
        files = [root / 'model.bin', root / 'config.json', root / 'tokenizer.json']
        if not all(p.is_file() for p in files):
            raise ValueError('请选择含 model.bin、config.json、tokenizer.json 的 CTranslate2 模型目录')
        from faster_whisper import WhisperModel
        model = WhisperModel(str(root), device='cpu', compute_type='int8', cpu_threads=2, local_files_only=True)
        del model
    else:
        raise ValueError('不支持的本地模型')
    write_json(binding_file(name), {'path': str(root), 'files': {str(p): fingerprint(p) for p in files}})
    return {'imported': True, 'path': str(root)}


def model_status(name):
    spec = NATIVE_MODELS[name]
    binding = checked_binding(name)
    runtime = runtime_status()
    ready = bool(binding) and runtime['ready']
    return dict(id=name, **{k: spec[k] for k in ('name', 'note', 'size', 'timestamps', 'speakers')},
        backend='transcribe.cpp · CPU', native=True, installed=ready, model_ready=bool(binding),
        runtime_ready=runtime['ready'], supported=runtime['supported'],
        state='ready' if ready else 'idle', path=binding['path'] if binding else '',
        filename=spec['file'], source_url='https://huggingface.co/' + spec['repo'] + '/tree/main',
        message=runtime['reason'] or ('文件和运行接口已校验；首次转录将加载模型' if ready else
                   '缺少运行工具' if binding else '缺少模型文件（不会自动下载）'))


def discover_models():
    """Bounded discovery in known cache roots; never scan user's documents or load weights."""
    roots = [store.DATA / 'models', Path.home() / '.cache' / 'huggingface' / 'hub']
    configured = os.environ.get('HF_HUB_CACHE')
    if configured:
        roots.append(Path(configured))
    candidates = []
    for name, spec in NATIVE_MODELS.items():
        patterns = [spec['file'], '*/' + spec['file'],
                    'models--' + spec['repo'].replace('/', '--') + '/snapshots/*/' + spec['file']]
        for root in roots:
            if root.is_dir():
                for pattern in patterns:
                    for path in root.glob(pattern):
                        if path.is_file():
                            candidates.append({'id': name, 'path': str(path.resolve()), 'verified': False})
    for name in LEGACY:
        root = store.DATA / 'models' / name
        if (root / 'model.bin').is_file():
            candidates.append({'id': name, 'path': str(root), 'verified': (root / '.ready').is_file()})
        for cache in roots[1:]:
            if cache.is_dir():
                for path in cache.glob('models--*--faster-whisper-' + name + '/snapshots/*/model.bin'):
                    candidates.append({'id': name, 'path': str(path.resolve().parent) if not path.is_symlink() else str(path.parent), 'verified': False})
    return candidates


class IncompleteResult(ValueError):
    def __init__(self, text):
        super().__init__('模型的时间轴或发言人结果不完整，未标记为转录完成')
        self.partial_text = text


def parse_rows(stdout, files, name, duration):
    rows = []
    header_seen = False
    for line in stdout.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError as exc:
            raise ValueError('本地模型返回了无效 JSONL') from exc
        if not isinstance(row, dict):
            raise ValueError('本地模型返回格式异常')
        if row.get('type') == 'batch_header':
            if rows or header_seen:
                raise ValueError('本地模型返回了重复批次')
            header_seen = True
            continue
        rows.append(row)
    if len(rows) != len(files):
        raise ValueError('模型未返回全部音频块，请保留原音频后重试')
    texts, segments = [], []
    for row, file in zip(rows, files):
        if row.get('file') != str(file) or row.get('error') or not isinstance(row.get('text'), str):
            raise ValueError('本地模型处理失败或返回了错误的源文件')
        text = re.sub(r'<\|[^>]*\|>', '', row['text']).strip()
        if name == 'sensevoice-q8':
            texts.append(text)
            continue  # No fabricated timing for an untimed model.
        raw_segments = row.get('segments', [])
        if not isinstance(raw_segments, list) or (text and not raw_segments):
            raise IncompleteResult(text)
        current = []
        for segment in raw_segments:
            if not isinstance(segment, dict):
                raise IncompleteResult(text)
            start, end, body = segment.get('t0_ms'), segment.get('t1_ms'), segment.get('text')
            speaker = segment.get('speaker_id')
            if (type(start) is not int or type(end) is not int or start < 0 or end <= start or
                    end > duration * 1000 + 100 or not isinstance(body, str) or
                    (speaker is not None and (type(speaker) is not int or not 0 < speaker <= 2147483647))):
                raise IncompleteResult(text)
            if body.strip():
                current.append(dict(start=start / 1000, end=end / 1000, text=body.strip(),
                                    **({'speaker_id': speaker} if speaker is not None else {})))
        compact = lambda value: re.sub(r'\s+', '', value)
        if compact(''.join(s['text'] for s in current)) != compact(text):
            raise IncompleteResult(text)
        if name == 'moss-q5' and text:
            raw = row.get('raw_text', '')
            if not isinstance(raw, str):
                raise IncompleteResult(text)
            matches = list(re.finditer(r'\[(\d+(?:\.\d+)?)\]\[S(\d+)\](.*?)\[(\d+(?:\.\d+)?)\]', raw, re.S))
            if len(matches) != len(current) or re.sub(r'\[(\d+(?:\.\d+)?)\]\[S(\d+)\](.*?)\[(\d+(?:\.\d+)?)\]', '', raw, flags=re.S).strip():
                raise IncompleteResult(text)
            for match, segment in zip(matches, current):
                start, speaker, body, end = match.groups()
                if (abs(float(start) - segment['start']) > .001 or abs(float(end) - segment['end']) > .001 or
                        int(speaker) != segment.get('speaker_id') or compact(body) != compact(segment['text'])):
                    raise IncompleteResult(text)
        texts.append(text)
        segments.extend(current)
    return '\n'.join(t for t in texts if t), segments


def transcribe(name, source, language, check_cancel=lambda: None):
    runtime = runtime_status()
    model = external_path(name)
    if not runtime['ready'] or not model:
        raise ValueError('请在本地语音转录设置中准备运行工具并导入所选模型')
    if name == 'moss-q5' and language not in ('auto', 'zh', 'en'):
        raise ValueError('MOSS 支持中文和英语，请切换识别语言或模型')
    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg:
        raise ValueError('缺少 FFmpeg，无法准备本地转录音频')
    check_cancel()
    with tempfile.TemporaryDirectory(prefix='lulu-native-audio-') as work:
        root = Path(work)
        wav = root / 'audio.wav'
        run_process([ffmpeg, '-hide_banner', '-loglevel', 'error', '-y', '-i', source,
                     '-vn', '-ac', '1', '-ar', '16000', '-c:a', 'pcm_s16le', wav], check_cancel, timeout=1800)
        with wave.open(str(wav), 'rb') as audio:
            duration = audio.getnframes() / audio.getframerate()
        if duration <= 0:
            raise ValueError('音频中没有可处理的采样')
        # MOSS has a finite context window; do not silently truncate long recordings.
        if name == 'moss-q5' and math.ceil(duration / 30) * 375 + math.ceil(duration / 5) * 20 + 512 >= 131072:
            raise ValueError('音频超过 MOSS 单次上下文，请先切片后再转录')
        files = [wav]
        if name == 'sensevoice-q8' and duration > 25:
            run_process([ffmpeg, '-hide_banner', '-loglevel', 'error', '-i', wav, '-f', 'segment',
                         '-segment_time', '25', '-c', 'copy', root / 'chunk-%05d.wav'], check_cancel, timeout=1800)
            files = sorted(root.glob('chunk-*.wav'))
        batch = root / 'input.txt'
        batch.write_text(''.join(str(p) + '\n' for p in files), encoding='utf-8')
        args = [sys.executable, '-X', 'utf8', Path(__file__).with_name('asr_worker.py'),
                '--library', runtime['library'], '--model', model, '--batch', batch]
        if language != 'auto':
            args.extend(['--language', language])
        if NATIVE_MODELS[name]['timestamps']:
            args.append('--timestamps')
        if name == 'moss-q5':
            args.append('--diarize')
        stdout, _ = run_process(args, check_cancel)
        check_cancel()
        text, segments = parse_rows(stdout, files, name, duration)
        languages = {row.get('language') for row in (json.loads(line) for line in stdout.splitlines() if line.strip())
                     if isinstance(row.get('language'), str) and row.get('language')}
        return {'text': text, 'segments': segments, 'duration': duration,
                'language': next(iter(languages)) if len(languages) == 1 else '',
                'asr': dict(model_id=name, runtime='transcribe.cpp', runtime_version=VERSION,
                            model_sha256=NATIVE_MODELS[name]['sha256'], backend='cpu',
                            timestamp_kind='segment' if NATIVE_MODELS[name]['timestamps'] else 'none',
                            diarization=NATIVE_MODELS[name]['speakers'], timing_estimated=False)}


class ImportPath(BaseModel):
    path: str = Field(min_length=1, max_length=4096)


@router.get('/status')
def status_route():
    return {'runtime': runtime_status(), 'models': [model_status(name) for name in NATIVE_MODELS],
            'candidates': discover_models(), 'runtime_candidate': discover_runtime()}


@router.post('/runtime/install')
def runtime_install_route():
    try:
        return install_runtime()
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post('/runtime/cancel')
def runtime_cancel_route():
    runtime_cancel.set()
    return {'cancelled': True}


@router.post('/runtime/import')
def runtime_import_route(data: ImportPath):
    try:
        return bind_runtime(data.path)
    except (OSError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post('/models/{name}/import')
def model_import_route(name: str, data: ImportPath):
    try:
        return bind_model(name, data.path)
    except (OSError, ValueError, RuntimeError) as exc:
        raise HTTPException(400, str(exc)) from exc


@router.delete('/models/{name}/binding')
def model_unbind_route(name: str):
    try:
        binding_file(name).unlink(missing_ok=True)
        return {'removed': True, 'source_files_preserved': True}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
