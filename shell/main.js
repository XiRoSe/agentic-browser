// Electron main process for Agentic Browser.
//
// Responsibilities:
//   1. Spawn the Python FastAPI backend (unless AGENTIC_BACKEND_EXTERNAL=1 — useful in dev
//      when you've already run `python server.py` yourself).
//   2. Open one BrowserWindow that loads the existing frontend/index.html.
//   3. Expose two namespaces of IPC to the renderer via preload:
//        - `settings.*`   — encrypted-at-rest provider + API key + model overrides
//        - `app.*`        — quit, openExternal, backendURL, isElectron probe
//      (URL-tab embedding is done with the renderer's <webview> tags — no main-process
//      involvement needed for v0.)

const { app, BrowserWindow, Menu, ipcMain, safeStorage, shell, session } = require('electron');
const path = require('node:path');
const fs = require('node:fs');
const { spawn } = require('node:child_process');

const ROOT = path.resolve(__dirname, '..');
// In dev, frontend/ is next to shell/. In packaged builds, it's shipped as
// extraResources under process.resourcesPath.
const FRONTEND_HTML = app.isPackaged
  ? path.join(process.resourcesPath, 'frontend', 'index.html')
  : path.join(ROOT, 'frontend', 'index.html');
const BACKEND_DIR = path.join(ROOT, 'backend');
const BACKEND_PORT = parseInt(process.env.PORT || '8003', 10);
const BACKEND_URL = `http://127.0.0.1:${BACKEND_PORT}`;
const SETTINGS_PATH = path.join(app.getPath('userData'), 'settings.enc');

let mainWindow = null;
let backendProc = null;

// ==================== Python backend lifecycle ====================

function pythonCandidates() {
  const venvPy = path.join(BACKEND_DIR, 'venv', 'Scripts', 'python.exe');
  const candidates = [];
  if (fs.existsSync(venvPy)) candidates.push(venvPy);
  if (process.platform === 'win32') candidates.push('python.exe', 'py');
  else candidates.push('python3', 'python');
  return candidates;
}

function packagedBackendExe() {
  // When electron-builder packages the app, the backend bundle is shipped via
  // extraResources at: <resources>/backend/agentic-backend.exe (Windows).
  const exe = process.platform === 'win32' ? 'agentic-backend.exe' : 'agentic-backend';
  return path.join(process.resourcesPath, 'backend', exe);
}

function wireBackendChild(child, source) {
  child.stdout.on('data', d => process.stdout.write(`[backend] ${d}`));
  child.stderr.on('data', d => process.stderr.write(`[backend] ${d}`));
  child.on('error', err => console.error(`[backend] spawn error (${source}):`, err.message));
  child.on('exit', (code, signal) => console.log(`[backend] exited code=${code} sig=${signal}`));
  backendProc = child;
  console.log(`[backend] spawned via ${source}`);
}

function spawnBackend() {
  if (process.env.AGENTIC_BACKEND_EXTERNAL === '1') {
    console.log('[backend] AGENTIC_BACKEND_EXTERNAL=1 — assuming an external server is running');
    return;
  }
  // 1) Packaged build: prefer the bundled exe.
  if (app.isPackaged) {
    const exe = packagedBackendExe();
    if (fs.existsSync(exe)) {
      try {
        const bundledPlaywright = path.join(process.resourcesPath, 'ms-playwright');
        const bundledFrontend = path.join(process.resourcesPath, 'frontend');
        const env = { ...process.env, PORT: String(BACKEND_PORT) };
        if (fs.existsSync(bundledPlaywright)) {
          env.PLAYWRIGHT_BROWSERS_PATH = bundledPlaywright;
        }
        if (fs.existsSync(bundledFrontend)) {
          env.AGENTIC_FRONTEND_DIR = bundledFrontend;
        }
        const child = spawn(exe, [], {
          cwd: path.dirname(exe),
          env,
          stdio: ['ignore', 'pipe', 'pipe'],
        });
        wireBackendChild(child, exe);
        return;
      } catch (e) {
        console.error(`[backend] failed to spawn ${exe}:`, e.message);
      }
    } else {
      console.error(`[backend] packaged backend exe missing at ${exe}`);
    }
  }
  // 2) Dev mode (or packaged fallback): spawn Python directly.
  const candidates = pythonCandidates();
  for (const cmd of candidates) {
    try {
      const child = spawn(cmd, ['server.py'], {
        cwd: BACKEND_DIR,
        env: { ...process.env, PORT: String(BACKEND_PORT) },
        stdio: ['ignore', 'pipe', 'pipe'],
      });
      wireBackendChild(child, cmd);
      return;
    } catch (e) {
      console.warn(`[backend] failed to spawn with ${cmd}:`, e.message);
    }
  }
  console.error('[backend] no Python interpreter found — set AGENTIC_BACKEND_EXTERNAL=1 and run the backend yourself');
}

