// Copy the Playwright Chromium-headless-shell from the per-user cache into
// shell/staged/ms-playwright/ so electron-builder can package it via a
// relative-path extraResources entry.
//
// Why this exists: electron-builder mis-joins `${env.LOCALAPPDATA}/...`
// absolute paths with the project root, ending up at
// C:\dev\agentic-browser\shell\C:\Users\... — invalid. Staging to a
// relative path under shell/ sidesteps the bug entirely.

const fs = require('node:fs');
const path = require('node:path');

const SRC = path.join(
  process.env.LOCALAPPDATA || '',
  'ms-playwright',
  'chromium_headless_shell-1223'
);
const DEST = path.join(__dirname, 'staged', 'ms-playwright', 'chromium_headless_shell-1223');

if (!fs.existsSync(SRC)) {
  console.error(`[stage:chromium] source not found: ${SRC}`);
  console.error('[stage:chromium] run "playwright install chromium-headless-shell" first');
  process.exit(1);
}

// fs.cpSync is in Node 16.7+; Electron-builder needs Node 18+ anyway.
fs.rmSync(path.dirname(DEST), { recursive: true, force: true });
fs.cpSync(SRC, DEST, { recursive: true });

const size = (p) => {
  let s = 0;
  for (const e of fs.readdirSync(p, { withFileTypes: true })) {
    const f = path.join(p, e.name);
    if (e.isDirectory()) s += size(f);
    else s += fs.statSync(f).size;
  }
  return s;
};
const mb = (size(DEST) / (1024 * 1024)).toFixed(1);
console.log(`[stage:chromium] copied ${SRC}\n[stage:chromium]   → ${DEST}  (${mb} MB)`);
