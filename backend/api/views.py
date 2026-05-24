"""HTTP + WebSocket endpoints.

Auth model: stateless. The user's chosen LLM provider + key arrive as
request headers; no key ever persists server-side.

Headers (case-insensitive):
  X-LLM-Provider          openai | anthropic | google
  X-LLM-Key               the user's API key
  X-Orchestrator-Model    optional override (e.g. claude-opus-4-7)
  X-Scraper-Model         optional override
"""

import asyncio
import io
import json
import os
import shutil
import time
import uuid
import zipfile
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, File, Header, HTTPException, Query, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from logic.agent.models import LLMCreds, Provider
from logic.agent.orchestrator import (
    ViewAgentError,
    clear_history,
    edit_view,
    generate_view,
)
from logic.cache import view_cache, tabs_store, scrape_cache
from logic.favorites import favorites
from logic.tools.browse import ProgressEvent

router = APIRouter(prefix="/api", tags=["views"])

DEFAULT_USER = "demo"

# ==================== Creds ====================

def _read_creds(
    provider: Optional[str],
    api_key: Optional[str],
    orchestrator_model: Optional[str],
    scraper_model: Optional[str],
) -> LLMCreds:
    if not provider:
        raise HTTPException(status_code=400, detail="Missing X-LLM-Provider header")
    if not api_key:
        raise HTTPException(status_code=400, detail="Missing X-LLM-Key header")
    if provider not in ("openai", "anthropic", "google"):
        raise HTTPException(status_code=400, detail=f"Unknown provider: {provider!r}")
    return LLMCreds(
        provider=provider,  # type: ignore[arg-type]
        api_key=api_key,
        orchestrator_model=orchestrator_model or None,
        scraper_model=scraper_model or None,
    )


# ==================== Live progress (WebSocket) ====================

class _JobBus:
    """In-process pub/sub from orchestrator/scrapers → connected WS clients."""

    def __init__(self) -> None:
        self._queues: dict[str, asyncio.Queue] = {}

    def open(self, job_id: str) -> asyncio.Queue:
        # Idempotent — render path pre-opens the queue so early progress events
        # aren't lost if the WS connects a beat later. WS handler also calls
        # open() and gets the same queue.
        q = self._queues.get(job_id)
        if q is None:
            q = asyncio.Queue(maxsize=256)
            self._queues[job_id] = q
        return q

    def close(self, job_id: str) -> None:
        self._queues.pop(job_id, None)

    async def push(self, job_id: str, event: ProgressEvent) -> None:
        q = self._queues.get(job_id)
        if q is None:
            return
        try:
            q.put_nowait(event.to_dict())
        except asyncio.QueueFull:
            # Renderer can't keep up — drop the event rather than block the agent.
            pass


_bus = _JobBus()


@router.websocket("/ws/scrape/{job_id}")
async def ws_scrape(ws: WebSocket, job_id: str) -> None:
    await ws.accept()
    q = _bus.open(job_id)
    try:
        while True:
            event = await q.get()
            await ws.send_json(event)
            if event.get("status") == "__end__":
                break
    except WebSocketDisconnect:
        pass
    finally:
        _bus.close(job_id)


# ==================== Render / Edit ====================

class RenderResponse(BaseModel):
    intent: str
    html: str
    from_cache: bool
    facts_count: int = 0  # total Facts collected across all scrapers; 0 = empty/failed render


class EditRequest(BaseModel):
    intent: str
    instruction: str
    user_id: str = DEFAULT_USER


class EditResponse(BaseModel):
    intent: str
    html: str


def _clamped_int(raw: Optional[str], default: int, lo: int, hi: int) -> int:
    try:
        v = int(raw) if raw is not None else default
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