function killBackend() {
  if (backendProc && !backendProc.killed) {
    try { backendProc.kill(); } catch (_) {}
  }
}

async function waitForBackend(timeoutMs = 30_000) {
  const started = Date.now();
  while (Date.now() - started < timeoutMs) {
    try {
      // /api/tabs is a cheap, always-present route — confirms uvicorn is actually serving.
      const r = await fetch(`${BACKEND_URL}/api/tabs`);
      if (r.ok) return true;
    } catch (_) { /* not up yet */ }
    await new Promise(r => setTimeout(r, 300));
  }
  return false;
}

// ==================== Settings storage (safeStorage) ====================

function readSettings() {
  try {
    if (!fs.existsSync(SETTINGS_PATH)) return null;
    const enc = fs.readFileSync(SETTINGS_PATH);
    if (!safeStorage.isEncryptionAvailable()) {
      // Fallback: plaintext. Better than failing — flag it to the renderer.
      try {
        const obj = JSON.parse(enc.toString('utf8'));
        return { ...obj, _unencrypted: true };
      } catch (_) { return null; }
    }
    const plain = safeStorage.decryptString(enc);
    return JSON.parse(plain);
  } catch (e) {
    console.warn('[settings] read failed:', e.message);
    return null;
  }
}

function writeSettings(obj) {
  const json = JSON.stringify(obj);
  let buf;
  if (safeStorage.isEncryptionAvailable()) {
    buf = safeStorage.encryptString(json);
  } else {
    buf = Buffer.from(json, 'utf8');
  }
  fs.mkdirSync(path.dirname(SETTINGS_PATH), { recursive: true });
  fs.writeFileSync(SETTINGS_PATH, buf, { mode: 0o600 });
}

// ==================== Window ====================

async function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1500,
    height: 950,
    backgroundColor: '#f8fafc',
    title: 'Agentic Browser',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
      webviewTag: true, // enable <webview> for URL tabs
    },
  });

  // Permissive permissions inside embedded <webview> guests so normal sites work.
  session.defaultSession.setPermissionRequestHandler((_wc, permission, cb) => {
    const allow = ['notifications', 'media', 'clipboard-read', 'clipboard-sanitized-write'];
    cb(allow.includes(permission));
  });

  // Open new-window/popup links in the system default browser, not inside the shell.
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (/^https?:/.test(url)) {
      shell.openExternal(url);
      return { action: 'deny' };
    }
    return { action: 'allow' };
  });

  await waitForBackend();
  await mainWindow.loadFile(FRONTEND_HTML);

  if (process.env.AGENTIC_DEVTOOLS === '1') {
    mainWindow.webContents.openDevTools({ mode: 'detach' });
  }
}

// ==================== IPC ====================

ipcMain.handle('app:info', () => ({
  backendUrl: BACKEND_URL,
  platform: process.platform,
  electronVersion: process.versions.electron,
}));

ipcMain.handle('app:openExternal', (_e, url) => shell.openExternal(url));

ipcMain.handle('settings:get', () => readSettings());

ipcMain.handle('settings:set', (_e, obj) => {
  writeSettings(obj || {});
  return true;
});

ipcMain.handle('settings:clear', () => {
  try { fs.unlinkSync(SETTINGS_PATH); } catch (_) {}
  return true;
});

// ==================== Lifecycle ====================

app.whenReady().then(async () => {
  // Drop the default File/Edit/View/Window/Help menu bar — this is a browser,
  // not a 1990s desktop app. Window controls + in-app chrome are enough.
  Menu.setApplicationMenu(null);
  spawnBackend();
  await createWindow();

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit();
});

app.on('before-quit', killBackend);
app.on('will-quit', killBackend);
process.on('exit', killBackend);
process.on('SIGINT', () => { killBackend(); process.exit(0); });
