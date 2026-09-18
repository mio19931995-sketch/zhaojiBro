"""Durable, local-only application state. One SQLite connection per operation."""
import json
import os
import sqlite3
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = Path(os.environ.get('LULU_DATA_DIR', ROOT / 'data')).resolve()
for part in ('media', 'documents', 'models', 'outputs', 'trash'):
    (DATA / part).mkdir(parents=True, exist_ok=True)
DB = DATA / 'library.sqlite3'


def connection():
    con = sqlite3.connect(DB, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def init():
    with connection() as con:
        con.execute('PRAGMA journal_mode=WAL')
        con.execute('''CREATE TABLE IF NOT EXISTS items (
          id TEXT PRIMARY KEY, title TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'media',
          source_url TEXT NOT NULL DEFAULT '', media_path TEXT NOT NULL DEFAULT '',
          duration REAL NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'idle',
          progress REAL NOT NULL DEFAULT 0, phase TEXT NOT NULL DEFAULT '等待开始',
          error TEXT NOT NULL DEFAULT '', transcript TEXT NOT NULL DEFAULT '',
          segments TEXT NOT NULL DEFAULT '[]', folder TEXT NOT NULL DEFAULT '根目录',
          created_at REAL NOT NULL, updated_at REAL NOT NULL, metadata TEXT NOT NULL DEFAULT '{}',
          deleted INTEGER NOT NULL DEFAULT 0
        )''')
        con.execute('CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
        con.execute('CREATE TABLE IF NOT EXISTS drafts (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
        con.execute('CREATE TABLE IF NOT EXISTS folders (name TEXT PRIMARY KEY)')
        con.execute('CREATE TABLE IF NOT EXISTS objects (kind TEXT, id TEXT, value TEXT NOT NULL, PRIMARY KEY(kind,id))')
        con.execute('''CREATE TABLE IF NOT EXISTS snapshots (
          id TEXT PRIMARY KEY, item_id TEXT NOT NULL, created_at REAL NOT NULL,
          reason TEXT NOT NULL, title TEXT NOT NULL, transcript TEXT NOT NULL,
          segments TEXT NOT NULL, characters INTEGER NOT NULL, segment_count INTEGER NOT NULL
        )''')
        con.execute('CREATE INDEX IF NOT EXISTS snapshots_item_created ON snapshots(item_id,created_at DESC)')
        # A stopped job is explicitly resumable; never pretend a partial transcript completed.
        con.execute("UPDATE items SET status='paused', phase='上次处理已中断，可以继续', progress=0 WHERE status IN ('queued','processing')")


def decode(row):
    if not row:
        return None
    obj = dict(row)
    obj['segments'] = json.loads(obj['segments'])
    obj['metadata'] = json.loads(obj['metadata'])
    return obj


def get(item_id):
    with connection() as con:
        return decode(con.execute('SELECT * FROM items WHERE id=? AND deleted=0', (item_id,)).fetchone())


def items():
    with connection() as con:
        return [decode(r) for r in con.execute('SELECT * FROM items WHERE deleted=0 ORDER BY created_at DESC')]


def add(title, **fields):
    item_id = uuid.uuid4().hex
    now = time.time()
    with connection() as con:
        con.execute('INSERT INTO items (id,title,created_at,updated_at) VALUES (?,?,?,?)', (item_id, title, now, now))
    update(item_id, **fields)
    return get(item_id)


ALLOWED = {'title','kind','source_url','media_path','duration','status','progress','phase','error','transcript','segments','folder','metadata','deleted'}


def _update(con, item_id, fields):
    if not fields:
        return
    if not set(fields).issubset(ALLOWED):
        raise ValueError('Invalid field')
    fields = dict(fields)
    for key in ('segments', 'metadata'):
        if key in fields:
            fields[key] = json.dumps(fields[key], ensure_ascii=False)
    fields['updated_at'] = time.time()
    con.execute(f"UPDATE items SET {','.join(k+'=?' for k in fields)} WHERE id=?", (*fields.values(), item_id))


def update(item_id, **fields):
    with connection() as con:
        _update(con, item_id, fields)


def _snapshot(con, item, reason, include_empty=False):
    if not include_empty and not item['transcript'] and not item['segments']:
        return
    con.execute('INSERT INTO snapshots VALUES (?,?,?,?,?,?,?,?,?)', (
        uuid.uuid4().hex, item['id'], time.time(), reason, item['title'], item['transcript'],
        json.dumps(item['segments'], ensure_ascii=False), len(item['transcript']), len(item['segments'])))


def update_with_snapshot(item_id, reason, **fields):
    """Keep the previous content and its replacement in the same transaction."""
    with connection() as con:
        con.execute('BEGIN IMMEDIATE')
        item = decode(con.execute('SELECT * FROM items WHERE id=? AND deleted=0', (item_id,)).fetchone())
        if not item:
            raise ValueError('找不到这份素材')
        if any(key in fields and fields[key] != item[key] for key in ('title', 'transcript', 'segments')):
            _snapshot(con, item, reason)
        _update(con, item_id, fields)


def versions(item_id):
    with connection() as con:
        return [dict(row) for row in con.execute(
            'SELECT id,created_at,reason,title,characters,segment_count FROM snapshots WHERE item_id=? ORDER BY created_at DESC,rowid DESC',
            (item_id,))]


def version(item_id, version_id):
    with connection() as con:
        row = con.execute('SELECT * FROM snapshots WHERE item_id=? AND id=?', (item_id, version_id)).fetchone()
    if not row:
        return None
    result = dict(row)
    result['segments'] = json.loads(result['segments'])
    return result


def restore_version(item_id, version_id):
    with connection() as con:
        con.execute('BEGIN IMMEDIATE')
        item = decode(con.execute('SELECT * FROM items WHERE id=? AND deleted=0', (item_id,)).fetchone())
        saved = con.execute('SELECT * FROM snapshots WHERE item_id=? AND id=?', (item_id, version_id)).fetchone()
        if not item or not saved:
            return None
        _snapshot(con, item, '恢复历史版本前', include_empty=True)
        segments = json.loads(saved['segments'])
        has_content = bool(saved['transcript'] or segments)
        empty_phase = '已恢复空文稿，可重新转录' if item['media_path'] or item['source_url'] else '已恢复空文稿'
        _update(con, item_id, {'title': saved['title'], 'transcript': saved['transcript'],
                             'segments': segments, 'status': 'done' if has_content else 'paused',
                             'progress': 100 if has_content else 0,
                             'phase': '已恢复历史文稿' if has_content else empty_phase, 'error': ''})
        con.execute('DELETE FROM drafts WHERE key=?', (item_id,))
        return decode(con.execute('SELECT * FROM items WHERE id=?', (item_id,)).fetchone())


def trash():
    with connection() as con:
        rows = con.execute('''SELECT id,title,kind,source_url,media_path,duration,status,folder,
          created_at,updated_at,updated_at AS deleted_at,length(transcript) AS characters,
          length(transcript)>0 AS has_transcript FROM items WHERE deleted=1 ORDER BY updated_at DESC''')
        return [dict(row) | {'has_transcript': bool(row['has_transcript'])} for row in rows]


def restore_items(item_ids):
    ids = list(dict.fromkeys(item_ids))
    with connection() as con:
        con.execute('BEGIN IMMEDIATE')
        items = [decode(con.execute('SELECT * FROM items WHERE id=?', (item_id,)).fetchone()) for item_id in ids]
        if any(not item or not item['deleted'] for item in items):
            raise ValueError('所选条目不在回收站，请刷新后重试')
        for item in items:
            fields = {'deleted': 0}
            if item['status'] in ('processing', 'queued'):
                fields.update(status='paused', progress=0, phase='已恢复，点击开始可重新处理')
            _update(con, item['id'], fields)
    return len(ids)


DEFAULTS = {'model': 'base', 'language': 'auto', 'llm_url': 'http://127.0.0.1:11434/v1', 'llm_model': '',
            'theme': 'light', 'cookies_path': '', 'feishu_app_id': '', 'feishu_base': '', 'feishu_table': '',
            'feishu_title_field': '标题', 'feishu_text_field': '转录文稿', 'feishu_url_field': '原作品链接', 'obsidian_path': ''}
DEFAULTS.update({'download_mode': 'audio', 'keep_media': 'false', 'download_cover': 'false',
                 'prefer_subtitles': 'true', 'save_directory': '',
                 'feishu_single_existing': '', 'feishu_single_new': '',
                 'feishu_collection_existing': '', 'feishu_collection_new': ''})


def object_get(kind, object_id):
    with connection() as con:
        row = con.execute('SELECT value FROM objects WHERE kind=? AND id=?', (kind, object_id)).fetchone()
    return json.loads(row['value']) if row else None


def object_put(kind, object_id, value):
    with connection() as con:
        con.execute('INSERT OR REPLACE INTO objects VALUES (?,?,?)', (kind, object_id, json.dumps(value, ensure_ascii=False)))
    return value


def object_list(kind):
    with connection() as con:
        return [json.loads(row['value']) for row in con.execute('SELECT value FROM objects WHERE kind=? ORDER BY rowid DESC', (kind,))]


def object_delete(kind, object_id):
    with connection() as con:
        con.execute('DELETE FROM objects WHERE kind=? AND id=?', (kind, object_id))


def folders():
    with connection() as con:
        return sorted({'根目录'} | {r[0] for r in con.execute('SELECT name FROM folders UNION SELECT folder FROM items WHERE deleted=0')})


def settings():
    result = dict(DEFAULTS)
    with connection() as con:
        result.update({r['key']: json.loads(r['value']) for r in con.execute('SELECT * FROM settings')})
    return result


def save_settings(values):
    with connection() as con:
        for k, v in values.items():
            con.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', (k, json.dumps(v, ensure_ascii=False)))


init()
