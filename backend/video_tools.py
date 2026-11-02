"""Local frame cadence and watermark exports; originals are never modified."""
from __future__ import annotations

import io
import json
import math
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from PIL import Image, ImageColor, ImageDraw, ImageFont, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, Field
from starlette.background import BackgroundTask

from . import store

router = APIRouter(prefix='/api/video-tools')
JOB_KIND = 'video_tools_jobs'
_lock = threading.RLock()
_gate = threading.Semaphore(1)
_stopping = threading.Event()
_active: dict[str, 'Control'] = {}


@dataclass
class Control:
    cancel: threading.Event = field(default_factory=threading.Event)
    process: subprocess.Popen | None = None
    thread: threading.Thread | None = None


class Watermark(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    type: Literal['none', 'text', 'image'] = 'none'
    text: str = Field(default='', max_length=500)
    image_id: str = ''
    x: float = Field(default=.95, ge=0, le=1)
    y: float = Field(default=.95, ge=0, le=1)
    size: float = Field(default=5, ge=.5, le=50)
    opacity: float = Field(default=.8, ge=0, le=1)
    color: str = Field(default='#ffffff', pattern=r'^#[0-9a-fA-F]{6}$')


class ExportRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    item_id: str
    drop_per_second: int = Field(default=0, ge=0, le=239, strict=True)
    watermark: Watermark = Field(default_factory=Watermark)


class PreviewRequest(ExportRequest):
    seconds: float = Field(default=0, ge=0, le=86400)


def _root() -> Path:
    root = store.DATA / 'video-tools'
    root.mkdir(parents=True, exist_ok=True)
    return root


def _id(value: str) -> str:
    if not re.fullmatch(r'[a-f0-9]{32}', value or ''):
        raise ValueError('无效的视频处理标识')
    return value


def _watermark_path(image_id: str) -> Path:
    path = _root() / 'watermarks' / (_id(image_id) + '.png')
    if not path.is_file():
        raise ValueError('水印图片不存在，请重新上传')
    return path


def _command(name: str) -> str:
    executable = shutil.which(name)
    if not executable:
        raise ValueError(f'没有找到 {name}，请安装 FFmpeg 后重试')
    return executable


def _probe(path: Path) -> dict:
    result = subprocess.run([_command('ffprobe'), '-v', 'error', '-protocol_whitelist', 'file,pipe',
        '-show_streams', '-show_format', '-of', 'json', str(path)], capture_output=True,
        text=True, encoding='utf-8', errors='replace', timeout=30,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode:
        raise ValueError('无法读取视频信息，请确认文件完整且包含视频画面')
    try:
        data = json.loads(result.stdout)
        video = next(s for s in data.get('streams', []) if s.get('codec_type') == 'video' and not s.get('disposition', {}).get('attached_pic'))
        rate = video.get('avg_frame_rate') or video.get('r_frame_rate')
        try:
            fps = float(Fraction(rate))
        except (ValueError, ZeroDivisionError, TypeError):
            fps = float(Fraction(video.get('r_frame_rate', '0')))
        duration = float(video.get('duration') or data.get('format', {}).get('duration') or 0)
        width, height = int(video['width']), int(video['height'])
        rotation = float(next((s['rotation'] for s in video.get('side_data_list', []) if 'rotation' in s), video.get('tags', {}).get('rotate', 0)))
    except (ValueError, TypeError, KeyError, StopIteration, ZeroDivisionError):
        raise ValueError('无法确定视频的时长、分辨率或帧率，纯音频不能使用视频处理') from None
    if not math.isfinite(fps) or not 1 <= fps <= 240 or not math.isfinite(duration) or not 0 < duration <= 86400:
        raise ValueError('视频帧率须为 1–240 fps，时长须大于 0 且不超过 24 小时')
    if not 1 <= width <= 16384 or not 1 <= height <= 16384:
        raise ValueError('视频分辨率不受支持')
    if abs(round(rotation)) % 180 == 90:
        width, height = height, width
    base = round(fps)
    # Every export runs through fps=base, including variable-frame-rate sources.
    return {'duration': duration, 'width': width, 'height': height, 'fps': fps, 'base_fps': base,
        'frame_count': int(video['nb_frames']) if str(video.get('nb_frames', '')).isdigit() else 0,
        'has_audio': any(s.get('codec_type') == 'audio' for s in data.get('streams', [])),
        'rotation': rotation, 'normalized': True, 'non_integer_fps': abs(fps - base) > .001,
        'normalization': f'先统一为 {base} fps，再均匀保留画面；时长和音频速度保持不变'}


def _source(item_id: str) -> tuple[dict, Path, dict]:
    item = store.get(_id(item_id))
    if not item or not item.get('media_path'):
        raise ValueError('请先导入视频，或选择已保留本地视频的素材')
    if item.get('kind') == 'article':
        raise ValueError('图文文章没有可处理的视频')
    if item.get('status') in ('queued', 'processing'):
        raise ValueError('素材仍在处理中，请完成或暂停后再处理视频')
    path = Path(item['media_path'])
    if not path.is_file():
        raise ValueError('源视频文件不存在，请重新导入')
    info = {'item_id': item['id'], 'title': item['title'], **_probe(path)}
    return item, path, info


def _validate(body: ExportRequest) -> tuple[dict, Path, dict]:
    item, path, info = _source(body.item_id)
    if body.drop_per_second >= info['base_fps']:
        raise ValueError(f'每秒删除帧数必须小于 {info["base_fps"]}，至少保留 1 帧')
    if info['width'] % 2 or info['height'] % 2:
        raise ValueError('当前视频宽高含奇数，无法直接导出兼容播放器的 H.264；请先将源视频转成偶数分辨率')
    if body.watermark.type == 'text' and not body.watermark.text.strip():
        raise ValueError('请填写水印文字')
    if body.watermark.type == 'image':
        _watermark_path(body.watermark.image_id)
    info['output_fps'] = info['base_fps'] - body.drop_per_second
    return item, path, info


@router.get('/source/{item_id}')
def source(item_id: str):
    return _source(item_id)[2]


@router.post('/watermark')
async def upload_watermark(file: UploadFile = File(...)):
    try:
        buffer = bytearray()
        while chunk := await file.read(1024 * 1024):
            buffer.extend(chunk)
            if len(buffer) > 16 * 1024 * 1024:
                raise ValueError('水印图片最大支持 16 MB')
        try:
            with Image.open(io.BytesIO(buffer)) as image:
                if image.format not in ('PNG', 'JPEG', 'WEBP') or image.width * image.height > 16_000_000:
                    raise ValueError('请上传 PNG、JPEG 或 WebP 图片，最多 1600 万像素')
                image.load()
                normalized = image.convert('RGBA')
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
            raise ValueError('水印图片无法读取，请选择有效的 PNG、JPEG 或 WebP 文件') from None
        image_id = uuid.uuid4().hex
        directory = _root() / 'watermarks'
        directory.mkdir(exist_ok=True)
        normalized.save(directory / (image_id + '.png'), format='PNG')
        return {'id': image_id, 'url': f'/api/video-tools/watermark/{image_id}', 'width': normalized.width, 'height': normalized.height}
    finally:
        await file.close()


@router.get('/watermark/{image_id}')
def watermark_image(image_id: str):
    return FileResponse(_watermark_path(image_id), media_type='image/png')


def _font(size: int):
    candidates = [Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Fonts' / name for name in ('msyh.ttc', 'msyh.ttf', 'simhei.ttf', 'segoeui.ttf')]
    candidates += [Path('/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc'), Path('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf')]
    for path in candidates:
        if path.is_file():
            return ImageFont.truetype(str(path), max(1, size))
    raise ValueError('没有找到可用于水印的字体，请安装微软雅黑或 Noto Sans CJK')


def _overlay(watermark: Watermark, info: dict, directory: Path):
    if watermark.type == 'none':
        return None
    width, height = info['width'], info['height']
    size = max(1, round(height * watermark.size / 100))
    if watermark.type == 'image':
        with Image.open(_watermark_path(watermark.image_id)) as original:
            image = original.convert('RGBA')
        ratio = min(size / image.height, width / image.width, height / image.height)
        dimensions = (max(1, round(image.width * ratio)), max(1, round(image.height * ratio)))
        if dimensions[0] * dimensions[1] > 16_000_000:
            raise ValueError('水印尺寸过大，请减小水印大小')
        image = image.resize(dimensions, Image.Resampling.LANCZOS)
    else:
        # All user text is rasterized; none becomes FFmpeg filter syntax.
        font = _font(size)
        draw = ImageDraw.Draw(Image.new('RGBA', (1, 1)))
        spacing = max(1, size // 5)
        box = draw.multiline_textbbox((0, 0), watermark.text, font=font, spacing=spacing, stroke_width=1)
        # Fit the font before allocating an image: a long line on a large source
        # must not allocate an enormous intermediate bitmap just to resize it.
        for _ in range(3):
            box_width, box_height = max(1, box[2] - box[0] + 4), max(1, box[3] - box[1] + 4)
            factor = min(width / box_width, height / box_height,
                         math.sqrt(16_000_000 / (box_width * box_height)), 1)
            if factor >= 1 or size == 1:
                break
            size = max(1, int(size * factor * .98))
            font = _font(size)
            spacing = max(1, size // 5)
            box = draw.multiline_textbbox((0, 0), watermark.text, font=font, spacing=spacing, stroke_width=1)
        if (box[2] - box[0] + 4) * (box[3] - box[1] + 4) > 16_000_000:
            raise ValueError('水印文字尺寸过大，请缩短文字或减小字号')
        image = Image.new('RGBA', (max(1, box[2] - box[0] + 4), max(1, box[3] - box[1] + 4)))
        draw = ImageDraw.Draw(image)
        draw.multiline_text((2 - box[0], 2 - box[1]), watermark.text, font=font,
            fill=ImageColor.getrgb(watermark.color) + (255,), spacing=spacing,
            stroke_width=1, stroke_fill=(0, 0, 0, 190))
        if image.width > width or image.height > height:
            image.thumbnail((width, height), Image.Resampling.LANCZOS)
    image.putalpha(image.getchannel('A').point(lambda a: round(a * watermark.opacity)))
    path = directory / 'watermark.png'
    image.save(path, 'PNG')
    return path, round((width - image.width) * watermark.x), round((height - image.height) * watermark.y)


def _args(path: Path, info: dict, body: ExportRequest, directory: Path, output: Path, seconds=None):
    command = [_command('ffmpeg'), '-nostdin', '-hide_banner', '-loglevel', 'error', '-y', '-protocol_whitelist', 'file,pipe']
    if seconds is not None:
        command += ['-ss', str(seconds)]
    command += ['-i', str(path)]
    overlay = _overlay(body.watermark, info, directory)
    if overlay:
        command += ['-i', str(overlay[0])]
    fps, kept = info['base_fps'], info['output_fps']
    video_filter = ('null' if seconds is not None else
        f'fps={fps},select=lt(mod(n*{kept}\\,{fps})\\,{kept}),setpts=N/({kept}*TB),tpad=stop_mode=clone:stop_duration=1')
    if overlay:
        filters = f'[0:v:0]{video_filter}[base];[base][1:v:0]overlay={overlay[1]}:{overlay[2]}:format=auto:eof_action=repeat[out]'
    else:
        filters = f'[0:v:0]{video_filter}[out]'
    command += ['-filter_complex_threads', '1', '-filter_complex', filters, '-map', '[out]']
    if seconds is not None:
        return command + ['-frames:v', '1', '-an', '-c:v', 'png', '-update', '1', str(output)]
    if info['has_audio']:
        command += ['-map', '0:a:0', '-af', f'apad,atrim=end={info["duration"]}', '-c:a', 'aac', '-b:a', '192k']
    # Keep the final partial second short even at 1 fps. Without a packet duration
    # limit, -t alone rounds a 2.2-second, 1-fps video up to three seconds. Disable
    # B-frame reordering so the shortened final display packet is also the final
    # decode packet; rescale after encoding (changing encoder timebase with CFR
    # would cause FFmpeg to duplicate frames).
    packet_timing = ('setts=time_base=1/90000:pts=PTS*TB*90000:dts=DTS*TB*90000:'
        f'duration=min(DURATION*TB*90000\\,max(1\\,ceil({info["duration"]}*90000)-PTS*TB*90000))')
    command += ['-c:v', 'libx264', '-preset', 'veryfast', '-crf', '18', '-pix_fmt', 'yuv420p', '-bf', '0',
        '-r', str(kept), '-fps_mode', 'cfr', '-bsf:v', packet_timing, '-video_track_timescale', '90000',
        '-frames:v', str(max(1, math.ceil(info['duration'] * kept - 1e-7))), '-map_metadata', '-1',
        '-movflags', '+faststart', '-progress', 'pipe:1', '-nostats', str(output)]
    return command


def _public(job: dict):
    return {key: value for key, value in job.items() if key not in ('source_path', 'output_path')}


def _save(job: dict, **values):
    job.update(values)
    job['updated_at'] = time.time()
    store.object_put(JOB_KIND, job['id'], job)


def _get(job_id: str) -> dict:
    job = store.object_get(JOB_KIND, _id(job_id))
    if not job:
        raise HTTPException(404, '找不到视频处理任务')
    return job


def _terminate(process):
    if process and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)


def _run(command, control: Control, directory: Path, progress):
    log = directory / 'ffmpeg.log'
    with log.open('wb') as error_file:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=error_file,
            text=True, encoding='utf-8', errors='replace', creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        control.process = process
        last = [0.0]
        def read_progress():
            for line in process.stdout:
                if line.startswith('out_time_us='):
                    try:
                        last[0] = float(line.partition('=')[2]) / 1_000_000
                    except ValueError:
                        pass
        reader = threading.Thread(target=read_progress, daemon=True)
        reader.start()
        try:
            previous = 0.0
            while process.poll() is None:
                if control.cancel.wait(.1) or _stopping.is_set():
                    _terminate(process)
                    raise InterruptedError('已取消视频处理')
                if time.monotonic() - previous > .5:
                    progress(last[0])
                    previous = time.monotonic()
            reader.join(timeout=2)
            if control.cancel.is_set() or _stopping.is_set():
                raise InterruptedError('已取消视频处理')
            if process.returncode:
                error_file.flush()
                detail = log.read_bytes()[-1800:].decode('utf-8', errors='replace')
                raise ValueError('视频导出失败：' + detail[-500:])
        finally:
            _terminate(process)
            reader.join(timeout=2)
            process.stdout.close()
            control.process = None


def _worker(job_id: str, control: Control):
    job = _get(job_id)
    acquired = False
    directory = _root() / 'jobs' / job_id
    temporary = directory / 'export.partial.mp4'
    output = directory / 'export.mp4'
    try:
        while not acquired:
            if control.cancel.is_set() or _stopping.is_set():
                raise InterruptedError('已取消视频处理')
            acquired = _gate.acquire(timeout=.2)
        directory.mkdir(parents=True, exist_ok=True)
        body = ExportRequest.model_validate(job['options'])
        # Re-check the source after waiting in the queue.
        item, path, info = _validate(body)
        _save(job, status='running', phase='正在处理视频', source_info=info, progress=0)
        _run(_args(path, info, body, directory, temporary), control, directory,
             lambda seconds: _save(job, progress=min(99, round(seconds / info['duration'] * 100, 1))))
        if not temporary.is_file() or not temporary.stat().st_size:
            raise ValueError('没有生成有效的视频文件')
        actual = _probe(temporary)
        if abs(actual['duration'] - info['duration']) > .02:
            raise ValueError('导出时长与原视频不符，未将结果标记为成功')
        if actual['width'] != info['width'] or actual['height'] != info['height'] or actual['has_audio'] != info['has_audio']:
            raise ValueError('导出的视频尺寸或音轨不完整，未将结果标记为成功')
        expected_frames = max(1, math.ceil(info['duration'] * info['output_fps'] - 1e-7))
        if actual['frame_count'] != expected_frames:
            raise ValueError('导出帧数与设置不符，未将结果标记为成功')
        with _lock:
            if control.cancel.is_set() or _stopping.is_set():
                raise InterruptedError('已取消视频处理')
            temporary.replace(output)
            saved = store.add(item['title'] + ' · 视频处理', kind='clip', media_path=str(output),
                duration=actual['duration'], status='done', phase='视频处理完成', progress=100,
                metadata={'source_id': item['id'], 'video_tools': body.model_dump(), 'video_job_id': job_id})
            _save(job, status='done', phase='视频已保存到切片库', progress=100, error='',
                output_item_id=saved['id'], output_path=str(output), download_url=f'/api/video-tools/jobs/{job_id}/download')
    except InterruptedError:
        _save(job, status='interrupted' if _stopping.is_set() else 'cancelled', phase='应用退出，导出已停止' if _stopping.is_set() else '已取消', error='')
    except Exception as exc:
        _save(job, status='error', phase='视频处理失败', error=str(exc)[:700])
    finally:
        temporary.unlink(missing_ok=True)
        if acquired:
            _gate.release()
        with _lock:
            _active.pop(job_id, None)


@router.post('/jobs')
def create_job(body: ExportRequest):
    _, path, info = _validate(body)
    with _lock:
        if _stopping.is_set():
            raise ValueError('应用正在退出，请重新打开后再导出')
        job_id = uuid.uuid4().hex
        job = {'id': job_id, 'status': 'queued', 'progress': 0, 'phase': '等待视频处理', 'error': '',
            'title': info['title'], 'created_at': time.time(), 'source_info': info, 'options': body.model_dump(), 'source_path': str(path)}
        store.object_put(JOB_KIND, job_id, job)
        control = Control()
        control.thread = threading.Thread(target=_worker, args=(job_id, control), name='lulu-video-export', daemon=True)
        _active[job_id] = control
        control.thread.start()
        return _public(job)


@router.get('/jobs')
def list_jobs():
    return [_public(job) for job in store.object_list(JOB_KIND)]


@router.post('/jobs/{job_id}/cancel')
def cancel_job(job_id: str):
    with _lock:
        job = _get(job_id)
        control = _active.get(job_id)
        if control:
            control.cancel.set()
            job['phase'] = '正在取消，请稍候'
        elif job['status'] in ('queued', 'running'):
            _save(job, status='cancelled', phase='已取消', error='')
        return _public(job)


@router.get('/jobs/{job_id}/download')
def download(job_id: str):
    job = _get(job_id)
    path = _root() / 'jobs' / job_id / 'export.mp4'
    if job['status'] != 'done' or not path.is_file():
        raise HTTPException(404, '视频尚未导出成功或文件已被移除')
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', job['title'])[:100].strip(' .') or 'Lulu视频'
    return FileResponse(path, media_type='video/mp4', filename=name + ' · 视频处理.mp4')


def _cleanup_preview(directory: Path):
    for name in ('preview.png', 'watermark.png'):
        (directory / name).unlink(missing_ok=True)
    try:
        directory.rmdir()
    except OSError:
        pass


@router.post('/preview')
def preview(body: PreviewRequest):
    _, path, info = _validate(body)
    if body.seconds >= info['duration']:
        raise ValueError('预览时间必须小于视频时长')
    directory = _root() / 'previews' / uuid.uuid4().hex
    directory.mkdir(parents=True)
    output = directory / 'preview.png'
    try:
        result = subprocess.run(_args(path, info, body, directory, output, seconds=body.seconds),
            capture_output=True, timeout=45, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if result.returncode or not output.is_file():
            raise ValueError('无法生成预览，请检查视频是否完整')
        return FileResponse(output, media_type='image/png', background=BackgroundTask(_cleanup_preview, directory))
    except Exception:
        _cleanup_preview(directory)
        raise


def recover():
    """Called once at startup; an interrupted encoder is never reported as done."""
    _stopping.clear()
    for job in store.object_list(JOB_KIND):
        if job.get('status') in ('queued', 'running'):
            _save(job, status='interrupted', phase='上次导出中断，请重新导出', error='')
            (_root() / 'jobs' / _id(job['id']) / 'export.partial.mp4').unlink(missing_ok=True)


def shutdown(timeout=8):
    _stopping.set()
    with _lock:
        controls = list(_active.values())
        for control in controls:
            control.cancel.set()
    deadline = time.monotonic() + max(0, min(float(timeout), 8))
    for control in controls:
        if control.thread and control.thread is not threading.current_thread():
            control.thread.join(timeout=max(0, deadline - time.monotonic()))
    return not any(c.thread and c.thread.is_alive() for c in controls)
