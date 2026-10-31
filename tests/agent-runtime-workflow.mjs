// Native Windows Electron acceptance with isolated synthetic history and local HTTP replies.
// Build first. No real cloud account, user library, platform requests or model downloads.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import http from 'node:http';
import { spawnSync } from 'node:child_process';
import { _electron as electron } from 'playwright';

const root = process.cwd();
await fs.mkdir(path.join(root, '.test-data'), { recursive: true });
const data = await fs.mkdtemp(path.join(root, '.test-data', 'agent-runtime-desktop-'));
const seeded = spawnSync(path.join(root, '.venv/Scripts/python.exe'), ['-X', 'utf8', '-m', 'tests.agent_runtime_fixture'], {
  cwd: root, windowsHide: true, encoding: 'utf8', env: { ...process.env, LULU_DATA_DIR: data },
});
assert.equal(seeded.status, 0, seeded.stderr);
const fixtures = JSON.parse(seeded.stdout);
const skillName = 'desktop-runtime-check';
const skillContent = `---\nname: ${skillName}\ndescription: 离线桌面验收技能。\ndisable-model-invocation: true\n---\n\nSKILL_METHOD_SENTINEL_42：保留价格199元，回答以“技能已读取”开头；不要自动保存来源。\n`;
const importedSkill = path.join(data, 'SKILL.md');
await fs.writeFile(importedSkill, skillContent);
const requests = [], modelErrors = [], errors = [], checks = [];
let mode = 'auto', toolStep = 0, brandStep = 0, releaseSummary = null, summaryReleased = false;
const summary = { goals: ['CTX_SUMMARY_SENTINEL：继续核对素材'], constraints: ['保留价格199元'], decisions: ['尚未写回来源'], pending: ['等待用户下一条明确要求'] };
const createdTitle = '工具确认后生成的验收文稿';
const createdText = '依据只读参考整理：重量120克，价格199元。';
const handoffUrl = 'https://v.douyin.com/offline-runtime-check/';
// Agnes includes index even with stream:false; accept it without echoing it back.
function call(id, name, args) { return { index: 0, id, type: 'function', function: { name, arguments: JSON.stringify(args) } }; }
const server = http.createServer(async (req, res) => {
  try {
    assert.equal(req.url, '/v1/chat/completions');
    let raw = ''; for await (const part of req) raw += part;
    const body = JSON.parse(raw);
    assert.equal(body.model, 'offline-runtime-fixture');
    for (const message of body.messages) for (const call of message.tool_calls || []) {
      assert(!Object.hasOwn(call, 'index'), 'Provider position metadata must not enter request tool envelopes');
    }
    const isSummary = String(body.messages[0]?.content).startsWith('把对话资料整理为紧凑摘要');
    requests.push({ mode, summary: isSummary, body });
    let message;
    if (isSummary) {
      assert(!body.tools, 'Compaction cannot request executable tools');
      const material = JSON.parse(body.messages.at(-1).content);
      assert(Array.isArray(material.messages) && material.messages.length);
      if (mode === 'cancel') {
        await new Promise(resolve => { releaseSummary = resolve; });
        summaryReleased = true;
        if (res.destroyed) return;
      }
      message = { role: 'assistant', content: mode === 'manual-bad' ? '这不是一个有效摘要对象' : JSON.stringify(summary) };
    } else if (mode.startsWith('proof-')) {
      if (mode !== 'proof-disabled') assert(JSON.stringify(body.messages).includes('本轮启用严格校对'));
      message = { role: 'assistant', content: mode === 'proof-good' ? fixtures.proof.transcript.replace('测式', '测试') : fixtures.proof.transcript.replaceAll('199', '299') };
    } else if (mode === 'brand') {
      if (body.messages[0].content.startsWith('识别材料中的品牌名')) {
        message = { role: 'assistant', content: JSON.stringify({ brands: ['松果X1'] }) };
      } else {
        const tokens = body.messages.at(-1).content.match(/BRANDLOCK_[A-Za-z0-9_]+_END/g);
        assert.equal(tokens.length, 3);
        assert(!JSON.stringify(body.messages).includes('松果X1'));
        assert(!JSON.stringify(body.messages).includes('米家 Pro'));
        if (brandStep++ === 0) message = { role: 'assistant', content: 'Pinecone X1 weighs 120 grams.' };
        else {
          assert(body.messages[0].content.includes('上一份译文未完整保留品牌标记'));
          message = { role: 'assistant', content: `${tokens[0]} weighs 120 grams. ${tokens[1]} costs 299 yuan. ${tokens[2]} launches on November 2.` };
        }
      }
    } else if (mode === 'tools') {
      const names = body.tools.map(value => value.function.name);
      for (const name of ['search_documents', 'read_document', 'create_document']) assert(names.includes(name));
      if (toolStep === 0) message = { role: 'assistant', content: null, tool_calls: [call('find-reference', 'search_documents', { query: '可检索参考' })] };
      else if (toolStep === 1) {
        assert.equal(body.messages.at(-1).role, 'tool');
        assert.equal(body.messages.at(-1).tool_call_id, 'find-reference');
        assert(body.messages.at(-1).content.includes(fixtures.reference.id));
        message = { role: 'assistant', content: null, tool_calls: [call('read-reference', 'read_document', { document_id: fixtures.reference.id, offset: 0 })] };
      } else if (toolStep === 2) {
        assert.equal(body.messages.at(-1).tool_call_id, 'read-reference');
        assert(body.messages.at(-1).content.includes(fixtures.reference.transcript));
        message = { role: 'assistant', content: null, tool_calls: [call('create-note', 'create_document', { title: createdTitle, markdown: createdText })] };
      } else throw Error('Pending write should stop the tool loop until user confirmation');
      toolStep++;
    } else if (mode === 'handoff') {
      assert(body.tools.some(value => value.function.name === 'prepare_transcription'));
      message = { role: 'assistant', content: null, tool_calls: [call('prepare-extract', 'prepare_transcription', { url: handoffUrl })] };
    } else if (mode === 'voice') {
      assert(body.tools.some(value => value.function.name === 'prepare_voice'));
      message = { role: 'assistant', content: null, tool_calls: [call('prepare-voice', 'prepare_voice', {})] };
    } else if (mode === 'unsupported') {
      assert(body.tools?.length);
      res.writeHead(400, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ error: { message: 'This offline model does not support tool calls.' } }));
      return;
    } else if (mode === 'skill') {
      assert(JSON.stringify(body.messages).includes('SKILL_METHOD_SENTINEL_42'));
      assert(!body.tools, 'Explicit skill works while tool execution remains disabled');
      message = { role: 'assistant', content: '技能已读取，价格仍为199元。' };
    } else if (mode === 'plain') {
      assert(!body.tools, 'Turning tools off must use the regular text request');
      message = { role: 'assistant', content: '普通全文处理完成，原文仍需用户确认保存。' };
    } else {
      assert.equal(mode, 'auto');
      assert(body.max_tokens > 0 && body.max_tokens <= 2048);
      message = { role: 'assistant', content: '压缩后继续处理，价格仍保留199元。' };
    }
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ choices: [{ message, finish_reason: message.tool_calls ? 'tool_calls' : 'stop' }] }));
  } catch (error) {
    modelErrors.push(error.message);
    res.writeHead(500, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ error: { message: 'Offline acceptance fixture rejected an unexpected request.' } }));
  }
});
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
let app, page, base;
const collectionRequests = [], voiceRequests = [];
async function request(url, method = 'GET', body) {
  return fetch(base + '/api' + url, { method,
    headers: { 'X-Lulu-Client': 'desktop', 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body) });
}
async function api(url, method = 'GET', body) {
  const response = await request(url, method, body);
  assert(response.ok, `${url}: ${response.status} ${response.ok ? '' : await response.text()}`);
  return response.json();
}
async function waitFor(fn, label, timeout = 20000) {
  const end = Date.now() + timeout;
  while (Date.now() < end) { if (await fn()) return; await new Promise(resolve => setTimeout(resolve, 100)); }
  throw Error('Timed out: ' + label);
}
const work = fixture => api(`/ai/workspaces/${fixture.workspace_id}`);
async function choose(fixture) {
  await page.getByRole('button', { name: '文案处理', exact: true }).click();
  await page.getByLabel('选择原文').selectOption(fixture.id);
  await waitFor(async () => await page.getByLabel('选择原文').inputValue() === fixture.id &&
    await page.getByLabel('待处理文案').inputValue() === fixture.transcript, 'source workspace loaded');
}
async function selectText(text) {
  const editor = page.getByLabel('待处理文案');
  await editor.evaluate((element, selected) => {
    const start = element.value.indexOf(selected);
    if (start < 0) throw Error('Selection fixture is missing');
    element.focus(); element.setSelectionRange(start, start + selected.length);
  }, text);
  await editor.press('Shift');
}
async function manager(tab) {
  await page.getByRole('button', { name: /^工具与技能/ }).click();
  const dialog = page.getByRole('dialog');
  await dialog.getByRole('button', { name: tab, exact: true }).click();
  return dialog;
}
async function closeManager() {
  if (await page.getByRole('dialog').count()) {
    await page.getByRole('dialog').getByRole('button', { name: '关闭窗口', exact: true }).last().click();
    await page.getByRole('dialog').waitFor({ state: 'hidden' });
  }
}
async function contextSettings(fixture, auto) {
  const dialog = await manager('上下文');
  await dialog.getByLabel('模型上下文窗口', { exact: true }).fill('8192');
  await dialog.getByLabel('自动压缩阈值', { exact: true }).selectOption('0.5');
  await dialog.getByLabel('自动压缩上下文', { exact: true }).setChecked(auto);
  await dialog.getByRole('button', { name: '保存上下文设置', exact: true }).click();
  await waitFor(async () => {
    const cfg = (await work(fixture)).agent_config;
    return cfg.context_window === 8192 && cfg.compact_threshold === .5 && cfg.auto_compact === auto;
  }, 'context configuration persisted');
  return dialog;
}
async function send(fixture, instruction, expected = 'done') {
  const old = (await work(fixture)).run_id;
  await page.getByLabel('文稿处理要求').fill(instruction);
  await page.getByRole('button', { name: '发送', exact: true }).click();
  let value;
  await waitFor(async () => {
    value = await work(fixture);
    if (value.run_id === old || !value.run || ['queued', 'running'].includes(value.run.status)) return false;
    assert.equal(value.run.status, expected, value.run.error || value.run.phase);
    return true;
  }, 'model run finished');
  return value;
}
async function compress(fixture, expected) {
  const previous = (await work(fixture)).compact_run?.id;
  await page.getByRole('dialog').getByRole('button', { name: '立即压缩', exact: true }).click();
  let value;
  await waitFor(async () => {
    value = await work(fixture);
    if (!value.compact_run || value.compact_run.id === previous || ['queued', 'running'].includes(value.compact_run.status)) return false;
    assert.equal(value.compact_run.status, expected, value.compact_run.error || value.compact_run.phase);
    return true;
  }, 'manual compression finished');
  return value;
}
async function createdDocuments() { return (await api('/state')).items.filter(item => item.title === createdTitle); }

