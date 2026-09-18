import asyncio
import re
import threading
from urllib.parse import urlparse
import httpx
from . import store, secrets


AGNES_HOSTS = {'apihub.agnes-ai.com', 'apihub.agnes-ai.cn', 'api.agnes-ai.cn'}
AGNES_BASE = 'https://apihub.agnes-ai.com/v1'
AGNES_MODEL = 'agnes-3.0-flash'
LLM_TEST_TIMEOUT = 60
llm_settings_lock = threading.RLock()


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


def normalize_llm_config(url, model):
    """Return a safe OpenAI-compatible base URL and a usable model name."""
    base = str(url or '').strip().rstrip('/')
    try:
        parsed = urlparse(base)
        port = parsed.port
    except ValueError:
        raise ValueError('模型地址或端口格式不正确') from None
    if parsed.scheme not in ('http', 'https') or not parsed.hostname:
        raise ValueError('模型地址必须以 http:// 或 https:// 开头')
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('模型地址只能填写 API 服务地址，不能包含账号、查询参数或页面锚点')
    if any(char in base for char in '\r\n\t'):
        raise ValueError('模型地址不能包含换行或制表符')

    hostname = parsed.hostname.lower().rstrip('.')
    if port is not None and port < 1:
        raise ValueError('模型端口必须在 1–65535 之间')
    authority = '[' + hostname + ']' if ':' in hostname else hostname.encode('idna').decode('ascii')
    if port is not None and port != (443 if parsed.scheme == 'https' else 80):
        authority += ':' + str(port)
    base = parsed.scheme + '://' + authority + parsed.path.rstrip('/')
    # platform.deepseek.com is the account console, not an API host. People often
    # copy the URL while creating a key, so repair it instead of saving a broken URL.
    if hostname in ('platform.deepseek.com', 'www.platform.deepseek.com'):
        base = 'https://api.deepseek.com'
        hostname = 'api.deepseek.com'
    elif hostname == 'api.deepseek.com':
        path = parsed.path.rstrip('/')
        if path.endswith('/chat/completions'):
            path = path[:-len('/chat/completions')]
        # DeepSeek documents both the root base and the compatible /v1 base.
        base = parsed.scheme + '://' + authority + (path if path == '/v1' else '')
    elif hostname == 'platform.agnes-ai.com':
        base = AGNES_BASE
        hostname = 'apihub.agnes-ai.com'
    elif hostname in AGNES_HOSTS and not parsed.path.rstrip('/'):
        base += '/v1'

    normalized_model = str(model or '').strip()
    if hostname == 'api.deepseek.com' and normalized_model.lower() == 'deepseek':
        normalized_model = 'deepseek-flash'
    if hostname in AGNES_HOSTS and normalized_model.lower() in ('', 'agnes', 'agens'):
        normalized_model = AGNES_MODEL
    return base, normalized_model


def llm_origin(url):
    base, _ = normalize_llm_config(url, '')
    parsed = urlparse(base)
    return parsed.scheme + '://' + parsed.netloc


def llm_keyring(cfg):
    """Associate legacy ciphertext only with its existing service, never the target."""
    saved = cfg.get('llm_keys_enc', {})
    keys = {origin: encrypted for origin, encrypted in saved.items()
            if isinstance(origin, str) and isinstance(encrypted, str) and encrypted} if isinstance(saved, dict) else {}
    legacy = cfg.get('llm_api_key_enc', '')
    if isinstance(legacy, str) and legacy:
        try:
            keys.setdefault(llm_origin(cfg.get('llm_url', '')), legacy)
        except ValueError:
            pass
    return keys


def llm_key_flags(cfg):
    keys = llm_keyring(cfg)
    try:
        active = bool(keys.get(llm_origin(cfg.get('llm_url', ''))))
    except ValueError:
        active = False
    return {'has_llm_key': active,
            'has_agnes_key': bool(keys.get('https://apihub.agnes-ai.com')),
            'has_deepseek_key': bool(keys.get('https://api.deepseek.com'))}


def llm_key_settings(cfg, base, supplied_key=''):
    keys = llm_keyring(cfg)
    origin = llm_origin(base)
    if not isinstance(supplied_key, str):
        raise ValueError('API Key 格式不正确')
    if supplied_key.strip():
        keys[origin] = secrets.seal(supplied_key.strip())
    return {'llm_keys_enc': keys, 'llm_api_key_enc': keys.get(origin, '')}


def llm_headers(cfg, base, supplied_key=''):
    key = supplied_key.strip()
    if not key:
        try:
            key = secrets.unseal(llm_keyring(cfg).get(llm_origin(base), ''))
        except Exception:
            raise ValueError('无法读取当前服务保存的 API Key，请重新填写密钥') from None
    if not key and urlparse(base).hostname in AGNES_HOSTS:
        raise ValueError('请填写当前 Agnes API 服务的 API Key；其他服务的密钥不会用于 Agnes')
    return {'Authorization': 'Bearer ' + key} if key else {}


