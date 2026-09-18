import csv
import io
import json
import os
import re
import shutil
import sys
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote, urlparse

from fastapi import FastAPI, HTTPException, Request, UploadFile, File
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import store, engine, integrations, secrets, workflows, assets


@asynccontextmanager
async def lifespan(app):
    engine.simplify_saved_content()
    engine.start_worker()
    yield


app = FastAPI(title='Lulu Workbench', lifespan=lifespan, docs_url=None, redoc_url=None)
app.include_router(workflows.router)


@app.middleware('http')
async def local_only(request: Request, call_next):
    host = request.url.hostname
    if host not in ('127.0.0.1', 'localhost', 'testserver'):
        return JSONResponse({'detail': '仅允许本地访问'}, 403)
    origin = request.headers.get('origin')
    if origin and origin != str(request.base_url).rstrip('/'):
        return JSONResponse({'detail': '不允许跨站访问'}, 403)
    if request.url.path.startswith('/api/') and request.method not in ('GET', 'HEAD'):
        if request.headers.get('x-lulu-client') != 'desktop':
            return JSONResponse({'detail': '缺少本地客户端标识'}, 403)
    result = await call_next(request)
    result.headers['X-Content-Type-Options'] = 'nosniff'
    result.headers['Referrer-Policy'] = 'no-referrer'
    result.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'"
    return result


@app.exception_handler(ValueError)
async def bad_value(request, exc):
    return JSONResponse({'detail': str(exc)}, 400)


def require(item_id):
    item = store.get(item_id)
    if not item:
        raise HTTPException(404, '找不到这份素材')
    return item


def public_settings():
    cfg = store.settings()
    return {k: v for k, v in cfg.items() if not k.endswith('_enc')} | {
        'has_llm_key': bool(cfg.get('llm_api_key_enc')), 'has_feishu_secret': bool(cfg.get('feishu_secret_enc'))}


@app.get('/api/health')
def health():
    return {'ok': True, 'version': '0.1.0', 'data_dir': str(store.DATA),
            'ffmpeg': bool(shutil.which('ffmpeg')), 'pending': len(engine.pending)}


@app.post('/api/shutdown')
def shutdown(request: Request):
    import hmac
    expected = os.environ.get('LULU_SHUTDOWN_TOKEN', '')
    if not expected or not hmac.compare_digest(request.headers.get('x-lulu-shutdown', ''), expected):
        raise HTTPException(403, '无效的关闭请求')
    app.state.server.should_exit = True
    return {'ok': True}


@app.get('/api/state')
def state():
    # The list is lightweight even for long transcripts.
    result = [{k: v for k, v in i.items() if k not in ('transcript', 'segments')} | {'characters': len(i['transcript']), 'has_transcript': bool(i['transcript'])} for i in store.items()]
    return {'items': result, 'settings': public_settings(), 'models': engine.all_models(), 'data_dir': str(store.DATA), 'folders':store.folders()}


@app.get('/api/items/{item_id}')
def get_item(item_id: str):
    return require(item_id)


@app.get('/api/items/{item_id}/versions')
def item_versions(item_id: str):
    require(item_id)
    return store.versions(item_id)


@app.get('/api/items/{item_id}/versions/{version_id}')
def item_version(item_id: str, version_id: str):
    require(item_id)
    saved = store.version(item_id, version_id)
    if not saved:
        raise HTTPException(404, '找不到这份历史文稿')
    return saved


@app.post('/api/items/{item_id}/versions/{version_id}/restore')
def restore_item_version(item_id: str, version_id: str):
    with engine.job_lock:
        item = require(item_id)
        if item_id in engine.pending or item['status'] in ('processing', 'queued'):
            raise ValueError('请先暂停任务，待处理停止后再恢复历史文稿')
        restored = store.restore_version(item_id, version_id)
        if not restored:
            raise HTTPException(404, '找不到这份历史文稿')
        return restored


@app.get('/api/drafts/{key}')
def read_draft(key: str):
    with store.connection() as con:
        row = con.execute('SELECT value FROM drafts WHERE key=?', (key,)).fetchone()
        return json.loads(row['value']) if row else None


