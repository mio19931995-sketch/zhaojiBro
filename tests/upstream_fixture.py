"""Seed an isolated desktop verification library. No real account or platform."""
import json
import os
from pathlib import Path
import shutil
import time
import uuid

assert os.environ.get('LULU_DATA_DIR'), 'An isolated data directory is required'
from backend import store, live

media = Path(os.environ['FIXTURE_VIDEO'])
item = store.add('工作区演示文稿', kind='file', status='done', progress=100,
                 phase='离线测试素材', media_path=str(media), duration=6,
                 transcript='这是第一句。\n这是第二句。',
                 segments=[{'start': 0, 'end': 3, 'text': '这是第一句。', 'speaker_id': 1},
                           {'start': 3, 'end': 6, 'text': '这是第二句。', 'speaker_id': 2}],
                 metadata={'asr': {'timestamp_kind': 'segment', 'diarization': True}})
rid, sid = uuid.uuid4().hex, uuid.uuid4().hex
folder = live.folder(rid, create=True)
filename = uuid.uuid4().hex[:12] + '-000000.mp4'
shutil.copyfile(media, folder / filename)
recording = {'id': rid, 'host_id': uuid.uuid4().hex, 'name': '离线演示直播', 'title': '合成录像与互动',
             'source': 'https://live.douyin.com/123456', 'started': time.time() - 10,
             'ended': time.time(), 'status': 'saved', 'directory': str(folder),
             'segments': [{'id': sid, 'file': filename, 'duration': 6, 'bytes': media.stat().st_size, 'item_id': ''}],
             'duration': 6, 'bytes': media.stat().st_size, 'comments_state': 'ended',
             'comments_count': 1, 'warning': ''}
store.object_put('live_recording', rid, recording)
(folder / 'comments.jsonl').write_text(json.dumps({'kind': 'chat', 'received_at': '2026-10-25T00:00:00Z',
    'elapsed_seconds': 2, 'nickname': '测试观众', 'text': '这是一条合成弹幕', 'message_id': 'demo1'}, ensure_ascii=False) + '\n', encoding='utf-8')
(folder / 'metrics.jsonl').write_text(json.dumps({'elapsed_seconds': 2, 'viewer_count': 123,
    'like_count': 456, 'live_state': 'live', 'title': '合成录像与互动'}, ensure_ascii=False) + '\n', encoding='utf-8')
print(json.dumps({'item': item['id'], 'recording': rid, 'segment': sid}))