def llm_chat_endpoint(base):
    value = base.rstrip('/')
    if value.lower().endswith('/chat/completions'):
        return value
    return value + '/chat/completions'


def raise_llm_error(response):
    status = response.status_code
    if status in (401, 403):
        raise ValueError('文本模型认证或权限不足，请检查 API Key 是否正确、有效且有权调用此模型')
    if status == 402:
        raise ValueError('文本模型账户额度不足，请检查余额或套餐额度')
    if status in (404, 405):
        raise ValueError('文本模型接口地址或模型名称不正确，请核对 API 地址和该服务支持的模型 ID')
    if status == 429:
        raise ValueError('文本模型请求过于频繁或账户额度不足，请稍后重试并检查服务额度')
    if 300 <= status < 400:
        raise ValueError('文本模型接口返回了重定向，请核对完整 API 地址后重试')
    if status >= 500:
        raise ValueError('文本模型服务暂时不可用，请稍后重试')
    raise ValueError(f'文本模型请求失败（HTTP {status}），请检查模型名称与服务配置')


def llm_content(response):
    if not 200 <= response.status_code < 300:
        raise_llm_error(response)
    try:
        payload = response.json()
    except ValueError:
        raise ValueError('文本模型返回的内容不是有效 JSON，请检查 API 地址是否误填为网页') from None
    if isinstance(payload, dict) and 'error' in payload and payload['error'] is not None:
        raise ValueError('文本模型服务返回了错误，请检查模型权限、账户额度和服务配置')
    choices = payload.get('choices') if isinstance(payload, dict) else None
    result = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
    if result.get('finish_reason') == 'length':
        raise ValueError('模型已响应，但输出达到长度限制；请缩短输入或调整模型后重试')
    message = result.get('message')
    content = message.get('content') if isinstance(message, dict) else None
    if not isinstance(content, str) or not content.strip():
        raise ValueError('文本模型没有返回有效正文，请检查模型名称与服务兼容性')
    return content.strip()


def llm_post(client, base, headers, payload):
    try:
        return llm_content(client.post(llm_chat_endpoint(base), headers=headers, json=payload))
    except httpx.TimeoutException:
        raise ValueError('文本模型请求超时，请检查网络或稍后重试') from None
    except (httpx.RequestError, httpx.InvalidURL):
        raise ValueError('无法连接文本模型服务，请检查 API 地址和网络连接') from None


async def test_llm_connection(url, model, supplied_key=''):
    base, model = normalize_llm_config(url, model)
    if not model:
        raise ValueError('请填写文本模型名称')
    headers = llm_headers(store.settings(), base, supplied_key)
    async def request():
        async with httpx.AsyncClient(timeout=LLM_TEST_TIMEOUT, trust_env=False, follow_redirects=False) as client:
            response = await client.post(llm_chat_endpoint(base), headers=headers, json={'model': model,
                'messages': [{'role': 'user', 'content': '只回复 OK'}],
                'stream': False, 'max_tokens': 1024, 'temperature': 0})
            return llm_content(response)
    try:
        # The overall deadline includes connect/read/write; cancelling also closes the request.
        await asyncio.wait_for(request(), timeout=LLM_TEST_TIMEOUT)
    except (asyncio.TimeoutError, httpx.TimeoutException):
        raise ValueError('文本模型连接测试超时，请检查网络或稍后重试') from None
    except (httpx.RequestError, httpx.InvalidURL):
        raise ValueError('无法连接文本模型服务，请检查 API 地址和网络连接') from None
    return {'ok': True, 'model': model, 'base_url': base, 'message': '连接成功，模型已返回有效响应'}


def process_text(text, action, instruction=''):
    cfg = store.settings()
    base, model = normalize_llm_config(cfg['llm_url'], cfg['llm_model'])
    if not model:
        raise ValueError('请先在 AI 大模型中配置文本模型名称和服务地址')
    headers = llm_headers(cfg, base)
    outputs = []
    prompt = PROMPTS.get(action, PROMPTS['rewrite'])
    if instruction.strip():
        prompt += '\n用户补充要求：' + instruction.strip()
    with httpx.Client(timeout=300, trust_env=False, follow_redirects=False) as client:
        for part in chunks(text):
            outputs.append(llm_post(client, base, headers,
                {'model': model, 'messages': [{'role': 'system', 'content': prompt}, {'role': 'user', 'content': part}],
                 'temperature': .3, 'max_tokens': 8192, 'stream': False}))
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
