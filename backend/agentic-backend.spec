# PyInstaller spec for agentic-backend — bundles FastAPI server + venv libs.
# Build:   venv\Scripts\pyinstaller agentic-backend.spec --clean --noconfirm
# Output:  dist/agentic-backend/agentic-backend.exe (+ supporting files)
#
# The Playwright Chromium browser is NOT bundled here — it lives in
# %LOCALAPPDATA%\ms-playwright and gets shipped via electron-builder
# extraResources at the Electron-packaging step.

from PyInstaller.utils.hooks import collect_submodules, collect_data_files
import os
from pathlib import Path

block_cipher = None
HERE = Path(SPECPATH)  # backend/

# ---- Hidden imports the static analyzer misses ----
hidden = []
# LLM SDKs use lazy/dynamic imports per provider.
for mod in ("agno", "openai", "anthropic", "google.generativeai"):
    try:
        hidden += collect_submodules(mod)
    except Exception:
        pass
# trafilatura ships lazy language-detection modules.
hidden += collect_submodules("trafilatura")
# uvicorn loop drivers
hidden += [
    "uvicorn.loops.asyncio",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets.websockets_impl",
    "uvicorn.lifespan.on",
]

# ---- Data files (non-Python assets) ----
datas = []
# Frontend HTML — server.py serves it via FileResponse(../frontend/index.html).
frontend = HERE.parent / "frontend"
if frontend.is_dir():
    datas.append((str(frontend), "frontend"))
# trafilatura ships a settings.cfg & TLD data file
try:
    datas += collect_data_files("trafilatura")
except Exception:
    pass
# playwright driver scripts (the node-based driver runner)
try:
    datas += collect_data_files("playwright", include_py_files=False)
except Exception:
    pass

a = Analysis(
    [str(HERE / "server.py")],
    pathex=[str(HERE)],
    binaries=[],
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        # Test frameworks bloat the bundle and aren't used at runtime.
        "pytest", "pytest_asyncio", "_pytest",
        # Jupyter / IPython / matplotlib pulled in transitively somewhere
        "IPython", "jupyter", "matplotlib", "tkinter",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="agentic-backend",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,            # UPX trips Windows Defender heuristics
    console=True,         # keep stdout/stderr visible for now; flip to False at v1
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="agentic-backend",
)
