// Actual Windows Electron, isolated library and local-only model fixture.
// Run after npm run build. No model downloads, live platforms or real credentials.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import http from 'node:http';
import { spawnSync } from 'node:child_process';
import { _electron as electron } from 'playwright';

const root = process.cwd();
await fs.mkdir(path.join(root, '.test-data'), { recursive: true });
const data = await fs.mkdtemp(path.join(root, '.test-data', 'upstream-desktop-'));
const media = path.join(data, 'synthetic.mp4');
const ffmpeg = spawnSync('ffmpeg', ['-hide_banner', '-loglevel', 'error', '-y', '-f', 'lavfi', '-i',
  'testsrc2=s=640x360:r=24:d=6', '-f', 'lavfi', '-i', 'sine=frequency=440:duration=6',
  '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-shortest', media], { encoding: 'utf8', windowsHide: true });
assert.equal(ffmpeg.status, 0, ffmpeg.stderr);
const seed = spawnSync(path.join(root, '.venv/Scripts/python.exe'), ['-X', 'utf8', '-m', 'tests.upstream_fixture'], {
  cwd: root, env: { ...process.env, LULU_DATA_DIR: data, FIXTURE_VIDEO: media }, encoding: 'utf8', windowsHide: true,
});
assert.equal(seed.status, 0, seed.stderr);
const ids = JSON.parse(seed.stdout);
let calls = 0;
const modelServer = http.createServer(async (req, res) => {
  assert.equal(req.url, '/v1/chat/completions');
  let body = ''; for await (const part of req) body += part;
  const request = JSON.parse(body); assert.equal(request.model, 'offline-fixture');
  calls++;
  res.writeHead(200, { 'Content-Type': 'application/json' });
  res.end(JSON.stringify({ choices: [{ message: { content: '這是第一句修正。\n這是第二句修正。' }, finish_reason: 'stop' }] }));
});
await new Promise(resolve => modelServer.listen(0, '127.0.0.1', resolve));
let app, page, base;
const checks = [], errors = [];
async function api(url, method = 'GET', body) {
  const response = await fetch(base + '/api' + url, { method,
    headers: { 'X-Lulu-Client': 'desktop', 'Content-Type': 'application/json' }, body: body === undefined ? undefined : JSON.stringify(body) });
  assert(response.ok, `${url}: ${response.status} ${response.ok ? '' : await response.text()}`);
  return response.json();
}
async function waitFor(fn, label, timeout = 15000) {
  const end = Date.now() + timeout;
  while (Date.now() < end) { if (await fn()) return; await new Promise(resolve => setTimeout(resolve, 100)); }
  throw Error('Timed out: ' + label);
}
try {
  app = await electron.launch({ args: ['electron/main.cjs'], cwd: root, timeout: 45000,
    env: { ...process.env, LULU_DATA_DIR: data, LULU_USER_DATA: path.join(data, 'electron-profile') } });
  page = await app.firstWindow();
  page.on('pageerror', error => errors.push(error.message));
  page.on('dialog', dialog => dialog.accept());
  await page.waitForURL(url => url.hostname === '127.0.0.1');
  base = new URL(page.url()).origin;
  await page.getByRole('button', { name: '直播录制', exact: true }).waitFor();
  assert.equal(await page.evaluate(() => typeof window.desktop?.copyText), 'function');
  checks.push('Actual Electron startup, native preload and isolated local backend');
  await api('/settings', 'PATCH', { llm_url: `http://127.0.0.1:${modelServer.address().port}/v1`, llm_model: 'offline-fixture', llm_api_key: 'offline-only-not-a-real-key' });

  await page.locator('.task-row').filter({ hasText: '工作区演示文稿' }).getByRole('button', { name: '查看', exact: true }).click();
  await page.getByLabel('同步字幕').waitFor();
  assert.equal(await page.locator('.synced-line .speaker-label').count(), 2);
  await page.getByRole('button', { name: '继续处理', exact: true }).click();
  await page.getByLabel('待处理文案').fill('这是第一句草稿。\n这是第二句。');
  await page.getByText('草稿已保存', { exact: true }).waitFor();
  assert.equal((await api(`/items/${ids.item}`)).transcript, '这是第一句。\n这是第二句。');
  await page.getByRole('button', { name: '文稿库', exact: true }).click();
  await page.getByRole('button', { name: '文案处理', exact: true }).click();
  await page.getByLabel('待处理文案').waitFor();
  assert.equal(await page.getByLabel('待处理文案').inputValue(), '这是第一句草稿。\n这是第二句。');
  checks.push('Speaker badges and AI draft survives navigation without overwriting source');

  await page.getByLabel('文稿处理要求').fill('校正文字，保留两行。');
  await page.getByRole('button', { name: '发送', exact: true }).click();
  await page.locator('.ai-message.assistant').waitFor();
  assert.match(await page.locator('.ai-message.assistant').innerText(), /这是第一句修正/);
  assert.equal(calls, 1);
  assert.equal((await api(`/items/${ids.item}`)).transcript, '这是第一句。\n这是第二句。');
  await page.getByRole('button', { name: '对照原文', exact: true }).click();
  await page.getByLabel('原文结果对照').waitFor();
  const sendBounds = await page.getByRole('button', { name: '发送', exact: true }).boundingBox();
  assert(sendBounds.y + sendBounds.height <= await page.evaluate(() => window.innerHeight), 'Desktop composer must remain within the window');
  await page.screenshot({ path: path.join(data, 'ai-workspace.png') });
  await page.getByRole('button', { name: '收起对照', exact: true }).click();
  await page.getByRole('button', { name: '应用到文稿', exact: true }).click();
  await waitFor(async () => (await api(`/items/${ids.item}`)).transcript.includes('修正'), 'AI explicit apply');
  const applied = await api(`/items/${ids.item}`);
  assert.deepEqual(applied.segments.map(s => [s.start, s.end, s.speaker_id]), [[0, 3, 1], [3, 6, 2]]);
  assert((await api(`/items/${ids.item}/versions`)).length > 0);
  await page.getByRole('button', { name: '另存结果', exact: true }).click();
  await page.getByRole('heading', { name: '输出库', exact: true }).waitFor();
  await page.locator('.document-title').filter({ hasText: 'AI 处理' }).waitFor();
  checks.push('Real local HTTP model response, simplified text, comparison, explicit apply/history, separate output');

  await page.getByRole('button', { name: '直播录制', exact: true }).click();
  await page.getByLabel('抖音直播链接').fill('https://live.douyin.com/123456');
  await page.getByRole('button', { name: '添加', exact: true }).click();
  await page.locator('.live-host').waitFor();
  const liveState = await api('/live/state');
  assert.equal(liveState.hosts.length, 1); assert.equal(liveState.hosts[0].enabled, false);
  await page.getByRole('button', { name: /录像与互动/ }).click();
  await page.locator('.live-history-row').filter({ hasText: '离线演示直播' }).click();
  await page.getByText('这是一条合成弹幕', { exact: true }).waitFor();
  await waitFor(() => page.locator('.live-detail video').evaluate(video => video.readyState >= 1), 'recording media metadata');
  assert.equal(await page.locator('.live-detail video').evaluate(video => Math.round(video.duration)), 6);
  const csv = await fetch(base + `/api/live/recordings/${ids.recording}/export/metrics`);
  assert.match(await csv.text(), /123.*456/);
  await page.screenshot({ path: path.join(data, 'live-workspace.png') });
  await page.getByRole('button', { name: '加入素材库', exact: true }).click();
  await page.getByRole('heading', { name: '文案提取', exact: true }).waitFor();
  await page.locator('.document-title').filter({ hasText: '离线演示直播' }).waitFor();
  assert((await api('/state')).items.some(item => item.metadata.live_recording_id === ids.recording));
  checks.push('Inactive host creation, synthetic recording playback, metrics/comments, import-to-transcription navigation');

  await page.getByRole('button', { name: 'AI 大模型', exact: true }).click();
  await page.getByRole('heading', { name: /SenseVoice Small/ }).waitFor();
  assert.equal(await page.locator('.transcription-models > .asr-model-grid .asr-model').count(), 4);
  assert.equal((await api('/asr/status')).runtime.ready, false);
  assert.equal((await api('/state')).settings.model, 'base');
  await page.screenshot({ path: path.join(data, 'models-workspace.png') });
  await api('/settings', 'PATCH', { theme: 'dark' });
  await page.waitForFunction(() => document.documentElement.dataset.theme === 'dark');
  assert.equal(await page.locator('.asr-model').first().evaluate(el => getComputedStyle(el).backgroundColor), 'rgb(44, 42, 39)');
  await page.screenshot({ path: path.join(data, 'models-dark.png') });
  checks.push('Four featured Windows model choices, missing runtime/weights are explicit, default unchanged');
  assert.deepEqual(errors, []);
  const evidence = { checks, model_calls: calls, errors, scope: 'Windows Electron + synthetic media/local model fixture; no real live platform or native model recognition', screenshots: ['ai-workspace.png', 'live-workspace.png', 'models-workspace.png'] };
  await fs.writeFile(path.join(data, 'result.json'), JSON.stringify(evidence, null, 2));
  console.log(JSON.stringify({ passed: checks.length, checks, evidence_dir: data }, null, 2));
} catch (error) {
  if (page) await page.screenshot({ path: path.join(data, 'failure.png') }).catch(() => {});
  throw error;
} finally {
  if (app) await app.close();
  await new Promise(resolve => modelServer.close(resolve));
}
