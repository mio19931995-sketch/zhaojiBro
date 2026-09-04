const { contextBridge, ipcRenderer } = require('electron');
contextBridge.exposeInMainWorld('desktop', {
  openData: () => ipcRenderer.invoke('lulu:open-data'),
  showItem: (id,kind='media') => ipcRenderer.invoke('lulu:show-item', id,kind),
  chooseFolder: () => ipcRenderer.invoke('lulu:choose-folder'),
  openExternal: url => ipcRenderer.invoke('lulu:open-external', url),
  platform: options => ipcRenderer.invoke('lulu:platform', options),
  stopPlatform: () => ipcRenderer.invoke('lulu:stop-platform'),
});