@router.get("/render", response_model=RenderResponse)
async def render(
    intent: str = Query(...),
    user_id: str = Query(DEFAULT_USER),
    force: bool = Query(False),
    job_id: Optional[str] = Query(None, description="If set, progress events are pushed to /ws/scrape/{job_id}."),
    x_llm_provider: Optional[str] = Header(None, alias="X-LLM-Provider"),
    x_llm_key: Optional[str] = Header(None, alias="X-LLM-Key"),
    x_orchestrator_model: Optional[str] = Header(None, alias="X-Orchestrator-Model"),
    x_scraper_model: Optional[str] = Header(None, alias="X-Scraper-Model"),
    x_scrape_timeout: Optional[str] = Header(None, alias="X-Scrape-Timeout"),
    x_scrape_concurrency: Optional[str] = Header(None, alias="X-Scrape-Concurrency"),
    x_scrape_word_cap: Optional[str] = Header(None, alias="X-Scrape-Word-Cap"),
):
    if not force:
        cached = view_cache.get(user_id, intent)
        if cached:
            # We don't know fact counts for cached views; default to 1 so the UI
            # doesn't show a misleading "no data" pill on cache hits.
            return RenderResponse(intent=intent, html=cached, from_cache=True, facts_count=1)

    creds = _read_creds(x_llm_provider, x_llm_key, x_orchestrator_model, x_scraper_model)
    scrape_timeout = _clamped_int(x_scrape_timeout, default=120, lo=30, hi=300)
    scrape_concurrency = _clamped_int(x_scrape_concurrency, default=6, lo=1, hi=10)
    scrape_word_cap = _clamped_int(x_scrape_word_cap, default=1000, lo=200, hi=5000)

    progress = None
    if job_id:
        # Pre-create the queue so events fired before the WS finishes its
        # handshake still land in the buffer.
        _bus.open(job_id)
        async def _push(e: ProgressEvent) -> None:
            await _bus.push(job_id, e)
        progress = _push

    try:
        html, facts_count = await generate_view(
            intent, creds=creds, user_id=user_id, progress=progress,
            scrape_timeout=scrape_timeout, scrape_concurrency=scrape_concurrency,
            word_cap=scrape_word_cap,
        )
    except ViewAgentError as e:
        raise HTTPException(status_code=502, detail=f"View agent error: {e}")
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}")
    finally:
        if job_id:
            await _bus.push(job_id, ProgressEvent(job_url=None, status="__end__"))

    view_cache.put(user_id, intent, html)
    return RenderResponse(intent=intent, html=html, from_cache=False, facts_count=facts_count)


@router.post("/edit", response_model=EditResponse)
async def edit(
    req: EditRequest,
    x_llm_provider: Optional[str] = Header(None, alias="X-LLM-Provider"),
    x_llm_key: Optional[str] = Header(None, alias="X-LLM-Key"),
    x_orchestrator_model: Optional[str] = Header(None, alias="X-Orchestrator-Model"),
    x_scraper_model: Optional[str] = Header(None, alias="X-Scraper-Model"),
):
    current = view_cache.get(req.user_id, req.intent)
    if not current:
        raise HTTPException(status_code=404, detail="No cached view for this intent. Render it first.")
    creds = _read_creds(x_llm_provider, x_llm_key, x_orchestrator_model, x_scraper_model)
    try:
        html = await edit_view(current, req.instruction, intent=req.intent, creds=creds, user_id=req.user_id)
    except ViewAgentError as e:
        raise HTTPException(status_code=502, detail=f"View agent error: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    view_cache.put(req.user_id, req.intent, html)
    return EditResponse(intent=req.intent, html=html)


@router.get("/cache")
async def list_cache(user_id: str = Query(DEFAULT_USER)):
    return {"items": view_cache.list_cached(user_id)}


@router.delete("/cache")
async def delete_cache_item(intent: str = Query(...), user_id: str = Query(DEFAULT_USER)):
    cache_deleted = view_cache.delete(user_id, intent)
    clear_history(intent, user_id=user_id)
    return {"deleted": cache_deleted}


# ==================== Connection test (Settings UI) ====================

@router.post("/test-connection")
async def test_connection(
    x_llm_provider: Optional[str] = Header(None, alias="X-LLM-Provider"),
    x_llm_key: Optional[str] = Header(None, alias="X-LLM-Key"),
    x_orchestrator_model: Optional[str] = Header(None, alias="X-Orchestrator-Model"),
    x_scraper_model: Optional[str] = Header(None, alias="X-Scraper-Model"),
):
    """One-shot sanity check used by the Settings 'Test connection' button."""
    creds = _read_creds(x_llm_provider, x_llm_key, x_orchestrator_model, x_scraper_model)
    try:
        from agno.agent import Agent
        agent = Agent(
            name="connection-test",
            model=creds.model("scraper"),  # cheaper tier — same key, same provider
            markdown=False,
        )
        response = await agent.arun("Reply with the single word: pong.")
        text = (response.content if response else "") or ""
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))

    # AGNO swallows provider errors and puts the message in `content`. Treat
    # anything that smells like an auth/quota/perm failure as a 502 so the
    # Settings 'Test' button shows a real failure instead of a fake success.
    lowered = text.lower()
    error_markers = (
        "incorrect api key", "invalid_api_key", "invalid api key",
        "api key not valid", "authentication", "unauthorized",
        "permission_denied", "permission denied",
        "invalid argument", "quota", "rate limit",
        "you can find your api key",
    )
    if any(m in lowered for m in error_markers) or text.strip().startswith("{"):
        raise HTTPException(status_code=502, detail=text[:300] or "Provider returned no content")
    return {"ok": True, "provider": creds.provider, "reply": text[:80]}


