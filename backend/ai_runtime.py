"""Bounded model/tool orchestration with durable events and staged compaction."""
import asyncio
import copy
import json
import time
import uuid

import httpx

from . import ai_context, ai_edits, ai_translation, ai_proofreading, engine, integrations, store

MAX_STEPS = 8


def api():
    from . import ai_workspace
    return ai_workspace


def alive(run_id):
    run = store.object_get('ai_run', run_id)
    return bool(run and run['status'] in api().ACTIVE)


def update(run_id, **fields):
    ai = api()
    with ai.lock:
        run = store.object_get('ai_run', run_id)
        if not run or run['status'] not in ai.ACTIVE:
            raise asyncio.CancelledError()
        run.update(fields)
        store.object_put('ai_run', run_id, run)
        return run


def allowed(run, name):
    ai = api()
    current = ai_context.config(ai.get_workspace(run['workspace_id']))
    frozen = run['agent_config']
    if (not current['tools_enabled'] or not frozen['tools_enabled']
            or name not in current['allowed_tools'] or name not in frozen['allowed_tools']):
        raise ValueError('工具权限已关闭或不在本轮允许范围，请重新发送')


async def request_tools(client, base, headers, model, messages, schemas, max_output):
    try:
        response = await client.post(integrations.llm_chat_endpoint(base), headers=headers, json={
            'model': model, 'messages': messages, 'temperature': .3, 'max_tokens': max_output,
            'stream': False, 'tools': schemas, 'tool_choice': 'auto'})
    except httpx.TimeoutException:
        raise ValueError('工具模型响应超时，请检查网络后重试') from None
    except (httpx.RequestError, httpx.InvalidURL):
        raise ValueError('无法连接工具模型，请检查服务地址和网络') from None
    if response.status_code in (400, 404, 422):
        raise ValueError('模型拒绝了工具调用请求；请确认模型支持 Chat Completions 工具，或关闭工具后重试')
    if not 200 <= response.status_code < 300:
        integrations.raise_llm_error(response)
    try:
        payload = response.json()
        choices = payload.get('choices')
        choice = choices[0] if isinstance(choices, list) and choices else {}
        message = choice.get('message')
    except (ValueError, AttributeError, IndexError):
        raise ValueError('工具模型返回的内容不是有效响应，请检查接口兼容性') from None
    if choice.get('finish_reason') == 'length':
        raise ValueError('模型输出达到长度限制，本轮未发布不完整结果')
    if not isinstance(message, dict) or payload.get('error'):
        raise ValueError('工具模型返回的内容无效，请检查模型权限和接口兼容性')
    calls = message.get('tool_calls') or []
    if (not isinstance(calls, list) or message.get('content') is not None and not isinstance(message.get('content'), str)
            or not calls and not isinstance(message.get('content'), str)):
        raise ValueError('模型没有返回正文或有效工具请求')
    # Agnes also returns stream-position metadata in non-streaming replies.
    # Remove only that known, non-executable field before strict validation and
    # the assistant/tool round trip; never relax function names or arguments.
    normalized = []
    for call in calls:
        if isinstance(call, dict) and 'index' in call:
            if type(call['index']) is not int or call['index'] < 0:
                raise ValueError('工具调用位置编号无效，本批未执行')
            call = {key: value for key, value in call.items() if key != 'index'}
        normalized.append(call)
    return {'content': message.get('content') or '', 'tool_calls': normalized}


def validate_calls(raw_calls, run, used_ids):
    from . import ai_tools
    if len(raw_calls) + run.get('tool_steps', 0) > MAX_STEPS:
        raise ValueError('本轮工具调用已达到 8 步上限，请查看已完成记录后继续')
    result, fresh = [], set()
    for raw in raw_calls:
        if type(raw) is not dict or set(raw) - {'id', 'type', 'function'} or raw.get('type') != 'function':
            raise ValueError('工具调用格式无效，本批未执行')
        identity, function = raw.get('id'), raw.get('function')
        if (not isinstance(identity, str) or not identity or len(identity) > 160
                or identity in used_ids or identity in fresh or type(function) is not dict
                or set(function) != {'name', 'arguments'}):
            raise ValueError('工具调用编号重复或结构不完整，本批未执行')
        if not isinstance(function['arguments'], str) or len(function['arguments']) > 240000:
            raise ValueError('工具参数格式无效或过大，本批未执行')
        def unique_object(pairs):
            value = {}
            for key, item in pairs:
                if key in value:
                    raise ValueError('duplicate key')
                value[key] = item
            return value
        try:
            arguments = json.loads(function['arguments'], object_pairs_hook=unique_object)
        except ValueError:
            raise ValueError('工具参数不是有效 JSON，本批未执行') from None
        call = {'id': identity, 'name': function['name'], 'arguments': arguments}
        allowed(run, call['name'])
        ai_tools.validate(call, run['agent_config']['allowed_tools'])
        fresh.add(identity)
        result.append(call)
    return result


