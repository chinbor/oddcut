// The page's only way out: a small, fixed API (no Node, no raw IPC).
const { contextBridge, ipcRenderer } = require('electron')

contextBridge.exposeInMainWorld('oddcut', {
  backendStatus: () => ipcRenderer.invoke('backend:status'),
  onBackendReady: (cb) => ipcRenderer.on('backend-ready', (_e, s) => cb(s)),
  onBackendExit: (cb) => ipcRenderer.on('backend-exit', (_e, msg) => cb(msg)),
  api: (method, route, body) => ipcRenderer.invoke('api', method, route, body),
  pickFolder: (title) => ipcRenderer.invoke('dialog:folder', title),
  pickMusic: () => ipcRenderer.invoke('dialog:music'),
  exportVideo: (src) => ipcRenderer.invoke('video:export', src),
  showInFolder: (file) => ipcRenderer.invoke('shell:show', file),
  examples: () => ipcRenderer.invoke('app:examples'),
})
