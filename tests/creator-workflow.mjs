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
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  page.on('dialog', dialog => dialog.accept());
  await page.goto(base);
  await page.locator('.task-open').filter({ hasText: 'creator-fixture' }).click();
  await page.waitForSelector('.synced-line');
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