def record_exchange(run_id, run, raw_message, results):
    ai = api()
    with ai.lock:
        saved = store.object_get('ai_run', run_id)
        if saved['status'] not in ai.ACTIVE:
            raise asyncio.CancelledError()
        workspace = ai.get_workspace(run['workspace_id'])
        # Updates are only to this run and to append-only conversation records.
        run['status'] = saved['status']
        workspace['messages'].append({'id': uuid.uuid4().hex, 'role': 'tool',
            'text': ai_context.encode({'calls': raw_message['tool_calls'], 'results': results}),
            'run_id': run_id, 'created_at': time.time()})
        workspace['updated_at'] = time.time()
        with store.connection() as con:
            ai.put_object(con, 'ai_run', run_id, run)
            ai.put_object(con, 'ai_workspace', workspace['id'], workspace)


async def compact(run_id, client, base, headers, model, history):
    ai = api()
    with ai.lock:
        run = update(run_id, status='running', phase='正在压缩较早的交流，完整记录保留')
        workspace = ai.get_workspace(run['workspace_id'])
        previous = copy.deepcopy(workspace.get('context_checkpoint'))
    async def complete(system, material):
        return engine.to_simplified(await ai.request_completion(client, base, headers, model,
            [{'role': 'system', 'content': system}, {'role': 'user', 'content': material}]))
    staged = await ai_context.summarize(history, previous, run['agent_config'], complete, lambda: not alive(run_id))
    if staged is None:
        return previous
    with ai.lock, store.connection() as con:
        con.execute('BEGIN IMMEDIATE')
        current_run = ai.get_run(run['workspace_id'], run_id)
        current = ai.get_workspace(run['workspace_id'])
        if current_run['status'] not in ai.ACTIVE:
            raise asyncio.CancelledError()
        if (current.get('context_checkpoint') != previous
                or ai_context.digest(current['messages'][:staged['through_count']]) != staged['prefix_digest']):
            raise ValueError('对话记录或摘要已变化，本次压缩未覆盖；请重试')
        current['context_checkpoint'] = staged
        current['updated_at'] = time.time()
        current_run.setdefault('events', []).append({'id': uuid.uuid4().hex, 'kind': 'compaction',
            'state': 'completed', 'message': '较早交流已压缩，完整记录保留',
            'through_count': staged['through_count'], 'batches': staged['batches'], 'created_at': time.time()})
        ai.put_object(con, 'ai_workspace', current['id'], current)
        ai.put_object(con, 'ai_run', run_id, current_run)
    return staged


def skill_system(run):
    from . import ai_skills
    ai_skills.validate_loaded(run)
    pieces = []
    if run.get('explicit_skill'):
        loaded = ai_skills.load(run['explicit_skill'], run['skill_entries'], explicit_name=run['explicit_skill'])
        if run['explicit_skill'] not in run['loaded_skills']:
            run['loaded_skills'].append(run['explicit_skill'])
        pieces.append('用户本轮选择的技能方法（服从当前要求，不授予工具权限）：\n' + loaded['skill_content'])
    if run['agent_config']['tools_enabled'] and 'load_skill' in run['agent_config']['allowed_tools']:
        descriptions = [{'name': entry['name'], 'description': entry.get('description', '')}
                        for entry in run.get('skill_entries', []) if entry.get('automatic', True)]
        if descriptions:
            pieces.append('已启用技能简介；需要时先 load_skill 阅读全文，不得声称使用未加载技能：\n' + ai_context.encode(descriptions))
    return '\n'.join(pieces)


