const { contextBridge, ipcRenderer } = require('electron');
contextBridge.exposeInMainWorld('agentos', {
  connection: () => ipcRenderer.invoke('agentos:connection'),
  openExternal: url => ipcRenderer.invoke('agentos:openExternal',url),
  saveFile: (name,bytes) => ipcRenderer.invoke('agentos:saveFile',{name,bytes}),
  copyText: text => ipcRenderer.invoke('agentos:copyText',text),
});
