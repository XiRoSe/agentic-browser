"""Agentic Browser — FastAPI server."""

import os
import sys
from pathlib import Path

# ── Bundled-exe shim ─────────────────────────────────────────────
# When PyInstaller-packaged, this script is frozen and the
# Playwright python package's bundled-browser path is empty. Point
# Playwright at the standard per-user cache so it finds the
# Chromium that ships with the installer.
if getattr(sys, "frozen", False) and not os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
    local_cache = os.path.expandvars(r"%LOCALAPPDATA%\ms-playwright")
    if os.path.isdir(local_cache):
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = local_cache

# Force stdout/stderr to UTF-8 so Playwright's pretty box-drawing
# error messages don't crash the lifespan handler on a cp1252 host.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

load_dotenv(Path(__file__).parent.parent / ".env", override=True)

from api.views import router as views_router

app = FastAPI(
    title="Agentic Browser",
    description="Generative UI over the open web — search, scrape, synthesize",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(views_router)

def _resolve_frontend_dir() -> Path:
    """Find frontend/ — different layouts in dev vs PyInstaller-bundled vs Electron-shipped.

    Order of attempts:
      1. <bundle>/frontend/   — Electron extraResources (set via env)
      2. <repo>/frontend/     — dev mode, sibling of backend/
      3. <_internal>/frontend/ — PyInstaller --onedir bundle root
    """
    candidates = []
    if os.getenv("AGENTIC_FRONTEND_DIR"):
        candidates.append(Path(os.environ["AGENTIC_FRONTEND_DIR"]))
    here = Path(__file__).resolve()
    candidates.append(here.parent.parent / "frontend")          # dev: backend/ → ../frontend
    candidates.append(here.parent / "frontend")                  # _internal/frontend after PyInstaller datas
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).parent / "frontend")            # next to the exe
        candidates.append(Path(sys.executable).parent / "_internal" / "frontend")
    for c in candidates:
        if c.is_dir() and (c / "index.html").is_file():
            return c
    return candidates[0]  # fall back to dev path; FileResponse will 404 cleanly


FRONTEND_DIR = _resolve_frontend_dir()


@app.on_event("startup")
async def _wipe_caches() -> None:
    """Testing-mode: wipe scrape + view caches on every restart so reproductions
    aren't masked by cached results. Set AB_KEEP_CACHE=1 to disable (production)."""
    if os.getenv("AB_KEEP_CACHE") == "1":
        print("[startup] AB_KEEP_CACHE=1 — keeping caches", flush=True)
        return
    try:
        from logic.cache import scrape_cache, view_cache
        n1 = scrape_cache.clear()
        n2 = view_cache.clear()
        print(f"[startup] wiped caches: scrape={n1} rows, view={n2} rows", flush=True)
    except Exception as e:
        print(f"[startup] cache wipe failed (non-fatal): {e}", flush=True)


@app.on_event("startup")
async def _warm_browser() -> None:
    """Pre-launch Chromium so the first scraper doesn't pay launch latency
    (worth a few seconds off first render)."""
    try:
        from logic.tools.browse import _ensure_browser
        await _ensure_browser()
        print("[startup] Playwright Chromium pre-warmed", flush=True)
    except Exception as e:
        print(f"[startup] browser pre-warm failed (non-fatal): {e}", flush=True)


@app.on_event("shutdown")
async def _shutdown_browser() -> None:
    try:
        from logic.tools.browse import shutdown_browser
        await shutdown_browser()
    except Exception:
        pass


@app.get("/")
async def serve_index():
    return FileResponse(FRONTEND_DIR / "index.html")


if __name__ == "__main__":
    import uvicorn

    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", 8003))
    uvicorn.run(app, host=host, port=port)
