const { app, BrowserWindow, ipcMain, dialog, shell, clipboard } = require('electron');
const { spawn } = require('node:child_process');
const path = require('node:path');
const net = require('node:net');
const crypto = require('node:crypto');
const fs = require('node:fs');

if (process.argv.includes('--render-mermaid')) {
  const index = process.argv.indexOf('--render-mermaid');
  require('./render.cjs').render(process.argv[index + 1], process.argv[index + 2]);
} else {
  let backend, window, connection, quitting = false;
  const root = path.join(__dirname, '..');
  const delay = ms => new Promise(resolve => setTimeout(resolve, ms));

  async function freePort() {
    const server = net.createServer();
    await new Promise((resolve, reject) => { server.once('error', reject); server.listen(0, '127.0.0.1', resolve); });
    const port = server.address().port;
    await new Promise(resolve => server.close(resolve));
    return port;
  }

  async function start() {
    const port = await freePort();
    const token = crypto.randomBytes(32).toString('hex');
    connection = { url: `http://127.0.0.1:${port}`, token };
    const env = { ...process.env, AGENTOS_TOKEN: token, AGENTOS_PARENT_PID: String(process.pid), AGENTOS_DATA_DIR: process.env.AGENTOS_DATA_DIR || path.join(app.getPath('userData'), 'data'), AGENTOS_ELECTRON: process.execPath, AGENTOS_PACKAGED: app.isPackaged ? '1' : '0', PYTHONIOENCODING: 'utf-8' };
    delete env.ELECTRON_RUN_AS_NODE;
    const executable = app.isPackaged ? path.join(process.resourcesPath, 'backend', 'agentos-backend', 'agentos-backend.exe') : (process.env.AGENTOS_PYTHON || 'python');
    const args = app.isPackaged ? ['--port', String(port)] : ['-m', 'backend.server', '--port', String(port)];
    backend = spawn(executable, args, { cwd: app.isPackaged ? app.getPath('userData') : root, env, windowsHide: true, stdio: 'ignore' });
    let spawnError;
    backend.on('error', error => { spawnError = error; });
    backend.on('exit', () => {
      if (window && !quitting) dialog.showErrorBox('本地服务已退出', '已有成果已保存在本机。请重新启动应用，查看任务的中断状态。');
    });
    let ready = false;
    for (let i = 0; i < 100; i++) {
      if (spawnError) throw new Error('无法启动 Python 后端，请安装依赖并设置 AGENTOS_PYTHON。');
      if (backend.exitCode !== null) throw new Error('后端启动失败，请检查 Python 依赖。');
      try {
        const response = await fetch(`${connection.url}/health`, { headers: { Authorization: `Bearer ${token}` }, signal: AbortSignal.timeout(800) });
        if (response.ok) { ready = true; break; }
      } catch {}
      await delay(250);
    }
    if (!ready) throw new Error('本地服务启动超时。');
    window = new BrowserWindow({ width: 1460, height: 950, minWidth: 1100, minHeight: 720, backgroundColor: '#f7f8fa', title: 'AgentOS Lite', autoHideMenuBar: true,
      webPreferences: { preload: path.join(__dirname, 'preload.cjs'), contextIsolation: true, sandbox: true, nodeIntegration: false } });
    ipcMain.handle('agentos:connection', event => {
      if (event.sender !== window.webContents) throw new Error('Untrusted sender');
      return connection;
    });
    ipcMain.handle('agentos:openExternal', async(event,value) => {
      if(event.sender !== window.webContents || typeof value !== 'string') throw new Error('Invalid link');
      const url=new URL(value);
      if(url.protocol!=='https:' || !url.hostname.includes('.') || net.isIP(url.hostname) || /\.(local|internal|localhost)$/.test(url.hostname) || url.username || url.password || url.port) throw new Error('Only public HTTPS links are supported');
      await shell.openExternal(url.href);
    });
    ipcMain.handle('agentos:saveFile',async(event,value)=>{
      if(event.sender!==window.webContents || !value || typeof value.name!=='string' || !/^[\w.-]+\.(zip|md|json|svg|mmd|patch)$/.test(value.name) || !(value.bytes instanceof Uint8Array) || value.bytes.byteLength>10_000_000) throw new Error('Invalid export');
      const selection=await dialog.showSaveDialog(window,{title:'导出文件',defaultPath:path.join(app.getPath('downloads'),value.name)});
      if(selection.canceled || !selection.filePath)return false;
      await fs.promises.writeFile(selection.filePath,Buffer.from(value.bytes));return true;
    });
    ipcMain.handle('agentos:copyText',(event,value)=>{
      if(event.sender!==window.webContents || typeof value!=='string' || value.length>100000)throw new Error('Invalid clipboard text');
      clipboard.writeText(value);
    });
    window.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
    window.webContents.on('will-navigate', event => event.preventDefault());
    if (process.env.AGENTOS_DEV_URL) await window.loadURL('http://127.0.0.1:5173');
    else {
      if (!fs.existsSync(path.join(root, 'dist/index.html'))) throw new Error('请先执行 npm run build。');
      await window.loadFile(path.join(root, 'dist/index.html'));
    }
    window.on('close', event => {
      if (quitting) return;
      event.preventDefault();
      (async () => {
        let active = true;
        try {
          const response = await fetch(`${connection.url}/tasks`, { headers: { Authorization: `Bearer ${token}` }, signal: AbortSignal.timeout(2000) });
          const tasks = await response.json();
          const health = await (await fetch(`${connection.url}/health`, {headers: {Authorization: `Bearer ${token}`}, signal: AbortSignal.timeout(2000)})).json();
          active = health.active_chats > 0 || tasks.some(t => !['succeeded', 'failed', 'cancelled', 'interrupted', 'needs_attention'].includes(t.status));
        } catch {}
        if (active) {
          const result = await dialog.showMessageBox(window, { type: 'question', title: '退出工作台', message: '可能仍有任务正在运行。退出会中断任务，已有成果将保留。', buttons: ['继续工作', '退出'], defaultId: 0, cancelId: 0 });
          if (result.response !== 1) return;
        }
        quitting = true;
        app.quit();
      })();
    });
  }
  app.whenReady().then(start).catch(error => { dialog.showErrorBox('AgentOS Lite 启动失败', error.message); quitting = true; app.quit(); });
  app.on('before-quit', () => { if (quitting && backend) backend.kill(); });
  app.on('window-all-closed', () => { quitting = true; if (backend) backend.kill(); app.quit(); });
  process.on('exit', () => { if (backend) backend.kill(); });
}
