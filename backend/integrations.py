import re
from urllib.parse import urlparse
import httpx
from . import store, secrets


PROMPTS = {
    'correct': '你是中文编辑。纠正错别字和口语标点，保持原意与信息完整，不增添事实。保留所有数字、专有名词和链接。只返回修正后的正文。',
    'summary': '将以下逐字稿整理成中文摘要，包含核心观点、具体细节与可执行要点。严格依据原文，不增添事实。',
    'rewrite': '将以下逐字稿改写为适合发布的中文文案，去掉无意义的口头重复，保持原文事实、数据、链接和观点。只返回正文。',
    'translate': '将以下正文翻译为自然的简体中文。如果原文已经是中文，则翻译为英文。保持段落、数字和链接。只返回译文。',
    'outline': '从原文提炼中文结构化大纲：主题、论点、例子、结论。标明原文没有给出的信息，不补造。',
}


def chunks(text, maximum=5000):
    # Never truncate a long document. Split at paragraph boundaries where possible.
    while text:
        end = min(len(text), maximum)
        if end < len(text):
            boundary = text.rfind('\n', 0, end)
            if boundary > maximum // 2:
                end = boundary + 1
        yield text[:end]
        text = text[end:]


def process_text(text, action, instruction=''):
    cfg = store.settings()
    if not cfg['llm_model']:
        raise ValueError('请先在 AI 大模型中配置文本模型名称和服务地址')
    base = cfg['llm_url'].rstrip('/')
    if urlparse(base).scheme not in ('http', 'https'):
        raise ValueError('模型地址必须以 http:// 或 https:// 开头')
    headers = {}
    key = secrets.unseal(cfg.get('llm_api_key_enc', ''))
    if key:
        headers['Authorization'] = 'Bearer ' + key
    outputs = []
    prompt = PROMPTS.get(action, PROMPTS['rewrite'])
    if instruction.strip():
        prompt += '\n用户补充要求：' + instruction.strip()
    with httpx.Client(timeout=300, trust_env=False) as client:
        for part in chunks(text):
            response = client.post(base + '/chat/completions', headers=headers,
                json={'model': cfg['llm_model'], 'messages': [{'role': 'system', 'content': prompt}, {'role': 'user', 'content': part}],
                      'temperature': .3, 'max_tokens': 8192})
            response.raise_for_status()
            result = response.json()['choices'][0]
            if result.get('finish_reason') == 'length':
                raise ValueError('模型输出达到长度限制，原文已保留；请缩短输入或换用更长上下文的模型')
            content = result['message']['content']
            if not isinstance(content, str) or not content.strip():
                raise ValueError('模型没有返回有效正文')
            outputs.append(content.strip())
    return '\n\n'.join(outputs)


def feishu_export(item_ids):
    cfg = store.settings()
    app_id = cfg['feishu_app_id']
    secret = secrets.unseal(cfg.get('feishu_secret_enc', ''))
    base = cfg['feishu_base'].strip()
    match = re.search(r'/base/([a-zA-Z0-9]+)', base)
    if match:
        base = match.group(1)
    table = cfg['feishu_table'].strip()
    if not all([app_id, secret, base, table]):
        raise ValueError('请先在飞书知识库设置中填写 App ID、App Secret、Base Token 和 Table ID')
    if not re.fullmatch('[a-zA-Z0-9]+', base) or not re.fullmatch('[a-zA-Z0-9]+', table):
        raise ValueError('Base Token 或 Table ID 格式不正确')
    selected = [store.get(i) for i in dict.fromkeys(item_ids)]
    if not selected or any(i is None for i in selected):
        raise ValueError('请选择有效文稿')
    records = []
    for item in selected:
        if not item['transcript']:
            raise ValueError(f'“{item["title"]}”还没有正文，请先完成转录')
        if len(item['transcript']) > 90000:
            raise ValueError('文稿过长，请拆分后导出，避免飞书单元格截断')
        fields = {cfg['feishu_title_field']: item['title'], cfg['feishu_text_field']: item['transcript']}
        if item['source_url'] and cfg['feishu_url_field']:
            fields[cfg['feishu_url_field']] = {'link': item['source_url'], 'text': '原作品链接'}
        records.append({'fields': fields})
    # No retry after an uncertain write: otherwise duplicate rows can be created.
    with httpx.Client(timeout=90) as client:
        auth = client.post('https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal',
                           json={'app_id': app_id, 'app_secret': secret})
        auth.raise_for_status()
        payload = auth.json()
        if payload.get('code') != 0:
            raise ValueError('飞书授权失败：' + payload.get('msg', '未知错误'))
        response = client.post(f'https://open.feishu.cn/open-apis/bitable/v1/apps/{base}/tables/{table}/records/batch_create',
            headers={'Authorization': 'Bearer ' + payload['tenant_access_token']}, json={'records': records})
        response.raise_for_status()
        result = response.json()
        if result.get('code') != 0:
            raise ValueError('飞书导出失败：' + result.get('msg', '请检查表格权限和字段类型'))
        return {'count': len(result.get('data', {}).get('records', []))}
