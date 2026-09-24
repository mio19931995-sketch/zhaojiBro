const { contextBridge, ipcRenderer } = require('electron');
contextBridge.exposeInMainWorld('desktop', {
  openData: () => ipcRenderer.invoke('lulu:open-data'),
  copyText: text => ipcRenderer.invoke('lulu:copy-text', text),
  showItem: (id,kind='media') => ipcRenderer.invoke('lulu:show-item', id,kind),
  chooseFolder: () => ipcRenderer.invoke('lulu:choose-folder'),
  openExternal: url => ipcRenderer.invoke('lulu:open-external', url),
  platform: options => ipcRenderer.invoke('lulu:platform', options),
  stopPlatform: () => ipcRenderer.invoke('lulu:stop-platform'),
  setFullscreen: value => ipcRenderer.invoke('lulu:set-fullscreen', value),
  onFullscreenChange: callback => {
    const handler = (_event, value) => callback(Boolean(value));
    ipcRenderer.on('lulu:fullscreen-changed', handler);
    return () => ipcRenderer.removeListener('lulu:fullscreen-changed', handler);
  },
});