@app.put('/api/drafts/{key}')
def save_draft(key: str, body: dict):
    if len(key) > 100 or len(json.dumps(body)) > 4000000:
        raise ValueError('草稿超过支持的大小')
    with store.connection() as con:
        con.execute('INSERT OR REPLACE INTO drafts VALUES (?,?)', (key, json.dumps(body, ensure_ascii=False)))
    return {'ok': True}


@app.delete('/api/drafts/{key}')
def delete_draft(key: str):
    with store.connection() as con:
        con.execute('DELETE FROM drafts WHERE key=?', (key,))
    return {'ok': True}


@app.get('/api/items/{item_id}/media')
def media(item_id: str):
    item = require(item_id)
    path = Path(item['media_path'])
    if not item['media_path'] or not path.is_file():
        raise HTTPException(404, '音视频文件不存在')
    return FileResponse(path)


@app.post('/api/import')
async def import_files(files: list[UploadFile] = File(...)):
    if len(files) > 50:
        raise ValueError('每次最多导入 50 个文件')
    result = []
    for file in files:
        filename = Path((file.filename or '录音.webm').replace('\\', '/')).name
        suffix = Path(filename).suffix.lower()
        if suffix not in engine.MEDIA_SUFFIXES | {'.txt', '.md', '.srt'}:
            raise ValueError(f'暂不支持 {suffix} 文件')
        item_dir = store.DATA / 'media' / uuid.uuid4().hex
        item_dir.mkdir()
        path = item_dir / ('source' + suffix)
        try:
            with path.open('wb') as target:
                while chunk := await file.read(1024*1024):
                    target.write(chunk)
            if suffix in {'.txt', '.md', '.srt'}:
                text = path.read_text(encoding='utf-8-sig')
                segments = engine.parse_srt(text) if suffix == '.srt' else []
                if suffix == '.srt' and not segments:
                    raise ValueError('字幕格式无法识别，请使用标准 SRT 格式')
                segments = [{**segment, 'text': engine.to_simplified(segment['text'])} for segment in segments]
                result.append(store.add(Path(filename).stem, kind='document', transcript='\n'.join(s['text'] for s in segments) if segments else engine.to_simplified(text),
                    segments=segments, status='done', progress=100, phase='文稿已导入'))
            else:
                result.append(store.add(Path(filename).stem, media_path=str(path), duration=engine.media_info(path)))
        finally:
            await file.close()
    return result


class LinkInput(BaseModel):
    urls: list[str] = Field(min_length=1, max_length=100)
    folder: str = '根目录'
    download_only: bool = False
    options: dict | None = None


@app.post('/api/links')
def import_links(body: LinkInput):
    if body.options is not None: assets.validate_options(body.options)
    result = []
    for url in body.urls:
        engine.validate_url(url)
        result.append(store.add('链接转写 · ' + (urlparse(url).hostname or ''), source_url=url, folder=body.folder,
                                metadata={'download_only': body.download_only, **({'options':body.options} if body.options is not None else {})}))
    return result


class CollectionInput(BaseModel):
    url: str
    limit: int = Field(default=20, ge=1, le=100)
    after: str = ''
    before: str = ''


@app.post('/api/collections/preview')
def preview_collection(body: CollectionInput):
    try:
        return engine.collect_preview(body.url, body.limit, body.after, body.before)
    except Exception as exc:
        raise ValueError('采集失败：' + str(exc)[:700] + '。若平台要求登录，请在设置中导入你自己的 cookies.txt。') from exc


@app.post('/api/items/{item_id}/start')
def start(item_id: str):
    require(item_id)
    engine.enqueue(item_id)
    return {'ok': True}


@app.post('/api/items/{item_id}/pause')
def pause(item_id: str):
    require(item_id)
    engine.pause(item_id)
    return {'ok': True}


@app.post('/api/items/{item_id}/retranscribe')
def retranscribe(item_id: str):
    with engine.job_lock:
        item = require(item_id)
        if item_id in engine.pending:
            raise ValueError('当前任务仍在处理中')
        if not item['media_path'] and not item['source_url']:
            raise ValueError('没有可重新转录的音视频素材')
        metadata = dict(item['metadata'])
        metadata.pop('detected_language', None)
        metadata.pop('transcript_source', None)
        store.update_with_snapshot(item_id, '重新转录前', transcript='', segments=[], status='idle', progress=0,
                                   phase='等待重新转录', error='', metadata=metadata)
        engine.enqueue(item_id)
    return {'ok': True}


