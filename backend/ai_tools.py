"""Closed, user-enabled Lulu tools. Models cannot grant themselves capabilities.

Reads use the run's selected scope. Other workflows are explicit handoffs.
Persistent writes produce reviewable actions; only the local confirmation API
commits them after checking the frozen document and skill generations again.
"""
import ipaddress
import json
import time
import uuid
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, StrictInt

from . import ai_edits, ai_skills, engine, store

router = APIRouter()
MAX_STEPS = 8
PAGE_SIZE = 24000


def spec(identity, title, description, properties=None, effect='read'):
    return {'name': identity, 'title': title, 'label': title, 'description': description,
            'effect': effect, 'read_only': effect == 'read',
            'parameters': {'type': 'object', 'properties': properties or {},
                           'required': list(properties or {}), 'additionalProperties': False}}


def string(limit=4096):
    return {'type': 'string', 'maxLength': limit}


TOOLS = [
    spec('search_documents', '查找文稿', '在本机文稿标题和正文中匹配连续子串，返回标题和编号，不修改文稿。query只填原文中的短关键词（如“发布资料”），不要填写“标题包含…的文稿”等自然语言查询句。未找到时可缩短关键词重试。', {'query': string()}),
    spec('read_document', '阅读文稿', '分页读取本轮已发现的文稿；当前文稿仅返回本轮选中范围，不能越界。', {'document_id': string(64), 'offset': {'type': 'integer', 'minimum': 0, 'maximum': 8000000}}),
    spec('read_targets', '读取当前范围', '读取发送时冻结的选中正文和只读引用，不扩大修改范围。'),
    spec('list_tasks', '查看任务', '查询当前会话最近 20 个任务的真实状态。'),
    spec('inspect_link', '识别链接', '只在本地识别公开作品或主页链接，不访问网页。', {'url': string()}),
    spec('prepare_voice', '准备配音', '准备当前范围的配音入口；仍需用户确认音色后生成。', effect='handoff'),
    spec('prepare_transcription', '准备转录', '准备内容提取入口，空链接表示选择本地文件；工具不会下载或识别。', {'url': string()}, 'handoff'),
    spec('prepare_collection', '准备主页采集', '准备抖音主页采集入口，等待用户确认登录和配置；不会自动抓取。', {'url': string(), 'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100}}, 'handoff'),
    spec('load_skill', '读取技能', '按需读取本轮启用技能的方法；技能不能扩大工具权限。', {'name': string(64)}),
    spec('read_skill_reference', '读取技能参考', '只读取已加载技能包内的 Markdown 资料，不能读取外部文件。', {'name': string(64), 'path': string(240)}),
    spec('search_skills', '查找本地技能', '搜索本机和内置技能，不联网搜索。', {'query': string()}),
    spec('prepare_skill_install', '打开技能管理', '提供本地技能导入入口，不自动安装或联网。', effect='handoff'),
    spec('list_skill_drafts', '查看技能草稿', '列出已预览、尚未安装的技能，供明确选择。'),
    spec('create_document', '另存文稿', '为用户要求的文章准备另存预览。用户确认后才创建，当前稿不变。', {'title': string(500), 'markdown': string(150000)}, 'confirm'),
    spec('create_skill_draft', '创建技能草稿', '把用户要求的方法准备为待安装草稿，用户确认后保存草稿，不自动安装。', {'name': string(64), 'description': string(1600), 'instructions': string(200000)}, 'confirm'),
    spec('install_skill', '安装已预览技能', '仅准备已预览包或内置技能的安装确认；name与token二选一，另一项为空。不自动替换同名不同内容。', {'name': string(64), 'token': string(64)}, 'confirm'),
]
BY_NAME = {tool['name']: tool for tool in TOOLS}


def api():
    from . import ai_workspace
    return ai_workspace


def catalog():
    return TOOLS


@router.get('/tools')
def listing():
    return {'tools': TOOLS, 'entries': TOOLS, 'max_steps': MAX_STEPS}


def schemas(allowed_tools):
    if len(allowed_tools) != len(set(allowed_tools)) or set(allowed_tools) - BY_NAME.keys():
        raise ValueError('工具清单包含重复或未开放的能力')
    return [{'type': 'function', 'function': {key: tool[key] for key in ('name', 'description', 'parameters')}}
            for tool in TOOLS if tool['name'] in allowed_tools]


def validate(call, allowed_tools=None):
    if type(call) is not dict:
        raise ValueError('工具调用必须为对象')
    identity, arguments = call.get('name'), call.get('arguments')
    if not isinstance(identity, str) or identity not in BY_NAME or (allowed_tools is not None and identity not in allowed_tools):
        raise ValueError('模型请求了未启用的工具，未执行')
    if not isinstance(call.get('id'), str) or not call['id'] or len(call['id']) > 200:
        raise ValueError('工具调用缺少有效编号')
    properties = BY_NAME[identity]['parameters']['properties']
    if type(arguments) is not dict or set(arguments) != set(properties):
        raise ValueError('工具参数不完整或含有额外字段，未执行')
    for key, field in properties.items():
        value = arguments[key]
        if field['type'] == 'string':
            if not isinstance(value, str) or len(value) > field['maxLength']:
                raise ValueError('工具文字参数无效或超过大小限制')
        elif type(value) is not int or not field['minimum'] <= value <= field['maximum']:
            raise ValueError('工具整数参数超出允许范围')
    return call


def target(run):
    return {'document_id': run['source_id'], 'title': run['title'], 'text': run['input_text'],
            'scope': run.get('scope', 'document'), 'references': [{'id': ref['id'], 'title': ref['title'], 'text': ref['text']} for ref in run.get('references', [])]}


def valid_url(value, optional=False):
    if not value and optional:
        return ''
    try:
        parts = urlsplit(value)
        if (parts.scheme not in {'http', 'https'} or not parts.hostname or parts.username or parts.password
                or parts.port not in (None, 80, 443) or any(c.isspace() for c in value)):
            raise ValueError()
        host = parts.hostname.lower()
        if host in {'localhost', 'localhost.localdomain'} or host.endswith('.local'):
            raise ValueError()
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            raise ValueError()
    except ValueError:
        raise ValueError('请提供不含凭据的完整公开 http/https 链接') from None
    return value


def inspect_link(value):
    value = valid_url(value)
    parsed = urlsplit(value)
    host, path = parsed.hostname.lower(), parsed.path
    platform = ('douyin' if host == 'douyin.com' or host.endswith('.douyin.com') else
                'bilibili' if host == 'bilibili.com' or host.endswith('.bilibili.com') or host == 'b23.tv' else
                'xiaohongshu' if host == 'xiaohongshu.com' or host.endswith('.xiaohongshu.com') or host == 'xhslink.com' else
                'youtube' if host in {'youtube.com', 'www.youtube.com', 'youtu.be'} else 'other')
    kind = 'homepage' if platform == 'douyin' and path.startswith('/user/') else 'work_or_link'
    return {'platform': platform, 'kind': kind, 'url': value, 'modes': ['collection'] if kind == 'homepage' else [],
            'message': '已在本地识别链接，尚未访问网页'}


def pending_action(run, call, preview):
    actions = run.setdefault('actions', [])
    existing = next((value for value in actions if value.get('call_id') == call['id']), None)
    if existing:
        if existing['tool'] != call['name'] or existing['arguments'] != call['arguments']:
            raise ValueError('同一工具调用编号对应不同操作，未执行')
        action = existing
    else:
        action = {'id': uuid.uuid4().hex, 'call_id': call['id'], 'tool': call['name'], 'label': BY_NAME[call['name']]['title'],
                  'state': 'pending', 'preview': preview, 'arguments': call['arguments'], 'created_at': time.time()}
        actions.append(action)
    return {'state': 'needs_input', 'action': 'confirm', 'action_id': action['id'], 'label': action['label'],
            'message': '已准备预览；尚未保存或安装，请用户确认后执行。', 'preview': action['preview']}


def execute(call, run):
    config = run.get('agent_config', {})
    validate(call, config.get('allowed_tools', []))
    if not config.get('tools_enabled'):
        raise ValueError('本轮工具未启用')
    ai_skills.validate_loaded(run)
    identity, args = call['name'], call['arguments']
    if identity in {'create_document', 'create_skill_draft'}:
        # Normalize model-authored output once, before freezing the preview and
        # its arguments. Local imports and read-only source material stay exact.
        args = {key: value if key == 'name' else engine.simplify_with_brands(value, run.get('translation_brands', [])) for key, value in args.items()}
        call = {**call, 'arguments': args}
        validate(call, config.get('allowed_tools', []))
    if identity == 'read_targets':
        if run['source_id']:
            api().require_source(run['source_id'])
        for reference in run.get('references', []):
            if reference.get('source_id'):
                api().require_source(reference['source_id'])
        return {'state': 'completed', 'message': '已读取本轮冻结的范围与只读参考', **target(run)}
    if identity == 'search_documents':
        with store.connection() as con:
            rows = con.execute("SELECT id,title,updated_at FROM items WHERE deleted=0 AND status NOT IN ('queued','processing') AND transcript<>'' AND instr(lower(title||char(10)||transcript),lower(?))>0 ORDER BY updated_at DESC LIMIT 20", (args['query'].strip(),)).fetchall()
        values = [dict(row) for row in rows]
        discovered = run.setdefault('discovered_documents', [])
        discovered.extend(row['id'] for row in values if row['id'] not in discovered)
        return {'state': 'completed', 'message': f'找到 {len(values)} 篇文稿（最多 20 篇）', 'documents': values}
    if identity == 'read_document':
        document_id = args['document_id']
        refs = [ref for ref in run.get('references', []) if ref.get('source_id') == document_id]
        if document_id == run['source_id']:
            if document_id:
                api().require_source(document_id)
            text, title = run['input_text'], run['title']
        elif refs:
            api().require_source(document_id)
            text, title = '\n\n'.join(ref['text'] for ref in refs), refs[0]['title']
        elif document_id in run.get('discovered_documents', []):
            item = api().require_source(document_id)
            snapshots = run.setdefault('read_snapshots', {})
            snapshot = snapshots.setdefault(document_id, {'text': item['transcript'], 'title': item['title'], 'revision': item['updated_at']})
            if snapshot['revision'] != item['updated_at']:
                raise ValueError('参考文稿在分页读取时已变化，请重新发送')
            text, title = snapshot['text'], snapshot['title']
        else:
            raise ValueError('请先搜索资料，不能猜测文稿编号')
        offset = args['offset']
        if offset > len(text):
            raise ValueError('读取起点超过文稿长度')
        end = min(offset + PAGE_SIZE, len(text))
        return {'state': 'completed', 'message': '已读取只读资料', 'document_id': document_id, 'title': title,
                'text': text[offset:end], 'offset': offset, 'next_offset': end if end < len(text) else None, 'total': len(text)}
    if identity == 'list_tasks':
        rows = [value for value in store.object_list('ai_run') if value['workspace_id'] == run['workspace_id']]
        rows.sort(key=lambda value: value['created_at'], reverse=True)
        return {'state': 'completed', 'tasks': [{key: value.get(key) for key in ('id', 'status', 'phase', 'current', 'total', 'saved_output_id')} for value in rows[:20]]}
    if identity == 'inspect_link':
        return {'state': 'completed', **inspect_link(args['url'])}
    if identity in {'load_skill', 'read_skill_reference'}:
        result = ai_skills.load(args['name'], run.get('skill_entries', []), explicit_name=run.get('explicit_skill'),
                                reference=args.get('path'), loaded=run.get('loaded_skills', []))
        if identity == 'load_skill' and args['name'] not in run.setdefault('loaded_skills', []):
            run['loaded_skills'].append(args['name'])
        return result
    if identity == 'search_skills':
        query = args['query'].casefold().strip()
        return {'state': 'completed', 'entries': [row for row in ai_skills.listing()['entries'] if query in ' '.join(str(row[key]) for key in ('name', 'title', 'description')).casefold()]}
    if identity == 'list_skill_drafts':
        rows = ai_skills.drafts()['entries']
        run.setdefault('skill_preview_tokens', []).extend(row['token'] for row in rows if row['token'] not in run.get('skill_preview_tokens', []))
        return {'state': 'completed', 'entries': [{key: row[key] for key in ('token', 'entry', 'existing')} for row in rows], 'message': '已查看技能预览，未安装'}
    if identity == 'create_document':
        if not args['title'].strip() or not args['markdown'].strip():
            raise ValueError('新文稿标题和正文不能为空')
        return pending_action(run, call, {'title': args['title'], 'text': args['markdown']})
    if identity == 'create_skill_draft':
        value = ai_skills.draft_package(args['name'], args['description'], args['instructions'])
        return pending_action(run, call, {'name': value['name'], 'description': value['description'], 'text': value['content']})
    if identity == 'install_skill':
        if bool(args['name']) == bool(args['token']):
            raise ValueError('请选择一个内置技能名称或已预览编号，不能同时指定')
        if args['name']:
            if args['name'] not in ai_skills.bundled():
                raise ValueError('名称安装仅支持本轮已知的内置技能；本地技能请先预览')
            value = ai_skills.get_skill(args['name'])
            preview = {'name': value['name'], 'description': value['description'], 'text': value['content'], 'digest': value['digest'], 'existing_generation': value['generation']}
        else:
            if args['token'] not in run.get('skill_preview_tokens', []):
                raise ValueError('请先查看待安装技能，不能猜测预览编号')
            record = store.object_get('ai_skill_preview', args['token'])
            if not record:
                raise ValueError('技能预览已移除，请重新选择')
            existing = store.object_get('ai_skill', record['package']['name']) or ai_skills.bundled().get(record['package']['name'])
            preview = {'name': record['package']['name'], 'description': record['package']['description'], 'text': record['package']['content'], 'token': args['token'], 'digest': record['package']['digest'], 'existing_generation': existing['generation'] if existing else None}
        return pending_action(run, call, preview)
    if identity == 'prepare_voice':
        return {'state': 'needs_input', 'action': 'voice', 'label': '前往文案配音', 'message': '已准备文字，请在配音页确认音色后生成', 'target': target(run)}
    if identity == 'prepare_transcription':
        return {'state': 'needs_input', 'action': 'transcription', 'label': '前往内容提取', 'message': '请在提取页确认来源和模型后开始', 'url': valid_url(args['url'], optional=True)}
    if identity == 'prepare_collection':
        info = inspect_link(args['url'])
        if info['kind'] != 'homepage':
            return {'state': 'unavailable', **info, 'message': '当前主页采集支持抖音用户主页；单条作品请使用内容提取'}
        return {'state': 'needs_input', 'action': 'collection', 'label': '前往主页采集', **info, 'limit': args['limit'], 'message': '请在主页采集中确认登录和数量后开始'}
    return {'state': 'needs_input', 'action': 'skills', 'label': '打开技能管理', 'message': '请在技能管理中预览并安装本地技能'}


class ActionInput(BaseModel):
    revision: StrictInt = Field(ge=0)


def check_action(workspace, run, action, revision, con):
    ai = api()
    ai.ensure_revision(workspace, revision)
    if run['status'] != 'done' or workspace['run_id'] != run['id']:
        raise HTTPException(409, '只能确认当前已完成请求的操作；取消、重启或新请求后的旧操作不会执行')
    frozen, current = run.get('agent_config', {}), workspace.get('agent_config', {})
    if not all(config.get('tools_enabled') and action['tool'] in config.get('allowed_tools', []) for config in (frozen, current)):
        raise HTTPException(409, '该工具已停用或不在本轮许可范围，未执行')
    if (workspace['revision'] != run['draft_revision'] or workspace['title'] != run['title']
            or workspace['text'] != run.get('base_text', run['input_text']) or workspace['source_id'] != run['source_id']
            or workspace['source_hash'] != run['source_hash'] or workspace.get('save_generation', 0) != run.get('save_generation', 0)):
        raise HTTPException(409, '当前文稿或保存状态已经变化，请重新发送后确认')
    if run['source_id']:
        item = store.decode(con.execute('SELECT * FROM items WHERE id=? AND deleted=0', (run['source_id'],)).fetchone())
        if (not item or item['status'] in ('queued', 'processing') or item['id'] in engine.pending
                or ai.fingerprint(item) != run['source_hash'] or item['updated_at'] != run.get('source_revision', item['updated_at'])):
            raise HTTPException(409, '来源已变化或不可用，未执行')
    ai_skills.validate_loaded(run)
    for reference in run.get('references', []):
        if ai_edits.reference_status(workspace, reference):
            raise HTTPException(409, '引用来源已变化，请重新发送')


def action_endpoint(workspace_id, run_id, action_id, body, confirm):
    ai = api()
    with engine.job_lock, ai.lock, ai_skills.LOCK, store.connection() as con:
        con.execute('BEGIN IMMEDIATE')
        workspace, run = ai.get_workspace(workspace_id), ai.get_run(workspace_id, run_id)
        action = next((value for value in run.get('actions', []) if value['id'] == action_id), None)
        if action is None:
            raise HTTPException(404, '找不到这项操作')
        final = 'confirmed' if confirm else 'dismissed'
        if action['state'] == final:
            return {'workspace': ai.read_workspace(workspace_id), 'idempotent': True, **action.get('result', {})}
        if action['state'] != 'pending':
            raise HTTPException(409, '这项操作已处理，请刷新查看')
        check_action(workspace, run, action, body.revision, con)
        result = {}
        if confirm:
            args = action['arguments']
            if action['tool'] == 'create_document':
                result = {'document_id': ai.create_item(con, action['preview']['title'], action['preview']['text'], metadata={'ai_run_id': run_id, 'source_id': run['source_id'], 'protected_brands': run.get('translation_brands', [])}), 'message': '已另存为新文稿，原文保持不变'}
            elif action['tool'] == 'create_skill_draft':
                value = ai_skills.draft_package(args['name'], args['description'], args['instructions'])
                value.update(origin='draft', installed=False, enabled=False, generation='', updated_at=time.time())
                token = uuid.uuid4().hex
                ai_skills.put(con, 'ai_skill_preview', token, {'token': token, 'package': value, 'created_at': time.time()})
                result = {'token': token, 'name': value['name'], 'message': '已保存技能草稿，尚未安装'}
            elif action['tool'] == 'install_skill':
                existing = store.object_get('ai_skill', action['preview']['name']) or ai_skills.bundled().get(action['preview']['name'])
                if (existing['generation'] if existing else None) != action['preview'].get('existing_generation'):
                    raise HTTPException(409, '待安装技能的启用或版本状态已变化，请重新预览确认')
                if args['token']:
                    record = store.object_get('ai_skill_preview', args['token'])
                    if not record or record['package']['digest'] != action['preview']['digest']:
                        raise HTTPException(409, '技能预览已变化，请重新选择')
                    skill = ai_skills.install(args['token'], False, con)
                else:
                    if ai_skills.get_skill(args['name'])['digest'] != action['preview']['digest']:
                        raise HTTPException(409, '待恢复技能的内容已变化，请重新预览')
                    skill = ai_skills.restore(args['name'], con)
                result = {'name': skill['name'], 'message': '已安装并启用技能'}
            else:
                raise ValueError('这项操作没有可执行的确认步骤')
        action.update(state=final, result=result, resolved_at=time.time())
        for event in run.get('events', []):
            if event.get('result', {}).get('action_id') == action_id:
                event['confirmation'] = {'state': final, **result}
        workspace['messages'].append({'id': uuid.uuid4().hex, 'role': 'status', 'text': ('用户确认：' + result['message']) if confirm else '用户已忽略：' + action['label'],
                                      'run_id': run_id, 'action_id': action_id, 'created_at': time.time()})
        workspace['updated_at'] = time.time()
        ai.put_object(con, 'ai_run', run_id, run)
        ai.put_object(con, 'ai_workspace', workspace_id, workspace)
    return {'workspace': ai.read_workspace(workspace_id), **result}


@router.post('/workspaces/{workspace_id}/runs/{run_id}/actions/{action_id}/confirm')
def confirm_action(workspace_id: str, run_id: str, action_id: str, body: ActionInput):
    return action_endpoint(workspace_id, run_id, action_id, body, True)


@router.post('/workspaces/{workspace_id}/runs/{run_id}/actions/{action_id}/dismiss')
def dismiss_action(workspace_id: str, run_id: str, action_id: str, body: ActionInput):
    return action_endpoint(workspace_id, run_id, action_id, body, False)