try {
  app = await electron.launch({ args: ['electron/main.cjs'], cwd: root, timeout: 45000,
    env: { ...process.env, LULU_DATA_DIR: data, LULU_USER_DATA: path.join(data, 'electron-profile') } });
  page = await app.firstWindow();
  page.on('pageerror', error => errors.push(error.message));
  page.on('dialog', dialog => dialog.accept());
  page.on('request', request => {
    const target = new URL(request.url()).pathname;
    if (target === '/api/links' && request.method() === 'POST') collectionRequests.push(request.url());
    if (target === '/api/voice' && request.method() === 'POST') voiceRequests.push(request.url());
  });
  await page.waitForURL(url => url.hostname === '127.0.0.1');
  base = new URL(page.url()).origin;
  await api('/settings', 'PATCH', { llm_url: `http://127.0.0.1:${server.address().port}/v1`,
    llm_model: 'offline-runtime-fixture', llm_api_key: 'offline-only-not-a-real-key' });

  await choose(fixtures.context);
  const original = (await work(fixtures.context)).messages;
  await contextSettings(fixtures.context, true);
  await page.screenshot({ path: path.join(data, 'context-settings.png') });
  await closeManager();
  const automatic = await send(fixtures.context, '当前要求：在保留价格199元的前提下继续处理。');
  assert.deepEqual(automatic.messages.slice(0, original.length), original);
  assert.equal(automatic.messages.length, original.length + 2);
  assert.equal(automatic.context.checkpoint.through_count, original.length - 4);
  const input = JSON.stringify(requests.findLast(value => value.mode === 'auto' && !value.summary).body.messages);
  assert(input.includes('CTX_SUMMARY_SENTINEL'));
  assert(input.includes('历史要求_06') && input.includes('历史答复_06'));
  assert(input.includes('历史要求_07') && input.includes('历史答复_07'));
  assert(!input.includes('历史要求_00'));
  assert.equal((await api(`/items/${fixtures.context.id}`)).transcript, fixtures.context.transcript);
  await page.reload(); await choose(fixtures.context);
  assert.deepEqual((await work(fixtures.context)).context.checkpoint, automatic.context.checkpoint);
  checks.push('Automatic compaction preserves the complete log, retains two complete turns, sends the checkpoint to the real HTTP request, and survives native reload');

  await choose(fixtures.manual);
  const manualBefore = await work(fixtures.manual);
  await contextSettings(fixtures.manual, false);
  mode = 'manual-bad';
  const failed = await compress(fixtures.manual, 'error');
  assert.equal(failed.context.checkpoint, null);
  assert.deepEqual(failed.messages, manualBefore.messages);
  assert.equal(failed.run_id, manualBefore.run_id);
  assert(failed.compact_run.error.includes('摘要'));
  mode = 'manual-good';
  const compacted = await compress(fixtures.manual, 'done');
  assert(compacted.context.checkpoint);
  assert.deepEqual(compacted.messages, manualBefore.messages);
  assert.equal(compacted.run_id, manualBefore.run_id);
  assert.equal((await api(`/items/${fixtures.manual.id}`)).transcript, fixtures.manual.transcript);
  await closeManager();
  checks.push('Manual compression failure keeps the previous checkpoint and all history; retry succeeds without replacing the chat run or modifying the source');

  mode = 'plain';
  await send(fixtures.manual, '为取消验证保留第一轮新的交流。');
  await send(fixtures.manual, '为取消验证保留第二轮新的交流。');
  const beforeCancel = await work(fixtures.manual);
  assert(beforeCancel.context.checkpoint && beforeCancel.context.can_compact);
  let dialog = await manager('上下文');
  mode = 'cancel'; releaseSummary = null; summaryReleased = false;
  await dialog.getByRole('button', { name: '立即压缩', exact: true }).click();
  await waitFor(() => releaseSummary !== null, 'delayed summary request started');
  await dialog.getByRole('button', { name: '取消压缩', exact: true }).click();
  await waitFor(async () => (await work(fixtures.manual)).compact_run?.status === 'cancelled', 'compression cancellation settled');
  releaseSummary();
  await waitFor(() => summaryReleased, 'late model response released');
  const cancelled = await work(fixtures.manual);
  assert.deepEqual(cancelled.context.checkpoint, beforeCancel.context.checkpoint);
  assert.deepEqual(cancelled.messages, beforeCancel.messages);
  assert.equal(cancelled.run_id, beforeCancel.run_id);
  assert.equal(cancelled.compact_run.status, 'cancelled');
  await page.screenshot({ path: path.join(data, 'cancelled-compaction.png') });
  await closeManager();
  checks.push('Cancelling a pending summary inside Agent settings preserves the existing checkpoint and complete history; a released late model response cannot publish');

  await choose(fixtures.tools);
  dialog = await manager('工具');
  await dialog.getByLabel('启用工具调用', { exact: true }).setChecked(true);
  for (const tool of ['search_documents', 'read_document', 'create_document']) await dialog.getByLabel(`允许工具 ${tool}`, { exact: true }).setChecked(true);
  await dialog.getByRole('button', { name: '保存工具设置', exact: true }).click();
  await waitFor(async () => (await work(fixtures.tools)).agent_config.allowed_tools.includes('create_document'), 'tool permissions saved');
  await page.screenshot({ path: path.join(data, 'tool-settings.png') });
  await closeManager();
  mode = 'tools'; toolStep = 0;
  const withTools = await send(fixtures.tools, '检索可检索参考，阅读后准备另存一篇新文稿。');
  assert.equal(toolStep, 3);
  assert.equal(withTools.run.actions.length, 1);
  const action = withTools.run.actions[0];
  assert.equal(action.state, 'pending');
  assert.equal((await createdDocuments()).length, 0);
  assert.equal((await api(`/items/${fixtures.tools.id}`)).transcript, fixtures.tools.transcript);
  await page.locator('.agent-tool-event').first().waitFor();
  assert.equal(await page.locator('button').filter({ hasText: /^查看入口$/ }).count(), 0, 'Confirmation results must not expose an empty handoff action');
  const card = page.locator('.agent-pending-action[data-state="pending"]').first();
  await card.getByRole('button', { name: '确认执行', exact: true }).waitFor();
  await page.screenshot({ path: path.join(data, 'pending-tool-action.png') });
  await card.getByRole('button', { name: '确认执行', exact: true }).click();
  await waitFor(async () => (await createdDocuments()).length === 1, 'explicitly confirmed new document');
  const repeated = await api(`/ai/workspaces/${withTools.id}/runs/${withTools.run.id}/actions/${action.id}/confirm`, 'POST', { revision: withTools.revision });
  assert.equal(repeated.idempotent, true);
  assert.equal((await createdDocuments()).length, 1);
  assert.equal((await api(`/items/${repeated.document_id}`)).transcript, createdText);
  assert.equal((await api(`/items/${fixtures.tools.id}`)).transcript, fixtures.tools.transcript);
  checks.push('Actual tool-call/result envelopes search and read local data; a new document is only created after explicit confirmation and repeated confirmation is idempotent');

  mode = 'unsupported';
  const rejected = await send(fixtures.tools, '检查当前工具模型兼容性。', 'error');
  assert(rejected.run.error.includes('关闭工具'));
  dialog = await manager('工具');
  await dialog.getByLabel('启用工具调用', { exact: true }).setChecked(false);
  await dialog.getByRole('button', { name: '保存工具设置', exact: true }).click();
  await waitFor(async () => !(await work(fixtures.tools)).agent_config.tools_enabled, 'tools disabled');
  await closeManager();
  mode = 'plain';
  const plain = await send(fixtures.tools, '关闭工具后，只返回普通全文处理结果。');
  assert(plain.run.result.includes('普通全文处理完成'));
  assert.equal((await api(`/items/${fixtures.tools.id}`)).transcript, fixtures.tools.transcript);
  checks.push('Unsupported tool protocol produces a useful error; disabling tools restores the existing text-only workflow without altering the source');

  dialog = await manager('工具');
  await dialog.getByLabel('启用工具调用', { exact: true }).setChecked(true);
  await dialog.getByLabel('允许工具 prepare_transcription', { exact: true }).setChecked(true);
  await dialog.getByRole('button', { name: '保存工具设置', exact: true }).click();
  await waitFor(async () => (await work(fixtures.tools)).agent_config.tools_enabled, 'handoff tool enabled');
  await closeManager();
  mode = 'handoff';
  const itemsBeforeHandoff = (await api('/state')).items.length;
  await send(fixtures.tools, '把这个作品链接带到文案提取页，稍后由我开始。');
  const handoffEvent = page.locator('.agent-tool-event').filter({ hasText: '准备转录' });
  await handoffEvent.locator('summary').click();
  await handoffEvent.getByRole('button', { name: '前往文案提取', exact: true }).click();
  await waitFor(async () => await page.getByLabel('作品链接', { exact: true }).inputValue() === handoffUrl, 'handoff URL filled');
  assert.equal((await api('/state')).items.length, itemsBeforeHandoff);
  assert.deepEqual(collectionRequests, []);
  checks.push('Transcription handoff fills the supplied URL in the existing extraction page without adding tasks or issuing a collection request');

  await choose(fixtures.tools);
  const oldManualVoice = { source: '', text: '先前保存的手动配音草稿。', voice: '', rate: '0' };
  const oldSourceVoice = { source: fixtures.tools.id, text: '这个来源的旧配音草稿也要保留。', voice: '', rate: '0' };
  await api('/drafts/voice-session', 'PUT', { source: '' });
  await api('/drafts/voice-draft-manual', 'PUT', oldManualVoice);
  await api(`/drafts/voice-draft-${fixtures.tools.id}`, 'PUT', oldSourceVoice);
  const selectedVoiceText = '查找参考资料';
  await selectText(selectedVoiceText);
  await page.getByRole('button', { name: '仅处理选中片段', exact: true }).click();
  dialog = await manager('工具');
  await dialog.getByLabel('允许工具 prepare_voice', { exact: true }).setChecked(true);
  await dialog.getByRole('button', { name: '保存工具设置', exact: true }).click();
  await waitFor(async () => (await work(fixtures.tools)).agent_config.allowed_tools.includes('prepare_voice'), 'voice handoff tool enabled');
  await closeManager();
  mode = 'voice';
  const voiceRun = await send(fixtures.tools, '把本轮选中片段带到配音页，保留之前的配音草稿。');
  assert.equal(voiceRun.run.input_text, selectedVoiceText);
  assert.equal(voiceRun.run.scope, 'selection');
  const voiceEvent = page.locator('.agent-tool-event').filter({ hasText: '准备配音' });
  await voiceEvent.locator('summary').click();
  const beforeVoiceItems = (await api('/state')).items.length;
  await voiceEvent.getByRole('button', { name: '前往配音', exact: true }).click();
  await waitFor(async () => await page.getByLabel('配音正文', { exact: true }).inputValue() === selectedVoiceText, 'frozen selected voice text filled');
  await page.getByTestId('voice-draft-status').filter({ hasText: '原配音草稿保留' }).waitFor();
  assert.deepEqual(await api('/drafts/voice-session'), { source: '' });
  assert.deepEqual(await api('/drafts/voice-draft-manual'), oldManualVoice);
  assert.deepEqual(await api(`/drafts/voice-draft-${fixtures.tools.id}`), oldSourceVoice);
  assert.deepEqual(voiceRequests, []);
  assert.equal((await api('/state')).items.length, beforeVoiceItems);
  await page.screenshot({ path: path.join(data, 'voice-selected-handoff.png') });
  checks.push('Voice handoff uses the frozen selection, preserves both previous manual and source voice drafts, and never generates audio automatically');

  await choose(fixtures.skill);
  dialog = await manager('技能');
  await dialog.getByLabel('导入技能文件', { exact: true }).setInputFiles(importedSkill);
  await dialog.getByText('安装预览', { exact: true }).waitFor();
  assert(!(await api('/ai/skills')).entries.some(entry => entry.name === skillName && entry.installed));
  await dialog.getByRole('button', { name: '确认安装', exact: true }).click();
  await waitFor(async () => (await api('/ai/skills')).entries.some(entry => entry.name === skillName && entry.installed && entry.enabled), 'skill installed');
  await dialog.locator(`.agent-skill-card[data-name="${skillName}"]`).waitFor();
  await page.screenshot({ path: path.join(data, 'skill-manager.png') });
  await closeManager();
  await page.getByLabel('本轮技能', { exact: true }).selectOption(skillName);
  mode = 'skill';
  const skillRun = await send(fixtures.skill, '按指定的方法检查价格。');
  assert.equal(skillRun.run.explicit_skill, skillName);
  assert(skillRun.run.loaded_skills.includes(skillName));
  assert(skillRun.run.result.startsWith('技能已读取'));
  assert.equal((await api(`/items/${fixtures.skill.id}`)).transcript, fixtures.skill.transcript);
  dialog = await manager('技能');
  await dialog.locator(`.agent-skill-card[data-name="${skillName}"]`).getByRole('button', { name: '停用', exact: true }).click();
  await waitFor(async () => !(await api('/ai/skills')).entries.find(entry => entry.name === skillName).enabled, 'skill disabled');
  await closeManager();
  const countBeforeDisabled = requests.length;
  const disabled = await request(`/ai/workspaces/${skillRun.id}/runs`, 'POST', { revision: skillRun.revision, instruction: '不应再加载已停用技能。', skill: skillName });
  assert([400, 409].includes(disabled.status));
  assert.equal(requests.length, countBeforeDisabled);
  checks.push('Local SKILL.md preview does not install automatically; confirmed skill reaches the actual model request and disabling it blocks later use');

  const oldSkill = await api(`/ai/skills/${skillName}`);
  await fs.writeFile(importedSkill, skillContent.replace('SKILL_METHOD_SENTINEL_42', 'SKILL_REPLACEMENT_SENTINEL_99'));
  dialog = await manager('技能');
  await dialog.getByLabel('导入技能文件', { exact: true }).setInputFiles(importedSkill);
  await dialog.getByLabel('替换现有技能', { exact: true }).setChecked(true);
  await dialog.getByRole('button', { name: '确认安装', exact: true }).click();
  await waitFor(async () => (await api(`/ai/skills/${skillName}`)).content.includes('SKILL_REPLACEMENT_SENTINEL_99'), 'replacement skill installed');
  const replacement = await api(`/ai/skills/${skillName}`);
  assert.notEqual(replacement.generation, oldSkill.generation);
  const history = (await api(`/ai/skills/${skillName}/versions`)).entries;
  const originalVersion = history.find(version => version.digest === oldSkill.digest);
  assert(originalVersion, 'Replacing a skill must retain its previous package');
  await dialog.locator(`.agent-skill-card[data-name="${skillName}"]`).getByRole('button', { name: '查看技能', exact: true }).click();
  await dialog.getByLabel('技能历史版本', { exact: true }).selectOption(originalVersion.id);
  await waitFor(async () => (await dialog.locator('.agent-skill-preview pre').innerText()).includes('SKILL_METHOD_SENTINEL_42'), 'old skill content previewed');
  assert.equal((await api(`/ai/skills/${skillName}`)).digest, replacement.digest, 'Preview alone cannot replace the current skill');
  await dialog.getByRole('button', { name: '恢复此版本', exact: true }).click();
  await waitFor(async () => (await api(`/ai/skills/${skillName}`)).digest === oldSkill.digest, 'skill historical version restored');
  const restoredSkill = await api(`/ai/skills/${skillName}`);
  assert.equal(restoredSkill.content, oldSkill.content);
  assert.notEqual(restoredSkill.generation, replacement.generation);
  assert((await api(`/ai/skills/${skillName}/versions`)).entries.some(version => version.digest === replacement.digest));
  await page.screenshot({ path: path.join(data, 'restored-skill-version.png') });
  await closeManager();
  checks.push('Confirmed same-name skill replacement preserves history; preview is read-only and restoring a version changes generation while retaining the replaced package');

  await choose(fixtures.brand);
  dialog = await manager('技能');
  await dialog.locator('.agent-skill-card[data-name="lulu-translate"]').getByRole('button', { name: '查看技能', exact: true }).click();
  await dialog.getByLabel('固定保留的品牌名', { exact: true }).fill('米家 Pro\n');
  await dialog.getByRole('button', { name: '保存技能偏好', exact: true }).click();
  await waitFor(async () => JSON.stringify((await api('/ai/skills/lulu-translate')).config.protected_terms) === '["米家 Pro"]', 'brand preference persisted without blank lines');
  await page.screenshot({ path: path.join(data, 'protected-brands-settings.png') });
  await closeManager();
  await page.reload(); await choose(fixtures.brand);
  await page.getByLabel('本轮技能', { exact: true }).selectOption('lulu-translate');
  mode = 'brand';
  const translated = await send(fixtures.brand, '将正文翻译为英文。');
  const translatedText = translated.run.result;
  assert.equal(brandStep, 2, 'The invalid first output should get exactly one repair attempt');
  assert.equal(translatedText.match(/松果X1/g).length, 2);
  assert(translatedText.includes('米家 Pro') && !translatedText.includes('BRANDLOCK_'));
  assert(!translated.messages.some(message => message.text.includes('Pinecone')));
  await waitFor(async () => (await page.getByLabel('已锁定品牌名').innerText()).includes('松果X1'), 'visible brand inventory');
  await page.getByRole('button', { name: '另存结果', exact: true }).click();
  await waitFor(async () => !!(await work(fixtures.brand)).run.saved_output_id, 'restored translation saved');
  const output = await api(`/items/${(await work(fixtures.brand)).run.saved_output_id}`);
  assert.equal(output.transcript, translatedText);
  assert.equal((await api(`/items/${fixtures.brand.id}`)).transcript, fixtures.brand.transcript);
  assert.deepEqual(output.metadata.protected_brands.sort(), ['松果X1', '米家 Pro'].sort());
  await choose(fixtures.brand);
  await page.screenshot({ path: path.join(data, 'brand-translation-result.png') });
  checks.push('Brand preferences survive reload; invalid translation is withheld and retried once; restored names and repeated mentions render and save exactly');

  await choose(fixtures.proof);
  await page.getByLabel('本轮技能', { exact: true }).selectOption('lulu-proofread');
  mode = 'proof-bad';
  const blocked = await send(fixtures.proof, '只校正错别字，保持其余内容。', 'error');
  assert.equal(blocked.run.proofreading_check.reason, 'protected_tokens');
  assert.equal(blocked.run.result, '');
  assert.equal(blocked.text, fixtures.proof.transcript);
  const refused = page.getByLabel('未通过的校对回复');
  await refused.locator('summary').click();
  assert((await refused.innerText()).includes('299'));
  assert.equal(await page.getByRole('button', { name: '应用到文稿', exact: true }).count(), 0);
  assert.equal((await request(`/ai/workspaces/${blocked.id}/results/${blocked.run.id}/save-output`, 'POST')).status, 400);
  await page.screenshot({ path: path.join(data, 'strict-proofreading-blocked.png') });
  await page.reload(); await choose(fixtures.proof);
  assert((await page.getByLabel('严格校对状态').innerText()).includes('未通过'));
  checks.push('Strict proofreading rejects changed numbers and URLs, preserves the read-only reply across reload and blocks save/apply without changing the draft');

  mode = 'proof-good';
  const corrected = await send(fixtures.proof, '仅把测式校正为测试，其余逐字保留。');
  assert.equal(corrected.run.proofreading_check.state, 'passed');
  await waitFor(async () => (await page.getByLabel('严格校对状态').innerText()).includes('通过：'), 'strict check passed in UI');
  await page.getByRole('button', { name: '另存结果', exact: true }).last().click();
  await waitFor(async () => !!(await work(fixtures.proof)).run.saved_output_id, 'strict result saved');
  const correctedItem = await api(`/items/${(await work(fixtures.proof)).run.saved_output_id}`);
  assert.equal(correctedItem.transcript, fixtures.proof.transcript.replace('测式', '测试'));
  assert.equal((await api(`/items/${fixtures.proof.id}`)).transcript, fixtures.proof.transcript);
  await choose(fixtures.proof);
  checks.push('A small valid correction passes numeric, URL and similarity checks and can be saved without changing its source');

  dialog = await manager('技能');
  await dialog.locator('[data-name="lulu-proofread"]').getByRole('button', { name: '查看技能', exact: true }).click();
  assert(await dialog.getByLabel('严格校对', { exact: true }).isChecked());
  await dialog.getByLabel('严格校对', { exact: true }).setChecked(false);
  await dialog.getByRole('button', { name: '保存技能偏好', exact: true }).click();
  await waitFor(async () => (await api('/ai/skills/lulu-proofread')).config.strict === false, 'strict preference persisted');
  await closeManager();
  await page.reload(); await choose(fixtures.proof);
  mode = 'proof-disabled';
  const relaxed = await send(fixtures.proof, '本次按已保存的偏好处理正文。');
  assert(!relaxed.run.proofreading_check);
  assert(relaxed.run.result.includes('299'));
  assert.equal((await api(`/items/${fixtures.proof.id}`)).transcript, fixtures.proof.transcript);
  checks.push('Strict mode is a visible, persistent proofreading preference; disabling it affects the next run without silently modifying any source');

  dialog = await manager('工具');
  const nativeWindow = await app.browserWindow(page);
  await nativeWindow.evaluate(window => window.setSize(900, 620));
  await waitFor(() => page.evaluate(() => window.innerWidth <= 900), 'small native window resized');
  const bounds = await dialog.boundingBox(), saveBounds = await dialog.getByRole('button', { name: '保存工具设置', exact: true }).boundingBox();
  assert(bounds.width <= await page.evaluate(() => window.innerWidth) && bounds.height <= await page.evaluate(() => window.innerHeight));
  assert(saveBounds.y >= bounds.y && saveBounds.y + saveBounds.height <= bounds.y + bounds.height, 'Tool settings save button must remain in the visible dialog');
  await page.screenshot({ path: path.join(data, 'small-tool-settings.png') });
  await closeManager();
  checks.push('Agent manager remains usable in a 900×620 native window with visible save action');

  assert.deepEqual(modelErrors, []); assert.deepEqual(errors, []);
  const result = { passed: checks.length, checks, requests: requests.length,
    summary_requests: requests.filter(value => value.summary).length, errors,
    scope: 'Actual Windows Electron, isolated synthetic library and local HTTP model; no cloud requests or production data' };
  await fs.writeFile(path.join(data, 'result.json'), JSON.stringify(result, null, 2));
  console.log(JSON.stringify({ ...result, evidence_dir: data }, null, 2));
} catch (error) {
  if (page) await page.screenshot({ path: path.join(data, 'failure.png') }).catch(() => {});
  console.error('Isolated acceptance artifacts: ' + data);
  throw error;
} finally {
  if (releaseSummary) releaseSummary();
  if (app) await app.close();
  server.closeAllConnections();
  await new Promise(resolve => server.close(resolve));
}
