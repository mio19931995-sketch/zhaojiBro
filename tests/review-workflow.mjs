// Offline browser + real local API/storage, deterministic model fixtures.
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import fs from 'node:fs/promises';
import path from 'node:path';
import net from 'node:net';
import { chromium } from 'playwright';
const root = process.cwd();
await fs.mkdir(path.join(root, '.test-data'), { recursive: true });
const data = await fs.mkdtemp(path.join(root, '.test-data', 'review-ui-'));
const port = await new Promise(resolve => { const s = net.createServer(); s.listen(0, '127.0.0.1', () => { const p = s.address().port; s.close(() => resolve(p)); }); });
const base = `http://127.0.0.1:${port}`;
const child = spawn(path.join(root, '.venv/Scripts/python.exe'), ['-m', 'tests.review_fixture'], {
  cwd: root, windowsHide: true, env: { ...process.env, LULU_DATA_DIR: data, LULU_PORT: String(port) }, stdio: ['ignore', 'pipe', 'pipe']
});
let browser, logs = '';
child.stderr.on('data', c => logs += c);
try {
  for (let n = 0; ; n++) {
    try { if ((await fetch(base + '/api/health')).ok) break; } catch {}
    if (n > 100) throw Error(logs);
    await new Promise(r => setTimeout(r, 200));
  }
  browser = await chromium.launch({ channel: 'msedge', headless: true });
  const page = await browser.newPage({ viewport: { width: 1370, height: 910 } });
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  await page.goto(base);
  await page.locator('.task-open').filter({ hasText: 'Review fixture' }).click();
  await page.getByRole('button', { name: '检查', exact: true }).click();
  await page.getByRole('button', { name: '一键检查', exact: true }).click();
  await page.getByText('1 项需要核对', { exact: true }).waitFor();
  await page.getByRole('button', { name: '定位正文', exact: true }).click();
  const selected = await page.getByRole('textbox', { name: '文稿正文', exact: true }).evaluate(el => el.value.slice(el.selectionStart, el.selectionEnd));
  assert.equal(selected, '这里有一句病句。');
  await page.getByRole('button', { name: '检查', exact: true }).click();
  await page.getByRole('button', { name: '生成修正建议', exact: true }).click();
  await page.getByText('这里是一句通顺的表达。', { exact: true }).waitFor();
  await page.getByRole('button', { name: '采纳这一处', exact: true }).click();
  await page.getByText('文稿已修改，检查结果已过期', { exact: true }).waitFor();
  assert(await page.getByRole('button', { name: '采纳这一处', exact: true }).isDisabled());
  await page.getByRole('button', { name: '重新检查', exact: true }).click();
  await page.getByText('1 项需要核对', { exact: true }).waitFor();
  const state = await (await fetch(base + '/api/state')).json();
  const item = await (await fetch(base + '/api/items/' + state.items[0].id)).json();
  assert(item.transcript.includes('这里是一句通顺的表达。'));
  const versions = await (await fetch(base + '/api/items/' + item.id + '/versions')).json();
  assert.equal(versions.length, 1);
  await page.screenshot({ path: path.join(data, 'review.png'), fullPage: true });
  await page.getByRole('button', { name: '关闭预览', exact: true }).click();
  await page.getByRole('button', { name: 'AI 大模型', exact: true }).click();
  await page.getByRole('button', { name: 'Jev 内容检查', exact: true }).click();
  await page.getByRole('heading', { name: 'Jev 创作检查助手' }).waitFor();
  assert.equal(await page.locator('input[type=password]').inputValue(), '');
  assert.deepEqual(errors, []);
  console.log(JSON.stringify({ ok: true, checks: ['check report', 'Unicode-safe highlight', 'suggestion preview', 'versioned accept', 'stale result disabled', 'recheck', 'independent configuration'], artifact: data }));
} finally { await browser?.close(); child.kill(); }
