// Real Windows Electron, isolated data. Optional LULU_WECHAT_LIVE_URL tests a public article.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { _electron as electron } from 'playwright';

const root = process.cwd();
await fs.mkdir(path.join(root, '.test-data'), { recursive: true });
const data = await fs.mkdtemp(path.join(root, '.test-data', 'article-reader-'));
const env = { ...process.env, LULU_DATA_DIR: data, LULU_USER_DATA: path.join(data, 'profile') };
const seed = spawnSync(path.join(root, '.venv/Scripts/python.exe'), ['-X', 'utf8', '-m', 'tests.wechat_fixture'], { cwd: root, env, encoding: 'utf8', windowsHide: true });
assert.equal(seed.status, 0, seed.stderr);
const { id, live } = JSON.parse(seed.stdout);
const checks = [], errors = [];
let app, page, base;
async function waitFor(fn, label, timeout = 30000) {
  const end = Date.now() + timeout;
  while (Date.now() < end) { if (await fn()) return; await new Promise(resolve => setTimeout(resolve, 150)); }
  throw Error('Timed out: ' + label);
}
async function api(url, method = 'GET', body) {
  const r = await fetch(base + '/api' + url, { method, headers: { 'X-Lulu-Client': 'desktop', 'Content-Type': 'application/json' }, body: body === undefined ? undefined : JSON.stringify(body) });
  assert(r.ok, `${url}: ${r.status}`); return r.json();
}
try {
  app = await electron.launch({ args: ['electron/main.cjs'], cwd: root, env, timeout: 45000 });
  page = await app.firstWindow();
  page.on('pageerror', e => errors.push(e.message));
  await page.waitForURL(url => url.hostname === '127.0.0.1');
  base = new URL(page.url()).origin;
  if (live) {
    await page.locator('.task-row').filter({ hasText: '旧版失败的公众号任务' }).getByRole('button', { name: '重试', exact: true }).click();
    await waitFor(async () => {
      const item = await api(`/items/${id}`);
      assert.notEqual(item.status, 'error', item.error);
      return item.status === 'done';
    }, 'live article extraction', 240000);
  }
  let item = await api(`/items/${id}`);
  const view = item.metadata.article_view;
  assert(view?.version && view.saved_images > 0, JSON.stringify(view));
  await page.locator('.task-row').filter({ hasText: item.title }).first().getByRole('button', { name: '查看', exact: true }).click();
  const reader = page.getByLabel('原文阅读器');
  await reader.getByRole('button', { name: '全屏浏览', exact: true }).waitFor();
  const frameElement = page.locator('iframe[title="文章原文图文"]');
  const frame = page.frameLocator('iframe[title="文章原文图文"]');
  await frame.locator('#js_content').waitFor({ state: 'visible' });
  await waitFor(async () => frame.locator('img').evaluateAll(images => images.length > 0 && images.every(img => img.complete && img.naturalWidth > 0)), 'cached images render');
  const imageCount = await frame.locator('img').count();
  assert(imageCount >= view.saved_images);
  const dimensions = await frameElement.boundingBox();
  assert(dimensions.height > 200, JSON.stringify(dimensions));
  const typography = await frame.locator('#js_content p').first().evaluate(p => {
    const style = getComputedStyle(p);
    return { font: style.fontFamily, size: style.fontSize, line: style.lineHeight, color: style.color };
  });
  if (!live) assert.deepEqual(typography, { font: 'Georgia, serif', size: '21px', line: '35px', color: 'rgb(31, 65, 93)' });
  assert.equal(await frame.locator('script,iframe').count(), 0);
  assert.equal(await frame.locator('body').evaluate(() => typeof window.desktop), 'undefined');
  checks.push('Original iframe displays cached images and source typography, with no scripts or desktop bridge');

  await reader.getByRole('button', { name: '全屏浏览', exact: true }).click();
  await waitFor(() => app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].isFullScreen()), 'native fullscreen');
  await waitFor(() => app.evaluate(({ BrowserWindow, screen }) => {
    const w = BrowserWindow.getAllWindows()[0], b = w.getBounds(), d = screen.getDisplayMatching(b).bounds;
    return b.width >= d.width && b.height >= d.height;
  }), 'window reaches monitor bounds');
  await reader.getByRole('button', { name: '退出全屏', exact: true }).waitFor();
  const bounds = await reader.boundingBox();
  const viewport = await page.evaluate(() => ({ width: innerWidth, height: innerHeight }));
  assert(bounds.x === 0 && bounds.y === 0 && Math.abs(bounds.width - viewport.width) < 1 && Math.abs(bounds.height - viewport.height) < 1, JSON.stringify({ bounds, viewport }));
  await page.screenshot({ path: path.join(data, 'fullscreen-top.png') });
  await frame.locator('#js_content').click({ position: { x: 10, y: 10 } });
  await page.keyboard.press('PageDown');
  await waitFor(() => frame.locator('body').evaluate(() => scrollY > 0), 'scroll original');
  const scrollPosition = await frame.locator('body').evaluate(() => scrollY);
  await page.screenshot({ path: path.join(data, 'fullscreen.png') });
  await page.keyboard.press('Escape');
  await waitFor(async () => !(await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].isFullScreen())), 'Escape from focused article');
  await reader.getByRole('button', { name: '全屏浏览', exact: true }).waitFor();
  assert((await frame.locator('body').evaluate(() => scrollY)) > 0, 'Fullscreen must not reload the article');
  checks.push('Native fullscreen fills the display, article scrolls, and Escape works while the iframe has focus');
  await reader.getByRole('button', { name: '全屏浏览', exact: true }).click();
  await reader.getByRole('button', { name: '退出全屏', exact: true }).click();
  await waitFor(async () => !(await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].isFullScreen())), 'exit button');
  checks.push('Repeated fullscreen toggles and the visible exit button return to the workbench');

  await page.locator('.inspector').getByRole('button', { name: '文稿', exact: true }).click();
  assert.equal(await page.getByLabel('文稿正文').inputValue(), item.transcript);
  const edited = item.transcript + '\n用户的编辑应保留。';
  await page.getByLabel('文稿正文').fill(edited);
  await page.locator('.inspector').getByRole('button', { name: '保存', exact: true }).click();
  await waitFor(async () => (await api(`/items/${id}`)).transcript === edited, 'save text');
  await page.locator('.inspector').getByRole('button', { name: '原文', exact: true }).click();
  await frame.locator('#js_content').waitFor();
  assert(!(await frame.locator('#js_content').innerText()).includes('用户的编辑应保留'));
  checks.push('Editable simplified transcript and the preserved source original stay separate');
  if (live) {
    await reader.getByRole('button', { name: '刷新图文', exact: true }).click();
    await waitFor(async () => {
      const current = await api(`/items/${id}`);
      assert.notEqual(current.status, 'error', current.error);
      return current.status === 'done' && current.metadata.article_view.version !== view.version;
    }, 'live refresh', 240000);
    assert.equal((await api(`/items/${id}`)).transcript, edited);
    checks.push('Refreshing the real public original saves a new snapshot without overwriting user edits');
  }
  assert.deepEqual(errors, []);
  await page.screenshot({ path: path.join(data, 'returned.png') });
  const result = { passed: checks.length, checks, live, view, imageCount, typography, scrollPosition, errors };
  await fs.writeFile(path.join(data, 'result.json'), JSON.stringify(result, null, 2));
  console.log(JSON.stringify({ ...result, evidence_dir: data }, null, 2));
} catch (e) {
  if (page) await page.screenshot({ path: path.join(data, 'failure.png') }).catch(() => {});
  console.error('Evidence: ' + data); throw e;
} finally { if (app) await app.close(); }
