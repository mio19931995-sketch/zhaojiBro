"""Seed only the isolated native Electron test library."""
import json
import os
from pathlib import Path

assert os.environ.get('LULU_DATA_DIR'), 'An isolated data directory is required'
from backend import engine, store, wechat

live_url = os.environ.get('LULU_WECHAT_LIVE_URL', '')
item = store.add('旧版失败的公众号任务', source_url=live_url or 'https://mp.weixin.qq.com/s/synthetic_article',
                 status='error', error='ERROR: Unsupported URL',
                 metadata={'download_only': True, 'action': 'assets', 'options': {'cover': bool(live_url)}})
if not live_url:
    wechat.fetch_html = lambda url, check: (Path(__file__).parent / 'fixtures/wechat_article.html').read_text(encoding='utf-8')
    engine.process(item['id'])
print(json.dumps({'id': item['id'], 'live': bool(live_url)}))
