// Actual Windows Electron + isolated API/database + local-only model responses.
// Requires npm run build. Never opens a platform or downloads model weights.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import http from 'node:http';
import { spawnSync } from 'node:child_process';
import { _electron as electron } from 'playwright';

const root = process.cwd();
await fs.mkdir(path.join(root, '.test-data'), { recursive: true });
const data = await fs.mkdtemp(path.join(root, '.test-data', 'agent-desktop-'));
const seeded = spawnSync(path.join(root, '.venv/Scripts/python.exe'), ['-X', 'utf8', '-m', 'tests.agent_fixture'], {
  cwd: root, windowsHide: true, encoding: 'utf8', env: { ...process.env, LULU_DATA_DIR: data },
});
assert.equal(seeded.status, 0, seeded.stderr);
const fixtures = JSON.parse(seeded.stdout);
const requests = [], responses = [], modelErrors = [], checks = [], errors = [];
const modelServer = http.createServer(async (req, res) => {
  try {
    assert.equal(req.url, '/v1/chat/completions');
    let body = ''; for await (const part of req) body += part;
    const request = JSON.parse(body);
    assert.equal(request.model, 'offline-agent-fixture');
    requests.push(request);
    assert(responses.length, 'Unexpected model request');
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ choices: [{ message: { content: responses.shift() }, finish_reason: 'stop' }] }));
  } catch (error) {
    modelErrors.push(error.message);
    res.writeHead(500, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ error: { message: 'Offline fixture rejected unexpected request' } }));
  }
});
await new Promise(resolve => modelServer.listen(0, '127.0.0.1', resolve));
let app, page, base;
async function api(url, method = 'GET', body) {
  const response = await fetch(base + '/api' + url, { method,
    headers: { 'X-Lulu-Client': 'desktop', 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body) });
  assert(response.ok, `${url}: ${response.status} ${response.ok ? '' : await response.text()}`);
  return response.json();
}
async function waitFor(fn, label, timeout = 15000) {
  const end = Date.now() + timeout;
  while (Date.now() < end) { if (await fn()) return; await new Promise(resolve => setTimeout(resolve, 100)); }
  throw Error('Timed out: ' + label);
}
async function workspace(source) {
  const sessions = await api('/ai/workspaces?source_id=' + encodeURIComponent(source));
  assert(sessions.length, 'No workspace for source');
  return api('/ai/workspaces/' + sessions[0].id);
}
async function chooseSource(source, text) {
  await page.getByRole('button', { name: '文案处理', exact: true }).click();
  await page.getByLabel('选择原文').selectOption(source);
  await waitFor(async () => await page.getByLabel('选择原文').inputValue() === source &&
    await page.getByLabel('待处理文案').inputValue() === text, 'selected source loaded');
}
async function selectText(text, last = false) {
  const editor = page.getByLabel('待处理文案');
  await editor.evaluate((el, options) => {
    const start = options.last ? el.value.lastIndexOf(options.text) : el.value.indexOf(options.text);
    if (start < 0) throw Error('Selection fixture is missing');
    el.focus(); el.setSelectionRange(start, start + options.text.length);
    el.dispatchEvent(new Event('select', { bubbles: true }));
  }, { text, last });
  // React's selection plugin listens to keyboard/selectionchange, not a
  // synthetic `select` event. A real modifier key preserves the exact range.
  await editor.press('Shift');
}
async function send(source, answer, instruction) {
  responses.push(answer);
  const previous = (await workspace(source)).run_id;
  await page.getByLabel('文稿处理要求').fill(instruction);
  await page.getByRole('button', { name: '发送', exact: true }).click();
  let result;
  await waitFor(async () => {
    result = await workspace(source);
    assert(!result.run || !['error', 'cancelled'].includes(result.run.status), result.run?.error);
    return result.run_id !== previous && result.run?.status === 'done';
  }, 'completed model response');
  await page.locator('.ai-message.assistant').last().waitFor();
  return result;
}
async function openProposals() {
  await page.getByRole('button', { name: '逐项建议', exact: true }).last().click();
  const dialog = page.getByRole('dialog');
  await dialog.locator('.ai-proposal').first().waitFor();
  return dialog;
}
async function closeDialog() {
  await page.getByRole('dialog').getByRole('button', { name: '关闭窗口', exact: true }).click();
  await page.getByRole('dialog').waitFor({ state: 'hidden' });
}

