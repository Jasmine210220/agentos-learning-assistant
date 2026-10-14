const { app, BrowserWindow } = require('electron');
const fs = require('node:fs');
const path = require('node:path');

async function render(input, output) {
  let win;
  try {
    await app.whenReady();
    const { source } = JSON.parse(fs.readFileSync(input, 'utf8'));
    if (typeof source !== 'string' || source.length > 50000) throw new Error('Invalid diagram input');
    win = new BrowserWindow({ show: false, width: 1200, height: 900, webPreferences: { sandbox: true, contextIsolation: true, nodeIntegration: false, partition: 'agentos-render' } });
    win.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
    win.webContents.session.webRequest.onBeforeRequest((details, callback) => callback({ cancel: /^https?:/.test(details.url) }));
    await win.loadURL('data:text/html;charset=utf-8,<html><head><meta charset="utf-8"></head><body></body></html>');
    const script = fs.readFileSync(path.join(__dirname, '../node_modules/mermaid/dist/mermaid.min.js'), 'utf8');
    await win.webContents.executeJavaScript(script + '\n;void 0;');
    const svg = await win.webContents.executeJavaScript(`(async () => {
      mermaid.initialize({ startOnLoad: false, securityLevel: 'strict', theme: 'base',
        themeVariables: { primaryColor: '#edf5f1', primaryTextColor: '#173f36', primaryBorderColor: '#8eb5a7', lineColor: '#638a7c', fontFamily: 'Microsoft YaHei, sans-serif' },
        flowchart: { htmlLabels: false, useMaxWidth: true } });
      return (await mermaid.render('agentosDiagram', ${JSON.stringify(source)})).svg;
    })()`);
    fs.writeFileSync(output, JSON.stringify({ ok: true, svg }), 'utf8');
  } catch (error) {
    fs.writeFileSync(output, JSON.stringify({ ok: false, error: String(error.message).slice(0, 500) }), 'utf8');
  } finally {
    if (win) win.destroy();
    app.exit();
  }
}
module.exports = { render };
if (process.argv[1] && path.resolve(process.argv[1]) === __filename) render(process.argv[2], process.argv[3]);
