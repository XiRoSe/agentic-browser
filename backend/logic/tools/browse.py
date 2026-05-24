"""Per-sub-agent Playwright browser session with live progress events.

Architecture:
  - ONE shared `playwright` + `Browser` process for the whole server
  - EACH scraper sub-agent gets its own `BrowserContext` (isolated cookies /
    storage / cache — tens of MB each, cheap)
  - Each `BrowseSession` can carry an async `progress` callback. It is called
    after every navigation action with the current URL + a JPEG screenshot
    (base64-encoded), so the renderer can show live thumbnails of each
    sub-agent as it works.
  - `make_browse_tools(session)` returns AGNO tools bound to that session.
"""

import asyncio
import json
import re
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

import base64

from agno.tools import tool
from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)
import trafilatura

DEFAULT_GOTO_TIMEOUT_MS = 20_000
DEFAULT_ACTION_TIMEOUT_MS = 8_000
MAX_READ_CHARS = 12_000
SCREENSHOT_JPEG_QUALITY = 55  # small enough to stream cheaply

_playwright: Optional[Playwright] = None
_browser: Optional[Browser] = None
_browser_lock = asyncio.Lock()


# ==================== Progress events ====================

@dataclass
class ProgressEvent:
    """One step in a scraper sub-agent's life — sent over the live-progress WS."""
    job_url: Optional[str]                       # which sub-agent (None for orchestrator-level events)
    status: str                                  # 'planning' | 'queued' | 'navigating' | 'reading' | 'ok' | 'partial' | 'failed' | 'timeout' | 'cache' | 'synthesizing'
    step: int = 0
    current_url: Optional[str] = None
    screenshot_b64: Optional[str] = None         # JPEG bytes, base64
    fact_count: Optional[int] = None
    goal: Optional[str] = None
    error: Optional[str] = None
    action: Optional[str] = None                 # 'goto' | 'click' | 'type' | 'back'
    selector: Optional[str] = None

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v is not None}


ProgressCallback = Callable[[ProgressEvent], Awaitable[None]]


# ==================== Shared browser ====================

async def _ensure_browser() -> Browser:
    global _playwright, _browser
    async with _browser_lock:
        if _browser is None or not _browser.is_connected():
            _playwright = await async_playwright().start()
            _browser = await _playwright.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
    return _browser


async def shutdown_browser() -> None:
    global _playwright, _browser
    if _browser is not None:
        try:
            await _browser.close()
        finally:
            _browser = None
    if _playwright is not None:
        try:
            await _playwright.stop()
        finally:
            _playwright = None


# ==================== Per-sub-agent session ====================

