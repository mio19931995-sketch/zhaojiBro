const { app, BrowserWindow, ipcMain, shell, dialog, session, desktopCapturer, clipboard } = require('electron');
const { spawn } = require('node:child_process');
const path = require('node:path');
const fs = require('node:fs');
const net = require('node:net');
const { hasExited, waitForExit } = require('./process-exit.cjs');
const shutdownToken = require('node:crypto').randomBytes(32).toString('hex');

let backend;
let window;
let shutdownPromise;
let shutdownComplete = false;
let restartRequested = false;
let baseURL = '';
const root = path.resolve(__dirname, '..');
const dataDir = process.env.LULU_DATA_DIR || path.join(root, 'data');
const logDir = path.resolve(dataDir) === path.join(root,'data') ? path.join(root,'logs') : path.join(dataDir,'logs');
fs.mkdirSync(logDir, { recursive: true });
const log = fs.openSync(path.join(logDir, 'backend.log'), 'a');
app.setName('Lulu Workbench');
app.setPath('userData', process.env.LULU_USER_DATA || path.join(process.env.LOCALAPPDATA || app.getPath('appData'), 'LuluWorkbench'));
if (!app.requestSingleInstanceLock()) { app.quit(); }
else {
  app.on('second-instance', () => {
    if (app.isQuitting) {
      // Repeated shortcut clicks during shutdown request one fresh instance.
      if (!restartRequested) { restartRequested = true; app.relaunch(); }
      return;
    }
    const target = window;
    if (!target || target.isDestroyed()) return;
    if (target.isMinimized()) target.restore();
    target.show(); target.focus();
  });
  app.whenReady().then(start).catch(error => {
    if (!app.isQuitting) dialog.showErrorBox('Lulu 启动失败', error.message + '\n请查看项目 logs 文件夹。');
    app.quit();
  });
}

async function freePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.on('error', reject);
    server.listen(0, '127.0.0.1', () => { const port = server.address().port; server.close(() => resolve(port)); });
  });
}

