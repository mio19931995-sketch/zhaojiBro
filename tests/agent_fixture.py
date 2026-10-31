"""Pure-text fixtures for the isolated Windows Agent desktop acceptance flow."""
import json
import os

assert os.environ.get('LULU_DATA_DIR'), 'An isolated data directory is required'
from backend import store

repeat_text = '开场😀请保留。\n这句话需要改。\n中间🧭只读。\n这句话需要改。\n收尾保持原样。'
reference_text = '参考事实：产品重 120 克，售价 199 元。'
timed_text = '第一句保持。\n第二句保留。'
conflict_text = '第一点旧。\n第二点旧。'


def document(title, text, **values):
    return store.add(title, transcript=text, kind='document', status='done', progress=100,
                     phase='离线验收素材', **values)


repeat = document('片段定位演示', repeat_text)
reference = document('只读参考资料', reference_text)
timed = document('字幕逐项修改演示', timed_text, duration=6,
                 segments=[{'start': 0, 'end': 3, 'text': '第一句保持。', 'speaker_id': 1},
                           {'start': 3, 'end': 6, 'text': '第二句保留。', 'speaker_id': 2}])
conflict = document('冲突保护演示', conflict_text)
print(json.dumps({'repeat': repeat['id'], 'reference': reference['id'], 'timed': timed['id'],
                  'conflict': conflict['id'], 'repeat_text': repeat_text,
                  'reference_text': reference_text, 'timed_text': timed_text,
                  'conflict_text': conflict_text}, ensure_ascii=False))