class BrowseSession:
    """One isolated browser context + page for a single scraper sub-agent."""

    def __init__(
        self,
        progress: Optional[ProgressCallback] = None,
        job_url: Optional[str] = None,
        goal: Optional[str] = None,
    ) -> None:
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self.steps: int = 0
        self.progress = progress
        self.job_url = job_url
        self.goal = goal
        # Side-channel image collection (don't trust the LLM to forward image URLs).
        self.collected_images: list[dict] = []
        # Forensics — what the agent actually did. Read these in the timeout path.
        self.fetch_calls: int = 0
        self.goto_calls: int = 0
        self.read_calls: int = 0
        self.last_fetch_status: Optional[str] = None
        self.last_fetch_chars: int = 0
        self.last_url: Optional[str] = None
        # Salvage buffer — raw text from every fetch/read. On agent timeout we
        # turn these into Facts instead of throwing the whole scrape away.
        self.text_chunks: list[dict] = []
        # Stop signal — when the LLM should give up making tool calls and emit
        # a final ScrapeResult. Set by fetch/read wrappers when:
        #   - a 403 lands on the first fetch (site is blocking us)
        #   - accumulated text > word_cap (we have enough material; commit)
        # Subsequent tool calls return a STOP instruction the model usually obeys.
        self.abort_signal: Optional[str] = None
        self.word_cap: int = 1000  # tightened from "agent decides" to a hard hint

    def _word_count(self) -> int:
        """Count words across collected text chunks AFTER stripping any HTML/CSS/JS
        tags so the cap reflects real prose, not markup noise."""
        import re
        total = 0
        for c in self.text_chunks:
            text = c.get("text") or ""
            # Strip <script>...</script> and <style>...</style> bodies wholesale.
            text = re.sub(r"<(?:script|style)\b[^>]*>.*?</(?:script|style)>", " ", text, flags=re.I | re.S)
            # Strip remaining tags.
            text = re.sub(r"<[^>]+>", " ", text)
            total += len(text.split())
        return total

    @classmethod
    async def create(
        cls,
        progress: Optional[ProgressCallback] = None,
        job_url: Optional[str] = None,
        goal: Optional[str] = None,
    ) -> "BrowseSession":
        self = cls(progress=progress, job_url=job_url, goal=goal)
        browser = await _ensure_browser()
        self.context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
            locale="en-US",
        )
        self.page = await self.context.new_page()
        return self

    async def close(self) -> None:
        try:
            if self.context is not None:
                await self.context.close()
        except Exception:
            pass
        self.context = None
        self.page = None

    async def _snapshot(self) -> Optional[str]:
        if self.page is None:
            return None
        try:
            png_or_jpeg = await self.page.screenshot(type="jpeg", quality=SCREENSHOT_JPEG_QUALITY, full_page=False)
            return base64.b64encode(png_or_jpeg).decode("ascii")
        except Exception:
            return None

    async def _emit(self, status: str, action: Optional[str], selector: Optional[str] = None) -> None:
        if not self.progress:
            return
        shot = await self._snapshot()
        await self.progress(ProgressEvent(
            job_url=self.job_url,
            status=status,
            step=self.steps,
            current_url=self.page.url if self.page else None,
            screenshot_b64=shot,
            action=action,
            selector=selector,
            goal=self.goal,
        ))


# ==================== Tool factory ====================

def _ok(payload: dict) -> str:
    return json.dumps({"ok": True, **payload}, ensure_ascii=False)


def _err(msg: str) -> str:
    return json.dumps({"ok": False, "error": msg})


async def _extract_text(page: Page) -> str:
    try:
        html = await page.content()
        text = trafilatura.extract(html, include_tables=True, favor_recall=True) or ""
        if not text:
            text = await page.evaluate("() => document.body && document.body.innerText || ''")
        text = re.sub(r"\n{3,}", "\n\n", text or "")
        if len(text) > MAX_READ_CHARS:
            text = text[:MAX_READ_CHARS]
        return text
    except Exception as e:
        return f"[read_text failed: {e}]"