# ==================== Favorites ====================

class PublishRequest(BaseModel):
    intent: str
    name: str
    description: str = ""
    user_id: str = DEFAULT_USER


class InstallRequest(BaseModel):
    view_id: int
    user_id: str = DEFAULT_USER


@router.get("/favorites")
async def list_favorites():
    return {"items": favorites.list_all()}


@router.get("/favorites/{view_id}")
async def get_favorite(view_id: int):
    item = favorites.get(view_id)
    if not item:
        raise HTTPException(status_code=404, detail="Favorite not found")
    return item


@router.post("/favorites/publish")
async def save_favorite(req: PublishRequest):
    html = view_cache.get(req.user_id, req.intent)
    if not html:
        raise HTTPException(status_code=404, detail="No cached view for this intent. Render it first.")
    view_id = favorites.publish(
        name=req.name.strip() or req.intent,
        description=req.description,
        intent=req.intent,
        html=html,
        author=req.user_id,
    )
    return {"id": view_id}


@router.post("/favorites/install")
async def open_favorite(req: InstallRequest):
    item = favorites.get(req.view_id)
    if not item:
        raise HTTPException(status_code=404, detail="Favorite not found")
    intent = item["intent"]
    if view_cache.get(req.user_id, intent):
        intent = f"{item['intent']} (from {item['author']})"
    view_cache.put(req.user_id, intent, item["html"])
    favorites.increment_installs(req.view_id)
    return {"intent": intent, "name": item["name"]}


