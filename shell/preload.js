// Preload script — bridges a small, typed API into the renderer.
//
// The renderer detects `window.electronAPI` to decide whether to enable
// Electron-only features (settings modal, URL tabs with <webview>, etc.).
// When loaded in a regular web browser this object is undefined, and the
// page falls back to "web-app mode."

const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('electronAPI', {
  isElectron: true,

  app: {
    info: () => ipcRenderer.invoke('app:info'),
    openExternal: (url) => ipcRenderer.invoke('app:openExternal', url),
  },

  settings: {
    get: () => ipcRenderer.invoke('settings:get'),
    set: (obj) => ipcRenderer.invoke('settings:set', obj),
    clear: () => ipcRenderer.invoke('settings:clear'),
  },
});
