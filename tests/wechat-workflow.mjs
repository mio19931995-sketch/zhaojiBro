// Native Windows Electron; an isolated library, no ASR/LLM calls or real settings.
// Optional LULU_WECHAT_LIVE_URL enables public-network retry and refresh checks.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { _electron as electron } from 'playwright';

const root = process.cwd();
await fs.mkdir(path.join(root, '.test-data'), { recursive: true });
const data = await fs.mkdtemp(path.join(root, '.test-data', 'wechat-desktop-'));
const env = { ...process.env, LULU_DATA_DIR: data, LULU_USER_DATA: path.join(data, 'electron-profile') };
const seed = spawnSync(path.join(root, '.venv/Scripts/python.exe'), ['-X', 'utf8', '-m', 'tests.wechat_fixture'], {
  cwd: root, env, encoding: 'utf8', windowsHide: true,
});
assert.equal(seed.status, 0, seed.stderr);
const { id, live } = JSON.parse(seed.stdout);
const checks = [], errors = [];
let app, page, base, previousClipboard, copied;
async function api(url, method = 'GET', body) {
  const response = await fetch(base + '/api' + url, { method,
    headers: { 'X-Lulu-Client': 'desktop', 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body) });
  assert(response.ok, `${url}: ${response.status}`);
  return response.json();
}
async function waitFor(fn, label, timeout = 120000) {
  const end = Date.now() + timeout;
  while (Date.now() < end) { if (await fn()) return; await new Promise(resolve => setTimeout(resolve, 200)); }
  throw Error('Timed out: ' + label);
}
async function completed() {
  const item = await api(`/items/${id}`);
  assert.notEqual(item.status, 'error', item.error);
  return item.status === 'done';
}
try {
  app = await electron.launch({ args: ['electron/main.cjs'], cwd: root, env, timeout: 45000 });
  page = await app.firstWindow();
  page.on('pageerror', error => errors.push(error.message));
  page.on('dialog', dialog => dialog.accept());
  await page.waitForURL(url => url.hostname === '127.0.0.1');
  base = new URL(page.url()).origin;
  await page.getByLabel('作品链接').waitFor();
  assert((await page.getByLabel('作品链接').getAttribute('placeholder')).includes('微信公众号'));
  if (live) {
    const row = page.locator('.task-row').filter({ hasText: '旧版失败的公众号任务' });
    const started = page.waitForResponse(r => r.url().endsWith(`/items/${id}/start`) && r.request().method() === 'POST');
    await row.getByRole('button', { name: '重试', exact: true }).click();
    assert((await started).ok());
    await waitFor(completed, 'real article retry');
    checks.push('Existing Unsupported URL task retries through the real public article network path');
  }
  let item = await api(`/items/${id}`);
  assert.equal(item.kind, 'article');
  assert(item.transcript.length > 50 && !item.media_path && !item.segments.length);
  const row = page.locator('.task-row').filter({ hasText: item.title }).first();
  await row.getByRole('button', { name: '查看', exact: true }).click();
  const inspector = page.locator('.inspector');
  await page.getByLabel('公众号提取说明').waitFor();
  assert.equal(await page.getByLabel('媒体可用状态').count(), 0);
  assert.equal(await page.getByLabel('文稿正文').inputValue(), item.transcript);
  await row.getByRole('button', { name: '重新提取', exact: true }).waitFor();
  checks.push('Article stays in the queue; body renders; refresh wording and media guidance are article-specific');
  if (live && item.metadata.cover_path) {
    assert((await fetch(base + `/api/items/${id}/cover`)).ok);
    await row.locator('img').waitFor();
    checks.push('Real article cover is saved and displayed in the queue');
  }
  await inspector.getByRole('button', { name: '信息', exact: true }).click();
  for (const key of ['account', 'author', 'published_at']) {
    if (item.metadata[key]) await inspector.getByText(item.metadata[key], { exact: true }).waitFor();
  }
  await inspector.getByRole('button', { name: '打开公众号原文', exact: true }).waitFor();
  const md = await (await fetch(base + `/api/items/${id}/export/md`)).text();
  assert(md.includes(item.metadata.author) && md.includes(item.source_url));
  assert((await inspector.getByRole('link', { name: 'SRT', exact: true }).getAttribute('class')).includes('disabled'));
  checks.push('Source, account, author and date display; Markdown retains attribution; no fabricated SRT');
  previousClipboard = await app.evaluate(({ clipboard }) => clipboard.readText());
  await inspector.getByRole('button', { name: '复制到 Codex', exact: true }).click();
  await waitFor(async () => {
    copied = await app.evaluate(({ clipboard }) => clipboard.readText());
    return copied.includes(id);
  }, 'native clipboard copy', 15000);
  assert(copied.includes('lulu_read_transcript') && !copied.includes('lulu_video_frame'));
  checks.push('Native clipboard contains the exact article ID and text-reading instructions for Codex');
  if (live) {
    await inspector.getByRole('button', { name: '文稿', exact: true }).click();
    const edited = item.transcript + '\n用户校对保留标记。';
    await page.getByLabel('文稿正文').fill(edited);
    await inspector.getByRole('button', { name: '保存', exact: true }).click();
    await waitFor(async () => (await api(`/items/${id}`)).transcript === edited, 'edit saved', 15000);
    const refresh = page.waitForResponse(r => r.url().endsWith(`/items/${id}/retranscribe`) && r.request().method() === 'POST');
    await row.getByRole('button', { name: '重新提取', exact: true }).click();
    assert((await refresh).ok());
    await waitFor(async () => (await completed()) && !(await api(`/items/${id}`)).transcript.includes('用户校对保留标记'), 'real article refresh');
    const versions = await api(`/items/${id}/versions`);
    let saved;
    for (const version of versions) {
      const content = await api(`/items/${id}/versions/${version.id}`);
      if (content.transcript === edited) saved = version.id;
    }
    assert(saved, 'Refresh lost the edited article');
    await api(`/items/${id}/versions/${saved}/restore`, 'POST');
    assert.equal((await api(`/items/${id}`)).transcript, edited);
    checks.push('Real refresh preserves edited text in restorable history');
    await page.getByLabel('作品链接').fill(env.LULU_WECHAT_LIVE_URL);
    const queued = page.waitForResponse(r => r.url().endsWith('/api/links') && r.request().method() === 'POST');
    await page.getByRole('button', { name: '抓取', exact: true }).click();
    assert.equal((await (await queued).json())[0].kind, 'article');
    checks.push('Pasting a WeChat article into the existing grab input creates an article task');
  }
  await page.getByRole('button', { name: '文稿库', exact: true }).click();
  const libraryEntry = page.getByRole('button', { name: item.title, exact: false }).first();
  await libraryEntry.waitFor();
  assert((await libraryEntry.innerText()).includes('公众号文章'));
  assert(!(await libraryEntry.innerText()).includes('0:00'));
  checks.push('Extracted article is available in the document library');
  assert.deepEqual(errors, []);
  await page.screenshot({ path: path.join(data, 'article-library.png') });
  const result = { passed: checks.length, checks, live, errors };
  await fs.writeFile(path.join(data, 'result.json'), JSON.stringify(result, null, 2));
  console.log(JSON.stringify({ ...result, evidence_dir: data }, null, 2));
} catch (error) {
  if (page) await page.screenshot({ path: path.join(data, 'failure.png') }).catch(() => {});
  throw error;
} finally {
  if (app) {
    if (previousClipboard !== undefined && copied) await app.evaluate(({ clipboard }, { copied, previous }) => {
      if (clipboard.readText() === copied) clipboard.writeText(previous);
    }, { copied, previous: previousClipboard }).catch(() => {});
    await app.close();
  }
}