try {
  app = await electron.launch({ args: ['electron/main.cjs'], cwd: root, timeout: 45000,
    env: { ...process.env, LULU_DATA_DIR: data, LULU_USER_DATA: path.join(data, 'electron-profile') } });
  page = await app.firstWindow();
  page.on('pageerror', error => errors.push(error.message));
  page.on('dialog', dialog => dialog.accept());
  await page.waitForURL(url => url.hostname === '127.0.0.1');
  base = new URL(page.url()).origin;
  await api('/settings', 'PATCH', { llm_url: `http://127.0.0.1:${modelServer.address().port}/v1`,
    llm_model: 'offline-agent-fixture', llm_api_key: 'offline-only-not-a-real-key' });
  await chooseSource(fixtures.repeat, fixtures.repeat_text);

  await page.getByRole('button', { name: '添加参考素材', exact: true }).click();
  let dialog = page.getByRole('dialog');
  await dialog.getByLabel('选择参考文稿').selectOption(fixtures.reference);
  await waitFor(async () => await dialog.getByLabel('参考文稿内容').inputValue() === fixtures.reference_text, 'reference text loaded');
  assert.equal(await dialog.getByLabel('参考文稿内容').evaluate(el => el.readOnly), true);
  await dialog.getByRole('button', { name: '引用全文', exact: true }).click();
  await waitFor(async () => (await workspace(fixtures.repeat)).references.length === 1, 'reference saved');
  await page.reload();
  await chooseSource(fixtures.repeat, fixtures.repeat_text);
  const before = await workspace(fixtures.repeat);
  assert.equal(before.references[0].text, fixtures.reference_text);
  assert.equal(before.references[0].source_id, fixtures.reference);
  assert.equal(before.references[0].stale, false);
  checks.push('External reference snapshot persists across native window reload');

  await selectText('中间🧭只读。');
  await page.getByRole('button', { name: '引用选中片段', exact: true }).click();
  await waitFor(async () => (await workspace(fixtures.repeat)).references.length === 2, 'current draft reference saved');

  const selected = '这句话需要改。';
  await selectText(selected, true);
  await page.getByRole('button', { name: '仅处理选中片段', exact: true }).click();
  const sendBounds = await page.getByRole('button', { name: '发送', exact: true }).boundingBox();
  assert(sendBounds.y + sendBounds.height <= await page.evaluate(() => window.innerHeight), 'Desktop composer with references and selection must fit within the native window');
  const scoped = await send(fixtures.repeat, '這句話已經改好。', '只修改选中片段，参考资料仅作核对。');
  assert.equal(scoped.run.scope, 'selection');
  assert.equal(scoped.run.input_text, selected);
  const expectedStart = Buffer.byteLength(fixtures.repeat_text.slice(0, fixtures.repeat_text.lastIndexOf(selected)), 'utf8');
  assert.equal(scoped.run.selection.start, expectedStart);
  assert.equal(scoped.run.selection.end, expectedStart + Buffer.byteLength(selected, 'utf8'));
  const sent = JSON.stringify(requests[0].messages);
  assert(sent.includes(fixtures.reference_text));
  assert(sent.includes('中间🧭只读。'));
  assert(!sent.includes('开场😀请保留。') && !sent.includes('收尾保持原样。'), 'Out-of-scope main text must not be sent');
  assert.equal(await page.getByRole('button', { name: '应用到文稿', exact: true }).count(), 0);
  dialog = await openProposals();
  const scopedPlan = await api(`/ai/workspaces/${scoped.id}/results/${scoped.run_id}/proposals`);
  const beforeFragment = scopedPlan.proposals[0].before;
  assert.notEqual(fixtures.repeat_text.indexOf(beforeFragment), fixtures.repeat_text.lastIndexOf(beforeFragment), 'The locate test must contain duplicate exact fragments');
  await dialog.locator('.ai-proposal').first().getByRole('button', { name: '定位原文', exact: true }).click();
  await page.getByRole('dialog').waitFor({ state: 'hidden' });
  await waitFor(async () => await page.getByLabel('待处理文案').evaluate((el, expected) =>
    el.selectionStart === expected.start && el.selectionEnd === expected.end,
  { start: fixtures.repeat_text.lastIndexOf(beforeFragment), end: fixtures.repeat_text.lastIndexOf(beforeFragment) + beforeFragment.length }), 'located exact second occurrence across UTF-8/UTF-16 boundaries');
  dialog = await openProposals();
  await page.screenshot({ path: path.join(data, 'selection-proposals.png') });
  await dialog.locator('.ai-proposal').first().getByRole('button', { name: '采纳到草稿', exact: true }).click();
  const selectedResult = fixtures.repeat_text.slice(0, fixtures.repeat_text.lastIndexOf(selected)) +
    '这句话已经改好。' + fixtures.repeat_text.slice(fixtures.repeat_text.lastIndexOf(selected) + selected.length);
  await waitFor(async () => (await workspace(fixtures.repeat)).text === selectedResult, 'selected proposal accepted');
  assert.equal((await api(`/items/${fixtures.repeat}`)).transcript, fixtures.repeat_text);
  assert.equal((await api(`/items/${fixtures.reference}`)).transcript, fixtures.reference_text);
  await closeDialog();
  assert.equal(await page.getByLabel('待处理文案').inputValue(), selectedResult);
  await page.getByRole('button', { name: '保存文稿', exact: true }).click();
  await waitFor(async () => (await api(`/items/${fixtures.repeat}`)).transcript === selectedResult, 'explicit source save');
  assert((await api(`/items/${fixtures.repeat}/versions`)).length > 0);
  checks.push('UTF-8 emoji/repeated-text selection edits only its exact occurrence; references stay read-only; source changes only after save');

  await chooseSource(fixtures.timed, fixtures.timed_text);
  const timed = await send(fixtures.timed, '第一句清晰表达。\n第二句准确收尾。', '逐句校正并保留原来两行。');
  const proposalPath = `/ai/workspaces/${timed.id}/results/${timed.run_id}/proposals`;
  const originalProposals = await api(proposalPath);
  assert.equal(originalProposals.proposals.length, 2);
  dialog = await openProposals();
  await dialog.locator('.ai-proposal').first().getByRole('button', { name: '采纳到草稿', exact: true }).click();
  await waitFor(async () => (await workspace(fixtures.timed)).text === '第一句清晰表达。\n第二句保留。', 'first independent proposal');
  await dialog.getByRole('button', { name: '撤销最近采纳', exact: true }).click();
  await waitFor(async () => (await workspace(fixtures.timed)).text === fixtures.timed_text, 'undo latest accepted proposal');
  for (let index = 0; index < 2; index++) {
    await dialog.locator('.ai-proposal').nth(index).getByRole('button', { name: '采纳到草稿', exact: true }).click();
    await waitFor(async () => (await api(proposalPath)).proposals[index].state === 'accepted', `accepted proposal ${index + 1}`);
  }
  assert.equal((await workspace(fixtures.timed)).text, '第一句清晰表达。\n第二句准确收尾。');
  assert.equal((await api(`/items/${fixtures.timed}`)).transcript, fixtures.timed_text);
  await page.screenshot({ path: path.join(data, 'accepted-proposals.png') });
  await closeDialog();
  await page.getByRole('button', { name: '保存文稿', exact: true }).click();
  await waitFor(async () => (await api(`/items/${fixtures.timed}`)).transcript === '第一句清晰表达。\n第二句准确收尾。', 'timed source saved');
  const saved = await api(`/items/${fixtures.timed}`);
  assert.deepEqual(saved.segments.map(segment => [segment.start, segment.end, segment.speaker_id]), [[0, 3, 1], [3, 6, 2]]);
  const versions = await api(`/items/${fixtures.timed}/versions`);
  assert(versions.length > 0);
  await api(`/items/${fixtures.timed}/versions/${versions[0].id}/restore`, 'POST');
  assert.equal((await api(`/items/${fixtures.timed}`)).transcript, fixtures.timed_text);
  checks.push('Sequential proposals rebase safely, undo restores draft, explicit save preserves timestamps/speakers and history restoration');

  await chooseSource(fixtures.conflict, fixtures.conflict_text);
  const conflict = await send(fixtures.conflict, '第一点新。\n第二点新。', '逐句改成新表述，保留两行。');
  dialog = await openProposals();
  await dialog.locator('.ai-proposal').last().getByRole('button', { name: '忽略', exact: true }).click();
  const conflictPath = `/ai/workspaces/${conflict.id}/results/${conflict.run_id}/proposals`;
  await waitFor(async () => (await api(conflictPath)).proposals.at(-1).state === 'dismissed', 'dismissed suggestion');
  assert.equal((await workspace(fixtures.conflict)).text, fixtures.conflict_text);
  await closeDialog();
  assert.equal(await page.getByRole('button', { name: '应用到文稿', exact: true }).count(), 0, 'Whole-result apply must not bypass an ignored proposal');
  const ignoredState = await workspace(fixtures.conflict);
  const ignoredApply = await fetch(base + `/api/ai/workspaces/${conflict.id}/results/${conflict.run_id}/apply`, {
    method: 'POST', headers: { 'X-Lulu-Client': 'desktop', 'Content-Type': 'application/json' },
    body: JSON.stringify({ revision: ignoredState.revision }),
  });
  assert([400, 409].includes(ignoredApply.status));
  assert.equal((await api(`/items/${fixtures.conflict}`)).transcript, fixtures.conflict_text);
  const manualText = '用户自己保留的内容😀。\n第二点旧。';
  await page.getByLabel('待处理文案').fill(manualText);
  await waitFor(async () => (await workspace(fixtures.conflict)).text === manualText, 'manual edit autosaved');
  dialog = await openProposals();
  assert.equal((await api(conflictPath)).can_apply, false);
  assert.equal(await dialog.locator('.ai-proposal').first().getByRole('button', { name: '采纳到草稿', exact: true }).isDisabled(), true);
  assert.equal((await workspace(fixtures.conflict)).text, manualText);
  const windowHandle = await app.browserWindow(page);
  await windowHandle.evaluate(window => window.setSize(900, 620));
  await waitFor(() => page.evaluate(() => window.innerWidth <= 900), 'small native window resized');
  const bounds = await dialog.boundingBox();
  assert(bounds.width <= await page.evaluate(() => window.innerWidth) && bounds.height <= await page.evaluate(() => window.innerHeight), 'Suggestion dialog must fit a small native window');
  const returnBounds = await dialog.getByRole('button', { name: '返回文稿', exact: true }).boundingBox();
  assert(returnBounds.y >= bounds.y && returnBounds.y + returnBounds.height <= bounds.y + bounds.height, 'Return action must remain visible without scrolling the whole dialog');
  await page.screenshot({ path: path.join(data, 'stale-small-window.png') });
  await closeDialog();
  checks.push('Ignored suggestions do not change text; manual edits disable stale writes; proposal dialog works at minimum window size');

  await windowHandle.evaluate(window => window.setSize(1370, 910));
  await chooseSource(fixtures.repeat, selectedResult);
  await api(`/items/${fixtures.reference}`, 'PATCH', { transcript: '参考事实已由用户更新：价格 299 元。' });
  await waitFor(async () => (await workspace(fixtures.repeat)).references[0].stale, 'reference became stale');
  await page.getByText('引用已变化，请查看并重新添加，或移除后发送。', { exact: true }).waitFor();
  await page.getByLabel('文稿处理要求').fill('确认过期引用不能发送');
  assert.equal(await page.getByRole('button', { name: '发送', exact: true }).isDisabled(), true);
  const latest = await workspace(fixtures.repeat), callCount = requests.length;
  const blocked = await fetch(base + `/api/ai/workspaces/${latest.id}/runs`, {
    method: 'POST', headers: { 'X-Lulu-Client': 'desktop', 'Content-Type': 'application/json' },
    body: JSON.stringify({ revision: latest.revision, instruction: '不要对已过期引用发起请求', reference_ids: [latest.references[0].id] }),
  });
  assert([400, 409].includes(blocked.status));
  assert.equal(requests.length, callCount);
  assert.equal((await api(`/items/${fixtures.repeat}`)).transcript, selectedResult);
  checks.push('Changed reference is explicitly stale; rejected send causes no model request or source write');

  assert.deepEqual(errors, []); assert.deepEqual(modelErrors, []); assert.equal(responses.length, 0);
  assert.equal(requests.length, 3);
  const result = { checks, requests: requests.length, errors,
    scope: 'Actual Windows Electron with isolated synthetic documents and local model fixture; no real cloud model or user data' };
  await fs.writeFile(path.join(data, 'result.json'), JSON.stringify(result, null, 2));
  console.log(JSON.stringify({ passed: checks.length, ...result, evidence_dir: data }, null, 2));
} catch (error) {
  if (page) await page.screenshot({ path: path.join(data, 'failure.png') }).catch(() => {});
  console.error('Isolated acceptance artifacts: ' + data);
  throw error;
} finally {
  if (app) await app.close();
  modelServer.closeAllConnections();
  await new Promise(resolve => modelServer.close(resolve));
}