def make_browse_tools(session: BrowseSession) -> list:
    """Return AGNO tools bound to this sub-agent's session."""

    def _stop_instruction(reason: str) -> str:
        msg = {
            "blocked":
                "This site is blocking us (HTTP 403). Do NOT keep trying tools — emit "
                "ScrapeResult NOW with status='failed', empty facts, error='blocked (403)'.",
            "budget":
                f"You have already gathered enough text (>{session.word_cap} words). "
                "STOP using tools. Emit ScrapeResult NOW with the facts you have, "
                "status='ok' or 'partial'.",
        }.get(reason, "Stop using tools and emit ScrapeResult now.")
        return json.dumps({"ok": False, "stop": True, "reason": reason, "instruction": msg})

    @tool(show_result=False)
    async def browse_goto(url: str) -> str:
        """Navigate the session to a URL. Use this before any other browse_* call."""
        if session.abort_signal:
            return _stop_instruction(session.abort_signal)
        if session.page is None:
            return _err("session closed")
        session.steps += 1
        session.goto_calls += 1
        try:
            resp = await session.page.goto(url, timeout=DEFAULT_GOTO_TIMEOUT_MS, wait_until="domcontentloaded")
            try:
                await session.page.wait_for_load_state("networkidle", timeout=4_000)
            except Exception:
                pass
            await session._emit("navigating", action="goto")
            session.last_url = session.page.url
            try:
                from logic.tools.images import extract_images
                html = await session.page.content()
                imgs = extract_images(html, base_url=session.page.url, source_url=session.page.url, max_imgs=3)
                if imgs:
                    session.collected_images.extend(imgs)
            except Exception:
                pass
            return _ok({"current_url": session.page.url, "status": resp.status if resp else None, "step": session.steps})
        except Exception as e:
            await session._emit("failed", action="goto")
            return _err(f"goto failed: {e}")

    @tool(show_result=False)
    async def browse_read_text() -> str:
        """Read the main readable text of the CURRENT page (truncated). Use this to inspect what you just loaded."""
        if session.abort_signal:
            return _stop_instruction(session.abort_signal)
        if session.page is None:
            return _err("session closed")
        session.read_calls += 1
        text = await _extract_text(session.page)
        await session._emit("reading", action=None)
        if text and len(text) > 200:
            session.text_chunks.append({"url": session.page.url, "title": "", "text": text})
            if session._word_count() >= session.word_cap and not session.abort_signal:
                session.abort_signal = "budget"
        return _ok({"current_url": session.page.url, "text": text, "chars": len(text)})

    @tool(show_result=False)
    async def browse_click(selector: str) -> str:
        """Click an element by CSS selector. Use `text=...` for text-match selectors. Increments step counter."""
        if session.page is None:
            return _err("session closed")
        session.steps += 1
        try:
            await session.page.click(selector, timeout=DEFAULT_ACTION_TIMEOUT_MS)
            try:
                await session.page.wait_for_load_state("domcontentloaded", timeout=4_000)
            except Exception:
                pass
            await session._emit("navigating", action="click", selector=selector)
            return _ok({"current_url": session.page.url, "step": session.steps})
        except Exception as e:
            await session._emit("failed", action="click", selector=selector)
            return _err(f"click failed on {selector!r}: {e}")

    @tool(show_result=False)
    async def browse_type(selector: str, text: str, press_enter: bool = False) -> str:
        """Type `text` into an input/textarea matched by CSS selector. Optionally press Enter after."""
        if session.page is None:
            return _err("session closed")
        session.steps += 1
        try:
            await session.page.fill(selector, text, timeout=DEFAULT_ACTION_TIMEOUT_MS)
            if press_enter:
                await session.page.press(selector, "Enter")
                try:
                    await session.page.wait_for_load_state("networkidle", timeout=4_000)
                except Exception:
                    pass
            await session._emit("navigating", action="type", selector=selector)
            return _ok({"current_url": session.page.url, "step": session.steps})
        except Exception as e:
            await session._emit("failed", action="type", selector=selector)
            return _err(f"type failed on {selector!r}: {e}")

    @tool(show_result=False)
    async def browse_wait_for(selector: str, timeout_ms: int = 5_000) -> str:
        """Wait until a CSS selector exists on the page (up to timeout_ms)."""
        if session.page is None:
            return _err("session closed")
        try:
            await session.page.wait_for_selector(selector, timeout=timeout_ms)
            return _ok({"current_url": session.page.url})
        except Exception as e:
            return _err(f"wait_for failed on {selector!r}: {e}")

    @tool(show_result=False)
    async def browse_back() -> str:
        """Go back one entry in the session's navigation history."""
        if session.page is None:
            return _err("session closed")
        session.steps += 1
        try:
            await session.page.go_back(timeout=DEFAULT_GOTO_TIMEOUT_MS)
            await session._emit("navigating", action="back")
            return _ok({"current_url": session.page.url, "step": session.steps})
        except Exception as e:
            return _err(f"back failed: {e}")

    @tool(show_result=False)
    async def browse_current_url() -> str:
        """Return the URL currently loaded in this session."""
        if session.page is None:
            return _err("session closed")
        return _ok({"current_url": session.page.url})

    return [
        browse_goto,
        browse_read_text,
        browse_click,
        browse_type,
        browse_wait_for,
        browse_back,
        browse_current_url,
    ]