async function start() {
  if (app.isQuitting) return;
  const python = path.join(root, '.venv', 'Scripts', 'python.exe');
  if (!fs.existsSync(python)) throw new Error('缺少 Python 运行环境，请先运行项目中的 setup.ps1。');
  if (!fs.existsSync(path.join(root, 'dist', 'index.html'))) throw new Error('缺少界面文件，请先运行 npm run build。');
  const port = await freePort();
  if (app.isQuitting) return;
  baseURL = `http://127.0.0.1:${port}`;
  backend = spawn(python, ['-m', 'backend.main'], {
    cwd: root, windowsHide: true, stdio: ['ignore', log, log],
    env: { ...process.env, LULU_PORT: String(port), LULU_DATA_DIR: dataDir, LULU_SHUTDOWN_TOKEN: shutdownToken, PYTHONIOENCODING: 'utf-8', PYTHONUTF8: '1' },
  });
  let startupError = null;
  backend.on('error', error => { startupError = error; });
  backend.on('exit', code => {
    if (window && !window.isDestroyed() && !app.isQuitting) dialog.showErrorBox('本地处理服务已停止', `退出码：${code}。请关闭后重新打开 Lulu；已保存文稿仍保留在本地。`);
  });
  let ready = false;
  for (let i = 0; i < 100; i++) {
    if (app.isQuitting) return;
    if (startupError) throw startupError;
    if (backend.exitCode !== null) throw new Error('本地处理服务未能启动。');
    try { const result = await fetch(baseURL + '/api/health'); if (result.ok) { ready = true; break; } } catch {}
    await new Promise(resolve => setTimeout(resolve, 250));
  }
  if (app.isQuitting) return;
  if (!ready) throw new Error('等待本地处理服务超时。');

  const mainWindow = window = new BrowserWindow({
    width: 1370, height: 910, minWidth: 900, minHeight: 620, show: false,
    backgroundColor: '#eeece7', title: 'Lulu · Windows 内容工作台',
    icon: path.join(root, 'public', 'app-icon.png'), autoHideMenuBar: true,
    webPreferences: { preload: path.join(__dirname, 'preload.cjs'), nodeIntegration: false, contextIsolation: true, sandbox: true, webSecurity: true, spellcheck: false },
  });
  mainWindow.once('closed', () => { window = null; app.quit(); });
  window.removeMenu();
  const fullscreenChanged = value => {
    if (!mainWindow.isDestroyed() && !mainWindow.webContents.isDestroyed()) mainWindow.webContents.send('lulu:fullscreen-changed', value);
  };
  window.on('enter-full-screen', () => fullscreenChanged(true));
  window.on('leave-full-screen', () => fullscreenChanged(false));
  window.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
  window.webContents.on('will-navigate', (event, url) => { if (!url.startsWith(baseURL + '/')) event.preventDefault(); });
  window.webContents.session.setPermissionRequestHandler((contents, permission, callback, details) => {
    callback(!app.isQuitting && !mainWindow.isDestroyed() && contents === mainWindow.webContents && details.requestingUrl.startsWith(baseURL + '/') && ['media','display-capture'].includes(permission));
  });
  window.webContents.session.setDisplayMediaRequestHandler(async (request, callback) => {
    if (app.isQuitting || mainWindow.isDestroyed() || !request.frame?.url.startsWith(baseURL + '/') || !request.userGesture) { callback({}); return; }
    let sources = [];
    try { sources = await desktopCapturer.getSources({types:['screen']}); } catch {}
    callback(!app.isQuitting && !mainWindow.isDestroyed() && sources.length ? {video:sources[0],audio:'loopback'} : {});
  });
  window.webContents.session.on('will-download', (event, item) => {
    const filename = item.getFilename();
    item.setSaveDialogOptions({ title: '导出到本地', defaultPath: path.join(app.getPath('downloads'), filename) });
  });
  function localEvent(event) {
    if (app.isQuitting || !window || window.isDestroyed() || event.sender !== window.webContents || !event.senderFrame?.url.startsWith(baseURL + '/')) throw new Error('无效的页面来源');
  }
  ipcMain.handle('lulu:open-data', async event => { localEvent(event); await shell.openPath(dataDir); });
  ipcMain.handle('lulu:copy-text', (event, text) => {
    localEvent(event);
    if (typeof text !== 'string' || text.length > 12000) throw new Error('无效的复制内容');
    clipboard.writeText(text);
  });
  ipcMain.handle('lulu:choose-folder', async event => { localEvent(event); const result = await dialog.showOpenDialog(window, { title: '选择保存文件夹', properties: ['openDirectory'] }); return result.canceled ? '' : result.filePaths[0]; });
  ipcMain.handle('lulu:show-item', async (event, id,kind='media') => {
    localEvent(event);
    if (!/^[a-f0-9]{32}$/.test(id)) throw new Error('无效的素材');
    if (!['media','cover','document'].includes(kind)) throw new Error('无效文件类型');
    const result = await fetch(baseURL + '/api/items/' + id+'/file-location?kind='+kind);
    if (!result.ok) throw new Error((await result.json()).detail || '找不到素材');
    const item = await result.json();
    shell.showItemInFolder(item.path);
  });
  const platform = require('./platform.cjs');
  ipcMain.handle('lulu:platform', async (event, options) => { localEvent(event); return platform.capture(options,baseURL,dataDir); });
  ipcMain.handle('lulu:stop-platform', event => { localEvent(event); platform.stop(); });
  ipcMain.handle('lulu:open-external', async (event, url) => { localEvent(event); const parsed = new URL(url); if (!['https:', 'http:'].includes(parsed.protocol)) throw new Error('不支持的链接'); await shell.openExternal(url); });
  ipcMain.handle('lulu:set-fullscreen', (event, value) => { localEvent(event); window.setFullScreen(Boolean(value)); return window.isFullScreen(); });
  await mainWindow.loadURL(baseURL);
  if (app.isQuitting || mainWindow.isDestroyed()) return;
  mainWindow.show();
  fs.writeFileSync(path.join(logDir, 'runtime.json'), JSON.stringify({ url: baseURL, pid: process.pid, visible: mainWindow.isVisible() }));
}
app.on('before-quit', event => {
  if (shutdownComplete) return;
  event.preventDefault();
  if (shutdownPromise) return;
  app.isQuitting = true;
  shutdownPromise = (async () => {
    if (hasExited(backend)) return;
    // Subscribe before the shutdown request: the process can exit before fetch resolves.
    const exited = waitForExit(backend, 20000);
    try { await fetch(baseURL + '/api/shutdown', { method: 'POST', headers: { 'X-Lulu-Client': 'desktop', 'X-Lulu-Shutdown': shutdownToken }, signal: AbortSignal.timeout(2500) }); } catch {}
    // Allow live recording workers to close their segments before terminating the owned tree.
    await exited;
    if (!hasExited(backend)) {
      // Windows venv launchers can have a child interpreter. Only this owned process tree is stopped.
      const killer = spawn('taskkill.exe', ['/PID', String(backend.pid), '/T', '/F'], { windowsHide: true, stdio: 'ignore' });
      await new Promise(resolve => { killer.once('close', resolve); killer.once('error', resolve); });
    }
  })().finally(() => {
    shutdownComplete = true;
    app.quit();
  });
});
app.on('window-all-closed', () => app.quit());
