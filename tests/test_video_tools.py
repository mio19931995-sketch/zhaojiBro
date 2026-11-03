"""Real FFmpeg/Pillow regression tests, isolated from the user's library."""
import hashlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

os.environ['LULU_DATA_DIR'] = tempfile.mkdtemp(prefix='lulu-video-tools-test-')

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from PIL import Image

from backend import store, video_tools as video

app = FastAPI()
app.include_router(video.router)


@app.exception_handler(ValueError)
def invalid(request, exc):
    return JSONResponse({'detail': str(exc)}, 400)


client = TestClient(app)
BASE = '/api/video-tools'


@pytest.fixture(autouse=True)
def database(tmp_path, monkeypatch):
    assert video.shutdown()
    monkeypatch.setattr(store, 'DATA', tmp_path)
    monkeypatch.setattr(store, 'DB', tmp_path / 'library.sqlite3')
    store.init()
    video.recover()
    yield
    assert video.shutdown()


@pytest.fixture(scope='module')
def media(tmp_path_factory):
    if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
        pytest.skip('FFmpeg and FFprobe required')
    directory = tmp_path_factory.mktemp('video-tools-media')
    path = directory / '原始视频.mp4'
    args = ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-f', 'lavfi', '-i',
        'color=c=teal:s=320x180:r=30:d=3', '-f', 'lavfi', '-i', 'sine=frequency=440:duration=3',
        '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-shortest', str(path)]
    result = subprocess.run(args, capture_output=True, timeout=30, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    assert result.returncode == 0, result.stderr.decode(errors='replace')
    return path


def item(media):
    return store.add('测试视频', media_path=str(media), duration=3)


def wait_job(job_id, statuses=('done', 'error', 'cancelled', 'interrupted'), timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = next(j for j in client.get(BASE + '/jobs').json() if j['id'] == job_id)
        if current['status'] in statuses:
            return current
        time.sleep(.05)
    raise AssertionError('Video job did not settle: ' + str(current))


def start(source, **kwargs):
    response = client.post(BASE + '/jobs', json={'item_id': source['id'], **kwargs})
    assert response.status_code == 200, response.text
    return response.json()


def probe(path):
    result = subprocess.run(['ffprobe', '-v', 'error', '-count_frames', '-show_streams', '-show_format',
        '-of', 'json', str(path)], capture_output=True, timeout=20)
    assert result.returncode == 0
    return json.loads(result.stdout)


def upload_image(color=(255, 0, 0, 255)):
    buffer = io.BytesIO()
    Image.new('RGBA', (40, 20), color).save(buffer, 'PNG')
    response = client.post(BASE + '/watermark', files={'file': ('../../logo.png', buffer.getvalue(), 'image/png')})
    assert response.status_code == 200, response.text
    return response.json()


def test_real_drop_cadence_audio_resolution_duration_and_original_preserved(media):
    source = item(media)
    original_hash = hashlib.sha256(media.read_bytes()).hexdigest()
    info = client.get(BASE + '/source/' + source['id']).json()
    assert (info['base_fps'], info['width'], info['height'], info['has_audio']) == (30, 320, 180, True)
    completed = wait_job(start(source, drop_per_second=6)['id'])
    assert completed['status'] == 'done', completed
    assert completed['progress'] == 100
    assert 'source_path' not in completed and 'output_path' not in completed
    result = store.get(completed['output_item_id'])
    assert result['kind'] == 'clip'
    assert result['metadata']['source_id'] == source['id']
    streams = probe(result['media_path'])['streams']
    picture = next(s for s in streams if s['codec_type'] == 'video')
    audio = next(s for s in streams if s['codec_type'] == 'audio')
    assert picture['avg_frame_rate'] == '24/1'
    assert int(picture['nb_read_frames']) == 72
    assert (picture['width'], picture['height']) == (320, 180)
    assert abs(float(picture['duration']) - 3) < .01
    assert abs(float(audio['duration']) - 3) < .03
    assert hashlib.sha256(media.read_bytes()).hexdigest() == original_hash
    download = client.get(completed['download_url'])
    assert download.status_code == 200 and len(download.content) == Path(result['media_path']).stat().st_size
    assert 'attachment' in download.headers['content-disposition']


def test_chinese_special_character_watermark_real_preview_and_export(media):
    source = item(media)
    watermark = {'type': 'text', 'text': "满满超人 100% : ' [ ] \\ ; ,", 'size': 12, 'x': 0, 'y': 0,
                 'color': '#ff0000', 'opacity': 1}
    preview = client.post(BASE + '/preview', json={'item_id': source['id'], 'watermark': watermark, 'seconds': 1})
    assert preview.status_code == 200, preview.text if preview.status_code != 200 else ''
    image = Image.open(io.BytesIO(preview.content)).convert('RGB')
    assert image.size == (320, 180)
    # Red glyphs are visible and text cannot inject FFmpeg filter syntax.
    assert sum(r > 150 and r > g * 2 and r > b * 2 for r, g, b in image.get_flattened_data()) > 100
    assert not list((store.DATA / 'video-tools' / 'previews').glob('*/preview.png'))
    result = wait_job(start(source, drop_per_second=0, watermark=watermark)['id'])
    assert result['status'] == 'done', result
    output = probe(store.get(result['output_item_id'])['media_path'])
    assert next(s for s in output['streams'] if s['codec_type'] == 'video')['nb_read_frames'] == '90'


def test_image_watermark_size_position_opacity_and_path_safety(media):
    source = item(media)
    logo = upload_image()
    assert Image.open(io.BytesIO(client.get(logo['url']).content)).size == (40, 20)
    base = {'item_id': source['id'], 'seconds': .5, 'watermark': {'type': 'image', 'image_id': logo['id'],
        'x': 1, 'y': 1, 'size': 20, 'opacity': .5}}
    response = client.post(BASE + '/preview', json=base)
    assert response.status_code == 200, response.text if response.status_code != 200 else ''
    image = Image.open(io.BytesIO(response.content)).convert('RGB')
    # Logo scales to height 36, width 72, anchored at bottom right. Alpha blends with teal.
    outside = image.getpixel((100, 100))
    inside = image.getpixel((300, 160))
    assert outside[0] < 30 and 110 < inside[0] < 150 and inside[1] < outside[1]
    result = wait_job(start(source, drop_per_second=6, watermark=base['watermark'])['id'])
    assert result['status'] == 'done', result
    invalid = client.post(BASE + '/jobs', json={'item_id': source['id'], 'watermark': {'type': 'image', 'image_id': '../secret'}})
    assert invalid.status_code == 400
    rejected = client.post(BASE + '/watermark', files={'file': ('logo.svg', b'<svg><script>bad()</script></svg>', 'image/svg+xml')})
    assert rejected.status_code == 400
    assert client.get(BASE + '/watermark/not-an-id').status_code == 400


@pytest.mark.parametrize('values,code', [({'drop_per_second':30},400), ({'drop_per_second':-1},422),
    ({'drop_per_second':1.5},422), ({'drop_per_second':True},422),
    ({'watermark':{'type':'text','text':'   '}},400), ({'watermark':{'x':2}},422),
    ({'watermark':{'opacity':1.1}},422), ({'watermark':{'color':'red;malicious'}},422)])
def test_invalid_parameters_do_not_create_jobs(media, values, code):
    source = item(media)
    assert client.post(BASE + '/jobs', json={'item_id':source['id'], **values}).status_code == code
    assert not client.get(BASE + '/jobs').json()


def test_invalid_preview_time_and_non_finite_values(media):
    source = item(media)
    assert client.post(BASE + '/preview', json={'item_id':source['id'],'seconds':3}).status_code == 400
    response = client.post(BASE + '/preview', content='{"item_id":"'+source['id']+'","seconds":NaN}', headers={'Content-Type':'application/json'})
    assert response.status_code in (400, 422)


def test_keep_one_frame_each_second_still_preserves_video_and_audio_duration(media):
    source = item(media)
    result = wait_job(start(source, drop_per_second=29)['id'])
    assert result['status'] == 'done', result
    actual = probe(store.get(result['output_item_id'])['media_path'])
    picture = next(s for s in actual['streams'] if s['codec_type'] == 'video')
    assert picture['avg_frame_rate'] == '1/1' and picture['nb_read_frames'] == '3'
    assert abs(float(picture['duration']) - 3) < .01
    assert abs(float(next(s for s in actual['streams'] if s['codec_type'] == 'audio')['duration']) - 3) < .03


def test_actual_encoder_cancellation_is_reaped_without_success_output(media, monkeypatch):
    source = item(media)
    original = video._args
    def slow(*args, **kwargs):
        command = original(*args, **kwargs)
        command.insert(1, '-re')
        return command
    monkeypatch.setattr(video, '_args', slow)
    job = start(source, drop_per_second=6)
    wait_job(job['id'], statuses=('running',))
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not video._active[job['id']].process:
        time.sleep(.02)
    process = video._active[job['id']].process
    assert process is not None
    assert client.post(BASE + '/jobs/' + job['id'] + '/cancel').status_code == 200
    result = wait_job(job['id'])
    assert result['status'] == 'cancelled', result
    assert process.poll() is not None
    assert not result.get('output_item_id')
    assert not list((store.DATA / 'video-tools' / 'jobs' / job['id']).glob('*.mp4'))
    assert client.get(BASE + '/jobs/' + job['id'] + '/download').status_code == 404


def test_encoder_failure_is_not_success_and_recovery_marks_stopped_tasks(media, monkeypatch):
    source = item(media)
    monkeypatch.setattr(video, '_args', lambda *a, **kw: [video._command('ffmpeg'), '-invalid-lulu-test-option'])
    result = wait_job(start(source, drop_per_second=6)['id'])
    assert result['status'] == 'error' and result['error'] and not result.get('download_url')
    job_id = uuid.uuid4().hex
    store.object_put(video.JOB_KIND, job_id, {'id':job_id,'status':'running'})
    directory = store.DATA / 'video-tools' / 'jobs' / job_id
    directory.mkdir(parents=True)
    (directory / 'export.partial.mp4').write_bytes(b'not finished')
    video.recover()
    assert store.object_get(video.JOB_KIND, job_id)['status'] == 'interrupted'
    assert not (directory / 'export.partial.mp4').exists()
    assert store.object_get(video.JOB_KIND, result['id'])['status'] == 'error'


def test_fractional_fps_and_video_without_audio(tmp_path):
    path = tmp_path / 'silent.mp4'
    generated = subprocess.run(['ffmpeg', '-hide_banner','-loglevel','error','-y','-f','lavfi','-i',
        'testsrc2=s=320x180:r=30000/1001:d=2','-c:v','libx264','-pix_fmt','yuv420p',str(path)],capture_output=True,timeout=30)
    assert generated.returncode == 0
    source = item(path)
    info = client.get(BASE + '/source/' + source['id']).json()
    assert info['base_fps'] == 30 and info['non_integer_fps'] and info['normalized'] and not info['has_audio']
    result = wait_job(start(source, drop_per_second=6)['id'])
    assert result['status'] == 'done', result
    actual = probe(store.get(result['output_item_id'])['media_path'])
    assert not any(s['codec_type'] == 'audio' for s in actual['streams'])
    assert abs(float(actual['format']['duration']) - info['duration']) < 1 / 24 + .01


def test_partial_final_second_at_one_fps_does_not_extend_video(media, tmp_path):
    path = tmp_path / '非整数秒.mp4'
    generated = subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y',
        '-i', str(media), '-t', '2.2', '-c:v', 'libx264', '-c:a', 'aac', str(path)],
        capture_output=True, timeout=30)
    assert generated.returncode == 0
    source = item(path)
    result = wait_job(start(source, drop_per_second=29)['id'])
    assert result['status'] == 'done', result
    actual = probe(store.get(result['output_item_id'])['media_path'])
    picture = next(s for s in actual['streams'] if s['codec_type'] == 'video')
    audio = next(s for s in actual['streams'] if s['codec_type'] == 'audio')
    assert picture['nb_read_frames'] == '3'
    assert abs(float(picture['duration']) - 2.2) < .01
    assert abs(float(audio['duration']) - 2.2) < .03
    assert abs(float(actual['format']['duration']) - 2.2) < .03