@router.delete("/favorites/{view_id}")
async def remove_favorite(view_id: int):
    deleted = favorites.delete(view_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Favorite not found")
    return {"deleted": True}


@router.get("/health")
async def health():
    return {"status": "healthy"}


# ==================== Persistent tabs ====================

class TabModel(BaseModel):
    intent: str
    label: Optional[str] = None
    kind: Optional[str] = "intent"   # 'intent' | 'url'
    url: Optional[str] = None


class TabsPutRequest(BaseModel):
    tabs: list[TabModel]
    active_intent: Optional[str] = None
    user_id: str = DEFAULT_USER


@router.get("/tabs")
async def get_tabs(user_id: str = Query(DEFAULT_USER)):
    return tabs_store.load(user_id)


@router.put("/tabs")
async def put_tabs(req: TabsPutRequest):
    tabs_store.save(req.user_id, [t.model_dump() for t in req.tabs], req.active_intent)
    return {"saved": len(req.tabs)}


@router.delete("/tabs")
async def delete_tabs(user_id: str = Query(DEFAULT_USER)):
    tabs_store.clear(user_id)
    return {"cleared": True}


# ==================== Closed tabs (history) ====================

class ClosedTabRequest(BaseModel):
    intent: str
    closed_at: int       # ms since epoch
    user_id: str = DEFAULT_USER


@router.get("/tabs/closed")
async def get_closed_tabs(user_id: str = Query(DEFAULT_USER)):
    return {"items": tabs_store.load_closed(user_id)}


@router.post("/tabs/closed")
async def push_closed_tab(req: ClosedTabRequest):
    tabs_store.push_closed(req.user_id, req.intent, req.closed_at)
    return {"ok": True}


@router.delete("/tabs/closed")
async def clear_closed_tabs(
    intent: Optional[str] = Query(None, description="Forget just this one intent (omit to wipe all)"),
    user_id: str = Query(DEFAULT_USER),
):
    if intent:
        deleted = tabs_store.forget_closed(user_id, intent)
        return {"forgot": intent if deleted else None}
    n = tabs_store.clear_closed(user_id)
    return {"cleared": n}


# ==================== Storage stats + backup / restore ====================

DATA_DIR = Path(__file__).parent.parent / "data"
DB_FILES = ["tabs.db", "view_cache.db", "scrape_cache.db", "favorites.db"]


def _file_size(p: Path) -> int:
    try:
        return p.stat().st_size if p.exists() else 0
    except Exception:
        return 0


@router.get("/storage/stats")
async def storage_stats(user_id: str = Query(DEFAULT_USER)):
    """One-stop summary of what's persisted — used by the Settings → Data pane."""
    open_tabs = tabs_store.load(user_id)
    closed = tabs_store.load_closed(user_id)
    views = view_cache.list_cached(user_id)
    favorites_items = favorites.list_all()

    # Scrape cache count (no per-user partition; it's a shared cache).
    import sqlite3
    sc_count = 0
    sc_path = DATA_DIR / "scrape_cache.db"
    if sc_path.exists():
        try:
            conn = sqlite3.connect(str(sc_path))
            sc_count = conn.execute("SELECT COUNT(*) FROM scrape_cache").fetchone()[0]
            conn.close()
        except Exception:
            sc_count = 0

    files = {fn: _file_size(DATA_DIR / fn) for fn in DB_FILES}
    return {
        "tabs":            {"count": len(open_tabs.get("tabs", []))},
        "closed_tabs":     {"count": len(closed)},
        "cached_views":    {"count": len(views), "items": views},
        "scrape_cache":    {"count": sc_count},
        "favorites":       {"count": len(favorites_items)},
        "files":           files,
        "total_bytes":     sum(files.values()),
    }


@router.delete("/storage/scrape-cache")
async def clear_scrape_cache():
    n = scrape_cache.clear()
    return {"cleared": n}


@router.get("/backup")
async def download_backup():
    """Stream a zip of all .db files. User saves it as a backup or to migrate
    to another machine. Plain zip — no encryption — keys aren't in here."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for fn in DB_FILES:
            p = DATA_DIR / fn
            if p.exists():
                z.write(p, arcname=fn)
        manifest = {
            "exported_at": int(time.time()),
            "files": [fn for fn in DB_FILES if (DATA_DIR / fn).exists()],
            "format_version": 1,
        }
        z.writestr("manifest.json", json.dumps(manifest, indent=2))
    buf.seek(0)
    filename = f"agentic-browser-backup-{int(time.time())}.zip"
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/restore")
async def restore_backup(file: UploadFile = File(...)):
    """Replace all .db files with the ones in the uploaded zip.
    Atomic per-file: write to .tmp, then rename. The user should reload the
    Electron window afterward — pretty much like a fresh launch."""
    data = await file.read()
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise HTTPException(status_code=400, detail="Not a valid zip file")

    names = set(z.namelist())
    restored = []
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    for fn in DB_FILES:
        if fn not in names:
            continue
        body = z.read(fn)
        tmp = DATA_DIR / (fn + ".restore.tmp")
        tmp.write_bytes(body)
        # Replace atomically. On Windows os.replace overwrites.
        os.replace(tmp, DATA_DIR / fn)
        restored.append(fn)

    if not restored:
        raise HTTPException(status_code=400, detail="Zip contained no recognised .db files")
    return {"restored": restored, "reload_required": True}
