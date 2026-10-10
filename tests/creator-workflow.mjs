// Offline end-to-end checks. Uses generated media and a new isolated data directory.
// Run after npm run build. Requires FFmpeg, project Python, Playwright and Edge.
import assert from 'node:assert/strict';
import { spawn, spawnSync } from 'node:child_process';
import fs from 'node:fs/promises';
import path from 'node:path';
import net from 'node:net';
import { chromium } from 'playwright';

const root = process.cwd();
await fs.mkdir(path.join(root, '.test-data'), { recursive: true });
const dataDir = await fs.mkdtemp(path.join(root, '.test-data', 'creator-workflow-'));
const mediaPath = path.join(dataDir, 'creator-fixture.mp4');
const generated = spawnSync('ffmpeg', ['-hide_banner', '-loglevel', 'error', '-y', '-f', 'lavfi', '-i', 'color=c=teal:s=480x270:r=24:d=24', '-f', 'lavfi', '-i', 'sine=frequency=440:duration=24', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-shortest', mediaPath], { windowsHide: true, encoding: 'utf8' });
assert.equal(generated.status, 0, generated.stderr || generated.error?.message);
const port = await new Promise(resolve => { const s = net.createServer(); s.listen(0, '127.0.0.1', () => { const p = s.address().port; s.close(() => resolve(p)); }); });
const base = `http://127.0.0.1:${port}`;
const python = path.join(root, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
const seeded = spawnSync(python, ['-c', `
import json, os, uuid
from backend import store
downloaded = store.add('Download-only fixture', status='done', media_path=os.environ['FIXTURE_VIDEO'], metadata={'download_only': True, 'action': 'assets'})
text_only = store.add('Text-only source', status='done', source_url='https://example.test/text-only', transcript='Only the transcript remains.')
group = {'id': uuid.uuid4().hex, 'url': 'https://example.test/collection', 'title': 'Review fixtures', 'ids': [], 'status': 'waiting', 'limit': 100, 'error': ''}
store.object_put('collections', group['id'], group)
print(json.dumps({'downloaded': downloaded['id'], 'text_only': text_only['id'], 'group': group['id']}))
`], { cwd: root, windowsHide: true, encoding: 'utf8', env: { ...process.env, LULU_DATA_DIR: dataDir, FIXTURE_VIDEO: mediaPath, PYTHONIOENCODING: 'utf-8' } });
assert.equal(seeded.status, 0, seeded.stderr);
const fixtureIds = JSON.parse(seeded.stdout);
const child = spawn(python, ['-m', 'backend.main'], { cwd: root, windowsHide: true, env: { ...process.env, LULU_DATA_DIR: dataDir, LULU_PORT: String(port), LULU_SHUTDOWN_TOKEN: 'creator-test' }, stdio: ['ignore', 'pipe', 'pipe'] });
let logs = '', browser;
child.stdout.on('data', value => logs += value);
child.stderr.on('data', value => logs += value);
const checks = [];
const headers = { 'X-Lulu-Client': 'desktop' };
async function api(url, method = 'GET', body) {
  const isForm = body instanceof FormData;
  const response = await fetch(base + '/api' + url, { method, headers: { ...headers, ...(!isForm ? { 'Content-Type': 'application/json' } : {}) }, body: body ? isForm ? body : JSON.stringify(body) : undefined });
  assert(response.ok, `${url}: ${response.status} ${response.ok ? '' : await response.text()}`);
  return response.json();
}
try {
  for (let i = 0; i < 120; i++) {
    try { await api('/health'); break; } catch { if (i === 119) throw Error(logs); await new Promise(r => setTimeout(r, 250)); }
  }
  const form = new FormData();
  form.append('files', new Blob([await fs.readFile(mediaPath)], { type: 'video/mp4' }), 'creator-fixture.mp4');
  const [item] = await api('/import', 'POST', form);
  const segments = Array.from({ length: 12 }, (_, i) => ({ start: i * 2, end: i * 2 + 1.8, text: `Sentence ${i + 1}. This is a sufficiently long caption for checking playback, editing and scrolling.` }));
  await api(`/items/${item.id}`, 'PATCH', { segments });
  browser = await chromium.launch({ channel: process.platform === 'win32' ? 'msedge' : undefined, headless: true, args: ['--autoplay-policy=no-user-gesture-required'] });
  const page = await browser.newPage({ viewport: { width: 1370, height: 910 } });
  await page.addInitScript(() => {
    Object.defineProperty(navigator, 'clipboard', { value: { writeText: async value => { window.__copiedText = value; } } });
  });
  const errors = [];
  let dialogs = 0;
  page.on('pageerror', error => errors.push(error.message));
  page.on('dialog', dialog => { dialogs++; dialog.accept(); });
  await page.goto(base);
  const downloadRow = page.locator('.task-row').filter({ hasText: 'Download-only fixture' });
  const transcriptionUrl = `**/api/items/${fixtureIds.downloaded}/retranscribe`;
  await page.route(transcriptionUrl, route => route.fulfill({ json: { ok: true } }));
  const beginTranscription = page.waitForRequest(r => r.url().endsWith(`/api/items/${fixtureIds.downloaded}/retranscribe`));
  await downloadRow.getByRole('button', { name: '开始转录', exact: true }).click();
  assert.equal((await beginTranscription).method(), 'POST');
  assert.equal(dialogs, 0, 'No existing transcript to overwrite');
  await page.unroute(transcriptionUrl);
  checks.push('download-only task offers Start transcription and calls the conversion endpoint');

  await page.locator('.task-open').filter({ hasText: 'Text-only source' }).click();
  const missing = page.getByRole('note', { name: '媒体可用状态' });
  await missing.waitFor();
  assert((await missing.textContent()).includes('仅文稿可用，无法播放/抽帧'));
  assert((await missing.textContent()).includes('重新粘贴原作品链接'));
  await page.getByRole('button', { name: '复制到 Codex', exact: true }).click();
  await page.waitForFunction(id => window.__copiedText?.includes(id), fixtureIds.text_only);
  await missing.getByRole('button', { name: '查看下载选项', exact: true }).click();
  assert(await page.getByLabel('保留音视频', { exact: true }).isVisible());
  await missing.getByRole('button', { name: 'Codex 缓存设置', exact: true }).click();
  await page.getByRole('heading', { name: 'Lulu 素材直连' }).waitFor();
  assert(!(await page.getByLabel('为 Codex 保留视频缓存').isChecked()));
  const settings = (await api('/state')).settings;
  assert.equal(settings.keep_media, 'false');
  assert.equal(settings.download_mode, 'audio');
  assert.equal(settings.codex_keep_video, 'false');
  await page.getByRole('button', { name: '文案提取', exact: true }).click();
  checks.push('text-only source explains playback/frame limits; copy works; existing settings links preserve defaults');
  await page.locator('.task-open').filter({ hasText: 'creator-fixture' }).click();
  await page.waitForSelector('.synced-line');
  assert.equal(await missing.count(), 0, 'Available media must not show the missing-media note');
  if (await page.getByRole('button', { name: '关闭提示', exact: true }).count()) await page.getByRole('button', { name: '关闭提示', exact: true }).click();
  const originalTitle = await page.locator('.document-title h2').textContent();
  await page.locator('.document-title h2').evaluate(el => { el.textContent = '一键复刻视频动作运镜与人物场景，上传参考视频提取关键词，再选择人物和产品图片生成。'.repeat(5); });
  for (const size of [{ width: 1370, height: 910 }, { width: 1100, height: 700 }, { width: 900, height: 620 }]) {
    await page.setViewportSize(size);
    const visible = await page.getByRole('button', { name: '复制到 Codex', exact: true }).evaluate(el => {
      const r = el.getBoundingClientRect();
      return r.top >= 0 && r.bottom <= innerHeight && r.right <= innerWidth && el.contains(document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2));
    });
    assert(visible, `Codex handoff clipped with long title at ${size.width}x${size.height}`);
  }
  await page.setViewportSize({ width: 1370, height: 910 });
  await page.locator('.document-title h2').evaluate((el, title) => { el.textContent = title; }, originalTitle);
  checks.push('Codex handoff stays visible with long video titles at three window sizes');
  const video = page.locator('.inspector video');
  await video.evaluate(async el => { el.muted = true; el.currentTime = 6.2; await el.play(); });
  await page.waitForFunction(() => document.querySelector('.synced-line.current')?.textContent.includes('Sentence 4.'));
  await page.waitForTimeout(250);
  const top = await page.evaluate(() => document.querySelector('.synced-line.current').getBoundingClientRect().top - document.querySelector('.document-body').getBoundingClientRect().top);
  assert(Math.abs(top) < 3, `Current caption not at top: ${top}`);
  const body = page.locator('.document-body');
  const box = await body.boundingBox();
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await page.mouse.wheel(0, 250);
  await page.getByRole('button', { name: '回到当前句', exact: true }).waitFor();
  await page.waitForTimeout(250);
  const scroll = await body.evaluate(el => el.scrollTop);
  await page.waitForTimeout(500);
  assert(Math.abs(await body.evaluate(el => el.scrollTop) - scroll) < 3, 'Manual scrolling was overridden');
  await page.getByRole('button', { name: '回到当前句', exact: true }).click();
  await page.waitForTimeout(100);
  assert(Math.abs(await page.evaluate(() => document.querySelector('.synced-line.current').getBoundingClientRect().top - document.querySelector('.document-body').getBoundingClientRect().top)) < 3);
  checks.push('playing caption at top; manual scroll pauses; resume follows');

  await video.evaluate(el => { el.pause(); el.currentTime = 2.1; });
  await page.waitForTimeout(150);
  await page.getByRole('button', { name: '单句循环', exact: true }).click();
  await page.waitForTimeout(2250);
  const loopTime = await video.evaluate(el => el.currentTime);
  assert(loopTime >= 2 && loopTime < 3.8, `Loop did not repeat: ${loopTime}`);
  await video.evaluate(el => { el.currentTime = 10.2; });
  await page.getByRole('button', { name: '单句循环', exact: true }).waitFor();
  assert(await video.evaluate(el => el.currentTime) >= 10, 'Scrub outside loop was pulled back');
  await video.evaluate(el => el.pause());
  await page.getByLabel('播放速度').selectOption('1.5');
  assert.equal(await video.evaluate(el => el.playbackRate), 1.5);
  checks.push('single sentence repeats; scrub exits loop; playback speed');

  await page.getByRole('button', { name: '全屏播放', exact: true }).click();
  await page.waitForFunction(() => !!document.fullscreenElement);
  await page.getByRole('button', { name: '退出全屏', exact: true }).click();
  await page.waitForFunction(() => !document.fullscreenElement);
  checks.push('video fullscreen enter/exit remains functional');

  await page.getByRole('button', { name: '编辑全文', exact: true }).click();
  const text = page.getByRole('textbox', { name: '文稿正文', exact: true });
  const corrected = segments.map((s, i) => i === 0 ? 'Corrected sentence one.' : s.text).join('\n');
  await text.fill(corrected);
  await page.getByRole('button', { name: '保存', exact: true }).click();
  await page.waitForSelector('.synced-line');
  const edited = await api(`/items/${item.id}`);
  assert.deepEqual(edited.segments.map(s => [s.start, s.end]), segments.map(s => [s.start, s.end]));
  assert.equal(edited.segments[0].text, 'Corrected sentence one.');
  const srt = await (await fetch(base + `/api/items/${item.id}/export/srt`)).text();
  assert(srt.includes('00:00:00,000 --> 00:00:01,800') && srt.includes('Corrected sentence one.'));
  checks.push('full-text correction preserves all timings and SRT');

  await page.getByRole('button', { name: '历史版本', exact: true }).click();
  await page.getByRole('region', { name: '版本内容预览' }).getByText(segments[0].text, { exact: false }).waitFor();
  await page.getByRole('button', { name: '恢复此版本', exact: true }).click();
  await page.waitForFunction(text => document.querySelector('.synced-line')?.textContent.includes(text), segments[0].text);
  assert.deepEqual((await api(`/items/${item.id}`)).segments, segments);
  assert.equal((await api(`/items/${item.id}/versions`)).length, 2);
  checks.push('preview and restore original version; preserve replaced version');

  await page.getByRole('button', { name: '编辑全文', exact: true }).click();
  await text.fill('A rewritten spoken script.\nA new ending.');
  assert(await page.getByRole('button', { name: '保存', exact: true }).isDisabled());
  await page.getByRole('button', { name: '另存口播稿', exact: true }).click();
  await page.waitForFunction(() => document.querySelector('.document-title h2')?.textContent.endsWith('· 口播稿'));
  const script = (await api('/state')).items.find(i => i.title.endsWith('· 口播稿'));
  assert.equal((await api(`/items/${script.id}`)).transcript, 'A rewritten spoken script.\nA new ending.');
  assert.deepEqual((await api(`/items/${item.id}`)).segments, segments);
  checks.push('structural edits blocked from overwriting timing; separate script keeps original');

  await page.getByRole('button', { name: '信息', exact: true }).click();
  await page.getByRole('button', { name: '移入回收站', exact: true }).click();
  await page.getByRole('button', { name: '回收站', exact: true }).click();
  await page.getByText(script.title, { exact: true }).waitFor();
  await page.getByLabel('全选回收站内容').check();
  await page.getByRole('button', { name: '恢复所选', exact: true }).click();
  await page.getByText('回收站是空的', { exact: true }).waitFor();
  assert.equal((await api(`/items/${script.id}`)).transcript, 'A rewritten spoken script.\nA new ending.');
  await page.getByRole('button', { name: '文稿库', exact: true }).click();
  await page.getByRole('button', { name: script.title, exact: false }).first().waitFor();
  checks.push('delete to trash; batch restore; restored document is usable');
  await page.getByRole('button', { name: script.title, exact: false }).first().click();
  const handoff = page.waitForResponse(r => r.url().endsWith('/api/codex/select') && r.request().method() === 'POST');
  await page.getByRole('button', { name: '复制到 Codex', exact: true }).click();
  const selected = await handoff;
  assert(selected.ok());
  const copied = (await selected.json()).prompt;
  assert(copied.includes(script.id) && copied.includes(script.title));
  await page.waitForFunction(expected => window.__copiedText === expected, copied);
  await api('/codex/select', 'POST', { item_id: item.id });
  assert.equal(await page.evaluate(() => window.__copiedText), copied, 'Later selection must not change copied reference');
  await page.evaluate(() => { window.__copiedText = ''; window.desktop = { copyText: async text => { window.__copiedText = text; } }; });
  const shortcut = page.waitForResponse(r => r.url().endsWith('/api/codex/select') && r.request().method() === 'POST');
  await page.keyboard.press('Control+Alt+c');
  assert((await shortcut).ok());
  await page.waitForFunction(expected => window.__copiedText === expected, copied);
  assert.equal(await page.locator('.document-title h2').textContent(), script.title);
  checks.push('One-click copy and Ctrl+Alt+C use exact ID and title without navigation or sending');
  await page.getByRole('button', { name: 'Codex 连接', exact: true }).click();
  await page.getByRole('heading', { name: 'Lulu 素材直连' }).waitFor();
  await page.getByLabel('为 Codex 保留视频缓存').check();
  await page.waitForFunction(() => document.querySelector('.service-note')?.textContent.includes('素材'));
  for (let n = 0; n < 20 && (await api('/state')).settings.codex_keep_video !== 'true'; n++) await page.waitForTimeout(100);
  assert.equal((await api('/state')).settings.codex_keep_video, 'true');
  await page.getByRole('button', { name: '文案提取', exact: true }).click();
  assert(await page.getByRole('button', { name: '仅音频', exact: true }).isDisabled());
  assert(await page.getByLabel('保留音视频', { exact: true }).isChecked());
  checks.push('Codex handoff; video cache setting and visible download override');

  const group = { id: fixtureIds.group };
  await api(`/collections/${group.id}/capture`, 'POST', { status: 'done', entries: [
    { url: 'https://example.test/a', title: 'Collection A', author: 'Author A' },
    { url: 'https://example.test/b', title: 'Collection B', author: 'Author B' },
  ] });
  const entries = (await api('/state')).items;
  const a = entries.find(i => i.title === 'Collection A'), b = entries.find(i => i.title === 'Collection B');
  await page.getByRole('button', { name: '主页采集', exact: true }).click();
  await page.getByLabel('采集任务', { exact: true }).selectOption(group.id);
  await page.getByLabel('选择 Collection A', { exact: true }).check();
  await page.getByLabel('选择 Collection B', { exact: true }).check();
  const search = page.getByLabel('搜索采集作品', { exact: true });
  await search.fill('Collection B');
  const toolbar = page.locator('.collection-table > .toolbar');
  assert.equal(await toolbar.locator('.toolbar-count').textContent(), '已选 1 / 共 1 条');
  const batches = [];
  await page.route('**/api/items/batch', async route => {
    const payload = route.request().postDataJSON();
    batches.push(payload);
    if (payload.action === 'delete') await route.continue();
    else await route.fulfill({ json: { count: payload.ids.length } });
  });
  for (const [name, action] of [['下载所选素材', 'assets'], ['转录所选', 'transcribe']]) {
    const response = page.waitForResponse(r => r.url().endsWith('/api/items/batch'));
    await toolbar.getByRole('button', { name, exact: true }).click();
    assert((await response).ok());
    assert.equal(batches.at(-1).action, action);
    assert.deepEqual(batches.at(-1).ids, [b.id]);
    await page.waitForFunction(() => ![...document.querySelectorAll('.collection-table > .toolbar button')].find(e => e.textContent.includes('下载所选素材')).disabled);
  }
  const paused = page.waitForResponse(r => r.url().endsWith('/api/items/batch'));
  await toolbar.getByRole('button').nth(3).click();
  await paused;
  assert.equal(batches.at(-1).action, 'pause');
  assert.deepEqual(batches.at(-1).ids, [b.id]);
  const exported = page.waitForResponse(r => r.url().endsWith('/api/library/export'));
  const exportedFile = page.waitForEvent('download');
  await toolbar.getByRole('button', { name: '导出表格', exact: true }).click();
  const csv = await exported;
  assert.deepEqual(csv.request().postDataJSON().ids, [b.id]);
  const csvPath = path.join(dataDir, 'selected.csv');
  await (await exportedFile).saveAs(csvPath);
  const csvText = await fs.readFile(csvPath, 'utf8');
  assert(csv.ok() && csvText.includes('Collection B') && !csvText.includes('Collection A'), `${csv.status()}: ${csvText}`);
  await page.evaluate(() => window.addEventListener('lulu-feishu', event => { window.__exportIds = event.detail.ids; }));
  await toolbar.getByRole('button', { name: '存入飞书', exact: true }).click();
  assert.deepEqual(await page.evaluate(() => window.__exportIds), [b.id]);
  await page.getByRole('dialog').getByRole('button', { name: '关闭窗口', exact: true }).click();
  await search.fill('No matching entry');
  assert.equal(await toolbar.locator('.toolbar-count').textContent(), '已选 0 / 共 0 条');
  for (const name of ['下载所选素材', '转录所选', '导出表格', '存入飞书']) assert(await toolbar.getByRole('button', { name, exact: true }).isDisabled());
  assert(await toolbar.getByTitle('移除所选条目', { exact: true }).isDisabled());
  assert(await toolbar.getByRole('button').nth(3).isDisabled());
  await search.fill('');
  assert.equal(await toolbar.locator('.toolbar-count').textContent(), '已选 2 / 共 2 条');
  assert(await page.getByLabel('选择 Collection A', { exact: true }).isChecked());
  assert(await page.getByLabel('选择 Collection B', { exact: true }).isChecked());
  await search.fill('Collection B');
  const removed = page.waitForResponse(r => r.url().endsWith('/api/items/batch'));
  await toolbar.getByTitle('移除所选条目', { exact: true }).click();
  assert((await removed).ok());
  assert.deepEqual(batches.at(-1).ids, [b.id]);
  await page.getByText('没有符合筛选条件的作品', { exact: true }).waitFor();
  await search.fill('');
  assert.equal(await toolbar.locator('.toolbar-count').textContent(), '已选 1 / 共 1 条');
  assert(await page.getByLabel('选择 Collection A', { exact: true }).isChecked());
  assert((await api('/state')).items.some(i => i.id === a.id));
  assert(!(await api('/state')).items.some(i => i.id === b.id));
  await page.unroute('**/api/items/batch');
  checks.push('collection download/transcribe/pause/delete/CSV/Feishu use visible selections; empty filter disables actions; hidden selection survives');
  assert.deepEqual(errors, []);
  await page.screenshot({ path: path.join(dataDir, 'workflow-final.png'), fullPage: true });
  await fs.writeFile(path.join(dataDir, 'result.json'), JSON.stringify({ checks, pageErrors: errors }, null, 2));
  console.log(JSON.stringify({ passed: checks.length, checks, dataDir }, null, 2));
} catch (error) {
  if (browser) await browser.contexts()[0]?.pages()[0]?.screenshot({ path: path.join(dataDir, 'failure.png') }).catch(() => {});
  console.error(`Isolated test artifacts: ${dataDir}`);
  throw error;
} finally {
  await browser?.close();
  await fetch(base + '/api/shutdown', { method: 'POST', headers: { ...headers, 'X-Lulu-Shutdown': 'creator-test' } }).catch(() => {});
  await Promise.race([new Promise(resolve => child.once('exit', resolve)), new Promise(resolve => setTimeout(resolve, 5000))]);
  if (child.exitCode === null) child.kill();
}
