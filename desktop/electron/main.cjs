// Oddcut main process: starts the oddcut backend (python -m oddcut.server on 127.0.0.1), relays the
// renderer's API calls to it over IPC (the token never reaches the page), and serves rendered videos
// to the page through the media:// scheme.
const { app, BrowserWindow, dialog, ipcMain, net, protocol, shell } = require('electron')
const { spawn } = require('node:child_process')
const fs = require('node:fs')
const path = require('node:path')
const readline = require('node:readline')
const { pathToFileURL } = require('node:url')

// desktop/ lives inside the odd_analysis project; ODDCUT_ROOT / ODDCUT_PYTHON override the guesses
const ROOT = process.env.ODDCUT_ROOT || path.resolve(__dirname, '..', '..')
const MEDIA_ROOTS = [path.join(ROOT, 'output'), path.join(ROOT, 'assets')]

let backend = null           // { proc, port, token }
let backendError = null
let mainWindow = null
let quitting = false

protocol.registerSchemesAsPrivileged([
  { scheme: 'media', privileges: { standard: true, secure: true, stream: true, supportFetchAPI: true } },
])

function findPython() {
  const candidates = [
    process.env.ODDCUT_PYTHON,
    path.join(ROOT, '.venv', 'Scripts', 'python.exe'),
    path.join(ROOT, '.venv', 'bin', 'python'),
  ].filter(Boolean)
  return candidates.find((p) => fs.existsSync(p)) || (process.platform === 'win32' ? 'python' : 'python3')
}

function startBackend() {
  return new Promise((resolve) => {
    const python = findPython()
    const proc = spawn(python, ['-m', 'oddcut.server', '--port', '0', '--parent-pid', String(process.pid)], {
      cwd: path.join(ROOT, 'code'),
      env: { ...process.env, ODDCUT_ROOT: ROOT, PYTHONIOENCODING: 'utf-8', PYTHONUTF8: '1' },
      stdio: ['ignore', 'pipe', 'pipe'],
      windowsHide: true,
    })
    let stderr = ''
    proc.stderr.on('data', (d) => {
      stderr = (stderr + d.toString()).slice(-4000)
    })
    const lines = readline.createInterface({ input: proc.stdout })
    lines.once('line', (line) => {
      try {
        const msg = JSON.parse(line)
        if (msg.type === 'ready') {
          backend = { proc, port: msg.port, token: msg.token, version: msg.version }
          return resolve(true)
        }
      } catch {}
      backendError = `后端启动输出异常：${line}`
      resolve(false)
    })
    proc.on('error', (err) => {
      backendError = `找不到 Python（${python}）：${err.message}`
      resolve(false)
    })
    proc.on('exit', (code) => {
      if (!backend) {
        backendError = `后端启动失败（退出码 ${code}）\n${stderr}`
        resolve(false)
      } else {
        backendError = `后端已退出（退出码 ${code}）\n${stderr}`
        backend = null
        if (!quitting && mainWindow && !mainWindow.isDestroyed()) mainWindow.webContents.send('backend-exit', backendError)
      }
    })
  })
}

async function callBackend(method, route, body) {
  if (!backend) throw new Error(backendError || '后端还没启动')
  const res = await fetch(`http://127.0.0.1:${backend.port}${route}`, {
    method,
    headers: { 'X-Oddcut-Token': backend.token, 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  const data = await res.json()
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`)
  return data
}

function isMediaAllowed(file) {
  const resolved = path.resolve(file)
  return MEDIA_ROOTS.some((r) => resolved.toLowerCase().startsWith(path.resolve(r).toLowerCase() + path.sep))
}

function registerIpc() {
  ipcMain.handle('backend:status', () => ({
    ready: !!backend, error: backendError, root: ROOT, version: backend?.version,
  }))
  ipcMain.handle('api', (_e, method, route, body) => callBackend(method, route, body))
  ipcMain.handle('dialog:folder', async (_e, title) => {
    const r = await dialog.showOpenDialog(mainWindow, { title, properties: ['openDirectory'] })
    return r.canceled ? null : r.filePaths[0]
  })
  ipcMain.handle('dialog:music', async () => {
    const r = await dialog.showOpenDialog(mainWindow, {
      title: '选择音乐', properties: ['openFile'],
      filters: [{ name: '音频', extensions: ['mp3', 'wav', 'm4a', 'aac', 'flac', 'ogg'] }],
    })
    return r.canceled ? null : r.filePaths[0]
  })
  ipcMain.handle('video:export', async (_e, src) => {
    const r = await dialog.showSaveDialog(mainWindow, {
      title: '导出成片', defaultPath: `Oddcut_${new Date().toISOString().slice(0, 10)}.mp4`,
      filters: [{ name: 'MP4 视频', extensions: ['mp4'] }],
    })
    if (r.canceled || !r.filePath) return null
    await fs.promises.copyFile(src, r.filePath)
    return r.filePath
  })
  ipcMain.handle('shell:show', (_e, file) => shell.showItemInFolder(file))
  ipcMain.handle('app:examples', () => ({
    clips: path.join(ROOT, 'assets', 'public', 'clips'),
    music: path.join(ROOT, 'assets', 'public', 'music', '400.mp3'),
  }))
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1180, height: 820, minWidth: 900, minHeight: 640,
    title: 'Oddcut', backgroundColor: '#14151a',
    webPreferences: { preload: path.join(__dirname, 'preload.cjs'), contextIsolation: true, sandbox: true },
  })
  mainWindow.removeMenu()
  mainWindow.loadFile(path.join(__dirname, '..', 'dist', 'index.html'))
  if (process.argv.includes('--dev')) mainWindow.webContents.openDevTools({ mode: 'detach' })
}

app.whenReady().then(async () => {
  // media://v/?p=<absolute path>: rendered videos and source clips for <video>, nothing outside the project
  protocol.handle('media', (req) => {
    const file = new URL(req.url).searchParams.get('p') || ''
    if (!isMediaAllowed(file)) return new Response('forbidden', { status: 403 })
    return net.fetch(pathToFileURL(file).toString(), { headers: req.headers })
  })
  registerIpc()
  createWindow()
  await startBackend()
  if (mainWindow && !mainWindow.isDestroyed()) mainWindow.webContents.send('backend-ready', { ready: !!backend, error: backendError })
})

app.on('window-all-closed', () => app.quit())
app.on('before-quit', () => {
  quitting = true
  if (backend?.proc && !backend.proc.killed) backend.proc.kill()
})
