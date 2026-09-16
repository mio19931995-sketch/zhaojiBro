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


def update(item_id, **fields):
    if not fields:
        return
    if not set(fields).issubset(ALLOWED):
        raise ValueError('Invalid field')
    for key in ('segments', 'metadata'):
        if key in fields:
            fields[key] = json.dumps(fields[key], ensure_ascii=False)
    fields['updated_at'] = time.time()
    with connection() as con:
        con.execute(f"UPDATE items SET {','.join(k+'=?' for k in fields)} WHERE id=?", (*fields.values(), item_id))


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
