// Actual Windows Electron windows + Python server. Only isolated test data/processes.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import { spawn, spawnSync } from 'node:child_process';
import { _electron as electron } from 'playwright';

const root = process.cwd();
await fs.mkdir(path.join(root, '.test-data'), { recursive: true });
const data = await fs.mkdtemp(path.join(root, '.test-data', 'electron-lifecycle-'));
const env = { ...process.env, LULU_DATA_DIR: data, LULU_USER_DATA: path.join(data, 'profile') };
delete env.ELECTRON_RUN_AS_NODE;
const executable = path.join(root, 'node_modules/electron/dist/electron.exe');
const runtimePath = path.join(data, 'logs/runtime.json');
const checks = [];
let app, restarted;

async function waitFor(fn, label, timeout = 30000) {
  const end = Date.now() + timeout;
  while (Date.now() < end) { if (await fn()) return; await new Promise(resolve => setTimeout(resolve, 100)); }
  throw Error('Timed out: ' + label);
}
function secondInstance() {
  const child = spawn(executable, [root], { cwd: root, env, windowsHide: true, stdio: 'ignore' });
  return new Promise((resolve, reject) => { child.once('error', reject); child.once('exit', code => code === 0 ? resolve() : reject(Error('Second instance exit: ' + code))); });
}
function closeOwnedWindow(pid) {
  assert(Number.isInteger(pid) && pid > 0);
  return spawnSync('powershell.exe', ['-NoProfile', '-Command', `$p=Get-Process -Id ${pid} -ErrorAction SilentlyContinue; if($p){$p.CloseMainWindow() | Out-Null}`], { encoding: 'utf8', windowsHide: true });
}

try {
  app = await electron.launch({ args: ['electron/main.cjs'], cwd: root, env, timeout: 45000 });
  const page = await app.firstWindow();
  await page.getByLabel('作品链接').waitFor();
  const first = JSON.parse(await fs.readFile(runtimePath, 'utf8'));
  const response = await fetch(first.url + '/api/documents', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Lulu-Client': 'desktop' }, body: JSON.stringify({ title: '生命周期回归', text: '关闭并重开后仍保留这份文稿。' }) });
  assert(response.ok);
  const document = await response.json();
  await app.evaluate(({ app, BrowserWindow, dialog }) => {
    global.__lifecycleErrors = [];
    global.__secondInstances = 0;
    dialog.showErrorBox = (title, message) => global.__lifecycleErrors.push({ title, message });
    app.on('second-instance', () => global.__secondInstances++);
    BrowserWindow.getAllWindows()[0].minimize();
  });
  await secondInstance();
  await waitFor(() => app.evaluate(({ BrowserWindow }) => !BrowserWindow.getAllWindows()[0].isMinimized()), 'restore minimized window');
  assert.equal(await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows().length), 1);
  checks.push('A real second launch restores the single existing window');

  // Keep shutdown in flight long enough to exercise rapid relaunch deterministically.
  // Normal production launches use [root]; remove Playwright debugger args only for this test relaunch.
  await app.evaluate(async ({ app, BrowserWindow }, { root, data }) => {
    const originalFetch = global.fetch;
    global.fetch = async (...args) => {
      if (String(args[0]).endsWith('/api/shutdown')) await new Promise(resolve => setTimeout(resolve, 2000));
      return originalFetch(...args);
    };
    const originalRelaunch = app.relaunch.bind(app);
    let relaunchCalls = 0;
    app.relaunch = () => {
      process.getBuiltinModule('fs').writeFileSync(process.getBuiltinModule('path').join(data, 'relaunch.json'), JSON.stringify({ calls: ++relaunchCalls, errors: global.__lifecycleErrors }));
      originalRelaunch({ args: [root] });
    };
    const target = BrowserWindow.getAllWindows()[0];
    await new Promise(resolve => { target.once('closed', resolve); target.close(); });
    // Exercise late OS callbacks and duplicate launch requests after the HWND is gone.
    target.emit('enter-full-screen');
    target.emit('leave-full-screen');
    app.emit('second-instance', {}, [], root);
    app.emit('second-instance', {}, [], root);
    // A second quit request must not skip the outstanding backend shutdown.
    app.quit();
  }, { root, data });
  const decision = JSON.parse(await fs.readFile(path.join(data, 'relaunch.json'), 'utf8'));
  assert.equal(decision.calls, 1);
  assert.deepEqual(decision.errors, []);
  assert((await fetch(first.url + '/api/health')).ok, 'Repeated quit terminated the backend before graceful cleanup');
  checks.push('Destroyed-window callbacks and repeated launch/quit requests are safe; one relaunch is scheduled');
  await waitFor(async () => {
    try {
      const next = JSON.parse(await fs.readFile(runtimePath, 'utf8'));
      if (next.pid === first.pid || !next.visible) return false;
      if (!(await fetch(next.url + '/api/health')).ok) return false;
      restarted = next;
      return true;
    } catch { return false; }
  }, 'native relaunch after graceful shutdown', 45000);
  const saved = await (await fetch(restarted.url + '/api/items/' + document.id)).json();
  assert.equal(saved.transcript, document.transcript);
  await assert.rejects(fetch(first.url + '/api/health'));
  checks.push('Native relaunch opens a fresh visible window, closes the old server and preserves the library');
  const closingAt = Date.now();
  closeOwnedWindow(restarted.pid);
  await waitFor(async () => { try { await fetch(restarted.url + '/api/health'); return false; } catch { return true; } }, 'normal shutdown');
  assert(Date.now() - closingAt < 15000, 'Idle exit unnecessarily waited for the 20-second deadline');
  checks.push('A normal idle close stops the server without the full grace-period delay');
  await fs.writeFile(path.join(data, 'result.json'), JSON.stringify({ checks, passed: checks.length, error_dialogs: decision.errors.length }, null, 2));
  console.log(JSON.stringify({ checks, passed: checks.length, evidence_dir: data }, null, 2));
} finally {
  // Only this test's exact process IDs and isolated profile are involved.
  if (restarted) closeOwnedWindow(restarted.pid);
  if (app) await app.close().catch(() => {});
}