@app.post('/api/start-all')
def start_all():
    count = 0
    for item in reversed(store.items()):
        if item['status'] in ('idle', 'paused', 'error') and item['kind'] != 'document':
            engine.enqueue(item['id'])
            count += 1
    return {'count': count}


class EditInput(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=500)
    transcript: str | None = None
    segments: list[dict] | None = None
    folder: str | None = Field(default=None, min_length=1, max_length=200)
    clear_timestamps: bool = False


@app.patch('/api/items/{item_id}')
def edit(item_id: str, body: EditInput):
    with engine.job_lock:
        item = require(item_id)
        if item_id in engine.pending:
            raise ValueError('请等待当前任务结束后编辑')
        values = body.model_dump(exclude_none=True, exclude={'clear_timestamps'})
        if body.segments is not None:
            for s in body.segments:
                if not isinstance(s.get('text'), str) or not isinstance(s.get('start'), (int, float)) or not isinstance(s.get('end'), (int, float)) or s['start'] < 0 or s['end'] < s['start']:
                    raise ValueError('无效的字幕时间戳')
            values['segments'] = [{**segment, 'text': engine.to_simplified(segment['text'])} for segment in body.segments]
            values['transcript'] = '\n'.join(segment['text'] for segment in values['segments'])
        elif body.transcript is not None:
            text = engine.to_simplified(body.transcript)
            values['transcript'] = text
            if body.clear_timestamps:
                values['segments'] = []
            elif item['segments'] and text != item['transcript']:
                lines = text.replace('\r\n', '\n').replace('\r', '\n').split('\n')
                if len(lines) != len(item['segments']):
                    raise ValueError('全文段数与字幕不一致。请保留每行一句，或到时间轴逐句校对；大幅改写请另存口播稿，以保留原字幕时间。')
                values['segments'] = [{**segment, 'text': line} for segment, line in zip(item['segments'], lines)]
                values['transcript'] = '\n'.join(lines)
        store.update_with_snapshot(item_id, '编辑文稿前', **values)
        return require(item_id)