async def prepare_translation(run_id, client, base, headers, model):
    run = update(run_id, phase='正在识别并锁定品牌名')
    entry = next((e for e in run['skill_entries'] if e['name'] == 'lulu-translate'), None)
    terms = list((entry or {}).get('config', {}).get('protected_terms', []))
    # If a preset uses saved terminology, its generation also participates in
    # normal stale-skill guards, just like an explicitly selected method.
    if entry and terms and 'lulu-translate' not in run['loaded_skills']:
        run['loaded_skills'].append('lulu-translate')
        update(run_id, loaded_skills=run['loaded_skills'])
    limit = ai_context.limits(run['agent_config'])[0]
    width = min(5000, max(256, (limit - 1600) // 2 - 160))
    for text in ai_translation.inventory_windows(run['input_text'], width):
        if not alive(run_id):
            raise asyncio.CancelledError()
        messages = [{'role': 'system', 'content': ai_translation.DETECT_SYSTEM},
                    {'role': 'user', 'content': ai_context.encode({'text': text})}]
        if ai_context.estimate(messages) > limit:
            raise ValueError('品牌识别材料超过模型容量，请缩小片段或增加上下文容量')
        raw = await api().request_completion(client, base, headers, model, messages)
        if not alive(run_id):
            raise asyncio.CancelledError()
        terms = ai_translation.names(list(dict.fromkeys(terms + ai_translation.parse_inventory(raw, text))))
    guard = ai_translation.BrandLock(run['input_text'], terms)
    update(run_id, translation_brands=guard.terms)
    return guard, width


def activate_proofreading(run_id, extra_skills=()):
    run = store.object_get('ai_run', run_id)
    if ai_proofreading.enabled(run, extra_skills):
        ai_proofreading.check_input(run['input_text'])
        if not run.get('proofreading_check'):
            run = update(run_id, proofreading_check={'state': 'checking'})
        if any(e['name'] == 'lulu-proofread' for e in run['skill_entries']) and 'lulu-proofread' not in run['loaded_skills']:
            run = update(run_id, loaded_skills=[*run['loaded_skills'], 'lulu-proofread'])
    return run


def check_proofreading(run_id, original, result, final=False):
    run = store.object_get('ai_run', run_id)
    if ai_proofreading.enabled(run):
        try:
            report = ai_proofreading.validate(original, result, engine.to_simplified)
        except ai_proofreading.Rejected as exc:
            update(run_id, rejected_result=result, proofreading_check={**exc.report,
                   'part': 0 if final else run.get('current', 1), 'total': run.get('total', 1)})
            raise
        if final:
            update(run_id, proofreading_check=report)
    return result


async def process_part(run_id, client, base, headers, model, messages, schemas, compact_prefix=None,
                       brand_lock=None, original_part='', late_protect=None):
    from . import ai_skills, ai_tools
    ai = api()
    local = copy.deepcopy(messages)
    prefix_length = len(local)
    run = store.object_get('ai_run', run_id)
    used_ids = {event['call_id'] for event in run.get('events', []) if event.get('call_id')}
    repeated_errors = {}
    repair = False
    while True:
        if not alive(run_id):
            raise asyncio.CancelledError()
        run = activate_proofreading(run_id)
        ai_skills.validate_loaded(run)
        if brand_lock is None and 'lulu-translate' in run.get('loaded_skills', []) and late_protect:
            brand_lock = await late_protect()
            run = store.object_get('ai_run', run_id)
        wire = brand_lock.messages(local, repair) if brand_lock else local
        if ai_proofreading.enabled(run):
            wire = [{**wire[0], 'content': wire[0]['content'] + ai_proofreading.SYSTEM}, *wire[1:]]
        if (run['agent_config']['auto_compact'] and compact_prefix is not None
                and ai_context.estimate(wire) + ai_context.estimate(schemas) >= ai_context.limits(run['agent_config'])[1]):
            replacement = await compact_prefix()
            if replacement is not None:
                # Never compress a live partial tool exchange or lose its exact result IDs.
                local = replacement + local[prefix_length:]
                prefix_length = len(replacement)
                run = store.object_get('ai_run', run_id)
                ai_skills.validate_loaded(run)
                wire = brand_lock.messages(local, repair) if brand_lock else local
                if ai_proofreading.enabled(run):
                    wire = [{**wire[0], 'content': wire[0]['content'] + ai_proofreading.SYSTEM}, *wire[1:]]
        if ai_context.estimate(wire) + ai_context.estimate(schemas) > ai_context.limits(run['agent_config'])[0]:
            raise ValueError('当前文稿、工具结果和必要上下文超过模型容量，请缩小片段或增加上下文容量；内容未截断')
        if not schemas:
            result = await ai.request_completion(client, base, headers, model, wire)
            ai_skills.validate_loaded(run)
            if not alive(run_id):
                raise asyncio.CancelledError()
            try:
                result = brand_lock.restore(result, original_part, engine.to_simplified) if brand_lock else engine.to_simplified(result)
            except ValueError:
                if repair:
                    raise
                repair = True
                update(run_id, phase='模型改动了品牌名，正在重新翻译（1/1）')
                continue
            return check_proofreading(run_id, original_part, result)
        response = await request_tools(client, base, headers, model, wire, schemas, ai_context.output_limit(run['agent_config']))
        ai_skills.validate_loaded(run)
        if not alive(run_id):
            raise asyncio.CancelledError()
        if not response['tool_calls']:
            if not response['content'].strip():
                raise ValueError('模型没有返回有效正文，请检查模型兼容性')
            try:
                result = brand_lock.restore(response['content'], original_part, engine.to_simplified) if brand_lock else engine.to_simplified(response['content'])
            except ValueError:
                if repair:
                    raise
                repair = True
                update(run_id, phase='模型改动了品牌名，正在重新翻译（1/1）')
                continue
            return check_proofreading(run_id, original_part, result)
        calls = validate_calls(response['tool_calls'], run, used_ids)
        if brand_lock:
            try:
                for call in calls:
                    if call['name'] == 'create_document':
                        brand_lock.restore(call['arguments']['markdown'], original_part)
                    call['arguments'] = brand_lock.map_values(call['arguments'], brand_lock.restore)
            except ValueError:
                if repair:
                    raise
                repair = True
                update(run_id, phase='模型改动了品牌名，正在重新翻译（1/1）')
                continue
        # Inspect every candidate before executing any tool in the batch,
        # including a batch that loads the proofreading skill and writes at once.
        run = activate_proofreading(run_id, [c['arguments']['name'] for c in calls if c['name'] == 'load_skill'])
        ai_skills.validate_loaded(run)
        for call in calls:
            if call['name'] == 'create_document':
                check_proofreading(run_id, run['input_text'],
                    engine.simplify_with_brands(call['arguments']['markdown'], run.get('translation_brands', [])), final=True)
        run = store.object_get('ai_run', run_id)
        raw = {'role': 'assistant', 'content': response['content'] or None, 'tool_calls': response['tool_calls']}
        results, stop = [], False
        for call in calls:
            with ai.lock:
                if not alive(run_id):
                    raise asyncio.CancelledError()
                allowed(run, call['name'])
                ai_skills.validate_loaded(run)
                event = {'id': uuid.uuid4().hex, 'kind': 'tool', 'call_id': call['id'], 'name': call['name'],
                         'arguments': call['arguments'], 'state': 'running', 'created_at': time.time()}
                run.setdefault('events', []).append(event)
                run['tool_steps'] = run.get('tool_steps', 0) + 1
                run['phase'] = f"正在调用工具：{call['name']}（{run['tool_steps']}/{MAX_STEPS}）"
                store.object_put('ai_run', run_id, run)
                try:
                    result = ai_tools.execute(call, run)
                except ValueError as exc:
                    result = {'state': 'error', 'message': str(exc)}
                event.update(state=result.get('state', 'completed'), result=result, finished_at=time.time())
                store.object_put('ai_run', run_id, run)
                used_ids.add(call['id'])
                results.append({'call_id': call['id'], 'name': call['name'], 'result': result})
                if result.get('state') == 'error':
                    key = (call['name'], result.get('message', ''))
                    repeated_errors[key] = repeated_errors.get(key, 0) + 1
                    if repeated_errors[key] >= 2:
                        stop = True
                if result.get('state') in ('needs_input', 'pending'):
                    stop = True
        if stop:
            run['non_edit_result'] = True
        record_exchange(run_id, run, raw, results)
        if stop:
            # A handoff or pending write is an actual artifact, never a claim of completion.
            return '\n'.join(result['result'].get('message', '请查看工具记录并完成所需操作') for result in results)
        local.append(raw)
        local.extend({'role': 'tool', 'tool_call_id': result['call_id'], 'content': ai_context.encode(result['result'])} for result in results)


async def run_job(run_id, base, headers, model, history):
    ai = api()
    token = None
    try:
        run = store.object_get('ai_run', run_id)
        token = ai.request_max_output.set(ai_context.output_limit(run['agent_config']))
        async with httpx.AsyncClient(timeout=300, trust_env=False, follow_redirects=False) as client:
            if run.get('mode') == 'compact':
                checkpoint = await compact(run_id, client, base, headers, model, history)
                update(run_id, status='done', phase='较早交流已压缩，完整记录保留' if checkpoint else '近期交流已全部保留，暂时无需压缩', finished_at=time.time())
                return
            from . import ai_skills, ai_tools
            tools = run['agent_config']['allowed_tools'] if run['agent_config']['tools_enabled'] else []
            schemas = ai_tools.schemas(tools)
            system = ai.SYSTEM + ('\n可以使用本轮启用的 Lulu 工具；只有工具返回成功才可声称执行完成。需要确认的动作必须如实说明尚未执行。' if schemas else '')
            skill = skill_system(run)
            if skill:
                system += '\n' + skill
                with ai.lock:
                    if not alive(run_id):
                        raise asyncio.CancelledError()
                    store.object_put('ai_run', run_id, run)
            brand_lock = None
            if ai_translation.requested(run):
                brand_lock, width = await prepare_translation(run_id, client, base, headers, model)
                parts = list(ai_translation.parts(run['input_text'], brand_lock.terms, width))
            else:
                parts = list(ai_proofreading.parts(run['input_text']) if ai_proofreading.enabled(run) else integrations.chunks(run['input_text']))
            async def late_protect():
                nonlocal brand_lock
                if brand_lock is None:
                    brand_lock, _ = await prepare_translation(run_id, client, base, headers, model)
                    if list(ai_translation.parts(run['input_text'], brand_lock.terms)) != parts:
                        raise ValueError('品牌名跨越当前分段，请选择“文案翻译”技能重新发送，以完整保护品牌')
                return brand_lock
            outputs = []
            checkpoint = ai.get_workspace(run['workspace_id']).get('context_checkpoint')
            for index, part in enumerate(parts):
                run = update(run_id, status='running', current=index + 1, total=len(parts), phase=f'正在处理第 {index + 1} / {len(parts)} 段')
                target = '选中片段（只处理此片段）' if run.get('scope') == 'selection' else '参考文稿'
                user = {'role': 'user', 'content': f"本轮要求：{run['instruction']}\n{target}（第 {index + 1}/{len(parts)} 段）：\n" + json.dumps(brand_lock.mask(part) if brand_lock else part, ensure_ascii=False) + ai_edits.reference_prompt(run)}
                messages = [{'role': 'system', 'content': system}, *ai_context.projection(history, checkpoint), user]
                async def compact_prefix():
                    nonlocal checkpoint
                    valid = ai_context.verified_checkpoint(history, checkpoint)
                    remaining = history[valid['through_count'] if valid else 0:]
                    if len(ai_context.turns(remaining)) <= 2:
                        return None
                    checkpoint = await compact(run_id, client, base, headers, model, history)
                    update(run_id, phase=f'正在处理第 {index + 1} / {len(parts)} 段')
                    return [{'role': 'system', 'content': system}, *ai_context.projection(history, checkpoint), user]
                outputs.append(await process_part(run_id, client, base, headers, model, messages, schemas,
                                                  compact_prefix, brand_lock, part, late_protect))
                if store.object_get('ai_run', run_id).get('non_edit_result'):
                    break
            run = store.object_get('ai_run', run_id)
            result = ai_proofreading.join_parts(parts, outputs) if ai_proofreading.enabled(run) and not run.get('non_edit_result') else '\n\n'.join(outputs)
            if not run.get('non_edit_result'):
                check_proofreading(run_id, run['input_text'], result, final=True)
            with ai.lock:
                run = ai.get_run(run['workspace_id'], run_id)
                if run['status'] not in ai.ACTIVE:
                    return
                ai_skills.validate_loaded(run)
                run.update(status='done', result=result, phase='处理完成，结果尚未写入文稿', finished_at=time.time())
                # Tool handoffs are conversation replies, not replacement manuscript text.
                run['non_edit_result'] = run.get('non_edit_result', False) or any(event.get('state') in ('needs_input', 'pending') for event in run.get('events', []))
                ai_edits.initialize_proposals(run)
                if run['non_edit_result']:
                    run['proposals'] = []
                workspace = ai.get_workspace(run['workspace_id'])
                workspace['messages'].append({'id': uuid.uuid4().hex, 'role': 'assistant', 'text': run['result'],
                    'run_id': run_id, 'scope': run.get('scope', 'document'), 'selection': run.get('selection'),
                    'references': run.get('references', []), 'non_edit_result': run['non_edit_result'], 'created_at': time.time()})
                workspace['updated_at'] = time.time()
                with store.connection() as con:
                    ai.put_object(con, 'ai_run', run_id, run)
                    ai.put_object(con, 'ai_workspace', workspace['id'], workspace)
    except asyncio.CancelledError:
        ai.finish_failed(run_id, 'cancelled', '已取消，原文与完整对话保持不变')
    except ValueError as exc:
        ai.finish_failed(run_id, 'error', str(exc))
    except Exception:
        ai.finish_failed(run_id, 'error', '处理未完成，请重试；原文、草稿与完整对话已保留')
    finally:
        if token is not None:
            ai.request_max_output.reset(token)
        ai.tasks.pop(run_id, None)
