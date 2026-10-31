"""Synthetic text-only history for the native Windows Agent runtime checks."""
import hashlib
import json
import os
import time
import uuid

assert os.environ.get('LULU_DATA_DIR'), 'An isolated data directory is required'
from backend import store


def document(title, text):
    return store.add(title, transcript=text, kind='document', status='done',
                     progress=100, phase='离线 Agent 运行验收')


def conversation(item, history=False):
    now = time.time()
    messages = []
    if history:
        for index in range(8):
            messages.extend([
                {'id': f'user-{item["id"]}-{index}', 'role': 'user',
                 'text': f'历史要求_{index:02d}。' + '保留专名和价格199元，先讨论素材，暂不执行外部动作。' * 8,
                 'run_id': f'historical-{index}', 'created_at': now - 100 + index * 2},
                {'id': f'assistant-{item["id"]}-{index}', 'role': 'assistant',
                 'text': f'历史答复_{index:02d}。' + '这是一份用于核对上下文连续性的合成讨论资料，不表示已经保存任何新文稿。' * 9,
                 'run_id': f'historical-{index}', 'created_at': now - 99 + index * 2},
            ])
    value = {'id': uuid.uuid4().hex, 'source_id': item['id'], 'title': item['title'],
             'text': item['transcript'], 'source_hash': hashlib.sha256(json.dumps(
                 [item['title'], item['transcript'], item['segments']], ensure_ascii=False).encode()).hexdigest(),
             'revision': 0, 'messages': messages, 'references': [], 'run_id': '',
             'instruction': '', 'preset': '', 'save_generation': 0, 'created_at': now, 'updated_at': now}
    store.object_put('ai_workspace', value['id'], value)
    return value


context = document('自动压缩演示', '这是压缩验收原文。价格固定199元。')
manual = document('手动压缩演示', '手动压缩只整理聊天记录，原文保持不变。')
tools = document('工具调用演示', '工具可以查找参考资料，这篇原文暂时保留。')
reference = document('可检索参考资料', '可检索参考：产品重量120克，价格199元。')
skill = document('技能方法演示', '使用指定技能核对价格199元，不自动保存来源。')
brand = document('品牌翻译演示', '松果X1重120克。米家 Pro售价299元。松果X1将于11月2日发布。')
values = {'context': context, 'manual': manual, 'tools': tools, 'reference': reference, 'skill': skill, 'brand': brand}
for name in ('context', 'manual', 'tools', 'skill', 'brand'):
    values[name]['workspace_id'] = conversation(values[name], name in ('context', 'manual'))['id']
print(json.dumps(values, ensure_ascii=False))