class DocumentInput(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    text: str
    folder: str = '根目录'


@app.post('/api/documents')
def document(body: DocumentInput):
    return store.add(body.title, kind='document', transcript=engine.to_simplified(body.text), folder=body.folder, status='done', phase='文稿已保存', progress=100)


@app.delete('/api/items/{item_id}')
def delete(item_id: str):
    require(item_id)
    if item_id in engine.pending:
        raise ValueError('请先暂停任务，待处理停止后再移除')
    store.update(item_id, deleted=1)
    return {'ok': True, 'message': '已从列表移除，原始文件仍保留在本地'}


@app.post('/api/items/{item_id}/restore')
def restore(item_id: str):
    with engine.job_lock:
        if item_id in engine.pending:
            raise ValueError('请等待当前任务停止后再恢复')
        store.restore_items([item_id])
        return require(item_id)


@app.get('/api/trash')
def trash_items():
    return store.trash()


class RestoreItemsInput(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=1000)


@app.post('/api/trash/restore')
def restore_trash(body: RestoreItemsInput):
    with engine.job_lock:
        if any(item_id in engine.pending for item_id in body.ids):
            raise ValueError('请等待所选任务停止后再恢复')
        return {'count': store.restore_items(body.ids)}


@app.get('/api/items/{item_id}/export/{fmt}')
def export(item_id: str, fmt: str):
    if fmt not in ('txt', 'srt', 'md'):
        raise HTTPException(404)
    item = require(item_id)
    safe = re.sub(r'[\\/:*?"<>|\r\n]', '_', item['title'])[:100]
    content = engine.export_content(item, fmt)
    return Response(content.encode('utf-8-sig'), media_type='text/plain; charset=utf-8',
                    headers={'Content-Disposition': f"attachment; filename*=UTF-8''{quote(safe+'.'+fmt)}"})


@app.get('/api/export-csv')
def export_csv():
    stream = io.StringIO(newline='')
    writer = csv.writer(stream)
    writer.writerow(['标题', '原作品链接', '时长（秒）', '状态', '文件夹', '文稿'])
    for i in store.items():
        row = [i['title'], i['source_url'], i['duration'], i['status'], i['folder'], i['transcript']]
        writer.writerow(["'"+v if isinstance(v, str) and v.startswith(('=', '+', '-', '@')) else v for v in row])
    return Response(stream.getvalue().encode('utf-8-sig'), media_type='text/csv', headers={'Content-Disposition': 'attachment; filename="lulu-library.csv"'})


@app.patch('/api/settings')
def update_settings(body: dict):
    allowed = set(store.DEFAULTS)
    values = {k: v for k, v in body.items() if k in allowed and isinstance(v, str)}
    if 'save_directory' in values and values['save_directory'] and not Path(values['save_directory']).is_dir():
        raise ValueError('保存文件夹不存在')
    if 'download_mode' in values and values['download_mode'] not in ('audio','video'):raise ValueError('无效的下载方式')
    if 'model' in values and values['model'] not in engine.MODELS:
        raise ValueError('不支持的模型')
    if 'language' in values and values['language'] not in ('auto', 'zh', 'en', 'ja', 'ko'):
        raise ValueError('不支持的语言')
    if 'llm_url' in values or 'llm_model' in values:
        current = store.settings()
        url, model = integrations.normalize_llm_config(
            values.get('llm_url', current['llm_url']),
            values.get('llm_model', current['llm_model']))
        values.update({'llm_url': url, 'llm_model': model})
    for raw, encoded in [('llm_api_key', 'llm_api_key_enc'), ('feishu_secret', 'feishu_secret_enc')]:
        if body.get(raw):
            values[encoded] = secrets.seal(body[raw])
    if body.get('feishu_secret') or ('feishu_app_id' in values and values['feishu_app_id'] != store.settings()['feishu_app_id']):
        values.update({k:'' for k in ('feishu_user_token_enc','feishu_refresh_token_enc','feishu_user_name')})
    store.save_settings(values)
    return public_settings()


@app.post('/api/cookies')
async def cookies(file: UploadFile = File(...)):
    data = await file.read(2*1024*1024)
    await file.close()
    if b'HTTP Cookie File' not in data[:200]:
        raise ValueError('请选择 Netscape 格式的 cookies.txt 文件')
    path = store.DATA / 'cookies.txt'
    path.write_bytes(data)
    store.save_settings({'cookies_path': str(path)})
    return {'ok': True}


@app.delete('/api/cookies')
def clear_cookies():
    path = store.DATA / 'cookies.txt'
    path.unlink(missing_ok=True)
    store.save_settings({'cookies_path': ''})
    return {'ok': True}


@app.post('/api/models/{name}/install')
def install_model(name: str):
    engine.install_model(name)
    return {'ok': True}


@app.delete('/api/models/{name}')
def remove_model(name: str):
    if engine.pending or engine.model_state.get(name, {}).get('state') == 'downloading':
        raise ValueError('请等待当前下载或转录任务结束后卸载模型')
    path = engine.model_dir(name).resolve()
    if path.parent != (store.DATA / 'models').resolve():
        raise ValueError('无效的模型目录')
    if path.exists():
        shutil.rmtree(path)
    engine.model_state.pop(name, None)
    return {'ok': True}


class TextInput(BaseModel):
    text: str = Field(min_length=1, max_length=150000)
    action: str = 'summary'
    instruction: str = ''
    title: str = '处理后的文稿'


@app.post('/api/process-text')
def process_text(body: TextInput):
    try:
        text = integrations.process_text(body.text, body.action, body.instruction)
    except Exception as exc:
        raise ValueError('文案处理失败：' + str(exc)[:700]) from exc
    return store.add(body.title, kind='output', transcript=engine.to_simplified(text), status='done', progress=100, phase='文案处理完成')


@app.get('/api/voices')
def voices():
    result = engine.run_command(['powershell.exe', '-NoProfile', '-File', str(Path(__file__).with_name('voice.ps1'))], timeout=20)
    if result.returncode:
        raise ValueError('无法读取 Windows 语音，请在系统语言设置中安装语音包')
    return json.loads(result.stdout.lstrip('\ufeff') or '[]')


class VoiceInput(BaseModel):
    text: str = Field(min_length=1, max_length=20000)
    voice: str = Field(default='', max_length=500)
    rate: int = Field(default=0, ge=-10, le=10)
    title: str = '新配音'
    source_id: str = ''


@app.post('/api/voice')
def voice(body: VoiceInput):
    if body.source_id:
        require(body.source_id)
    text = engine.to_simplified(body.text)
    output_id = uuid.uuid4().hex
    path = store.DATA / 'outputs' / f'{output_id}.wav'
    request_file = store.DATA / 'outputs' / f'{output_id}.json'
    request_file.write_text(json.dumps(body.model_dump() | {'text': text, 'output': str(path)}, ensure_ascii=False), encoding='utf-8-sig')
    try:
        result = engine.run_command(['powershell.exe', '-NoProfile', '-File', str(Path(__file__).with_name('voice.ps1')), str(request_file)], timeout=300)
        if result.returncode or not path.is_file():
            raise ValueError('Windows 配音失败，请检查语音包是否可用')
        return store.add(body.title, kind='voice', media_path=str(path), transcript=text,
                         duration=engine.media_info(path), status='done', progress=100, phase='配音已生成',
                         metadata={'source_id': body.source_id, 'voice': body.voice, 'rate': body.rate})
    finally:
        request_file.unlink(missing_ok=True)


class ClipInput(BaseModel):
    item_id: str
    start: float = Field(ge=0)
    end: float = Field(gt=0)


@app.post('/api/clip')
def clip(body: ClipInput):
    item = require(body.item_id)
    if not item['media_path'] or body.end <= body.start or (item['duration'] and body.end > item['duration'] + .1):
        raise ValueError('请检查源文件和切片起止时间')
    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg:
        raise ValueError('没有找到 FFmpeg')
    is_video = Path(item['media_path']).suffix.lower() in {'.mp4','.mov','.mkv','.webm','.avi','.m4v'}
    path = store.DATA / 'outputs' / (uuid.uuid4().hex + ('.mp4' if is_video else '.wav'))
    args = [ffmpeg, '-hide_banner', '-loglevel', 'error', '-ss', str(body.start), '-i', item['media_path'], '-t', str(body.end-body.start)]
    args += ['-c:v','libx264','-preset','fast','-c:a','aac','-movflags','+faststart'] if is_video else ['-vn','-c:a','pcm_s16le']
    result = engine.run_command(args + [str(path)], timeout=600)
    if result.returncode:
        raise ValueError('切片失败：' + result.stderr[-500:])
    return store.add(item['title'] + f' · {engine.stamp(body.start)}–{engine.stamp(body.end)}', kind='clip',
                     media_path=str(path), duration=body.end-body.start, status='done', progress=100, phase='切片已保存')


class ExportInput(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=100)


@app.post('/api/feishu/export')
def feishu_export(body: ExportInput):
    try:
        return integrations.feishu_export(body.ids)
    except Exception as exc:
        raise ValueError('飞书导出未完成：' + str(exc)[:700]) from exc


@app.post('/api/obsidian/export')
def obsidian_export(body: ExportInput):
    location = store.settings()['obsidian_path']
    if not location or not Path(location).is_dir():
        raise ValueError('请在设置中填写已有的 Obsidian 仓库文件夹路径')
    directory = Path(location) / 'Lulu'
    directory.mkdir(exist_ok=True)
    for item_id in dict.fromkeys(body.ids):
        item = require(item_id)
        safe = re.sub(r'[\\/:*?"<>|\r\n]', '_', item['title'])[:80]
        (directory / f'{safe}-{item_id[:8]}.md').write_text(engine.export_content(item, 'md'), encoding='utf-8')
    return {'count': len(set(body.ids)), 'path': str(directory)}


dist = store.ROOT / 'dist'
if dist.is_dir():
    app.mount('/', StaticFiles(directory=dist, html=True), name='ui')


if __name__ == '__main__':
    import uvicorn
    port = int(os.environ.get('LULU_PORT', '18793'))
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, log_level='warning'))
    app.state.server = server
    server.run()
