"""Static fetcher — the cheap, fast first step before escalating to a browser.

Uses httpx + trafilatura to grab a page and return its main readable text,
stripping nav/footer/ads. Returns truncated text so a scraper sub-agent can
reason about it without blowing its context.

Two flavors:
- `fetch_url`            — plain AGNO tool, no progress events (for direct use)
- `make_fetch_tool(...)` — factory that returns a fetch tool bound to a progress
  callback, so the live-scrapers grid can show "fetching…" status updates even
  though there's no browser to screenshot.
"""

import asyncio
import json
from typing import Awaitable, Callable, Optional

import httpx
import trafilatura
from agno.tools import tool

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

MAX_TEXT_CHARS = 15_000


@tool(show_result=False)
def fetch_url(url: str, max_chars: int = MAX_TEXT_CHARS) -> str:
    """
    Fetch a URL with a normal HTTP client and extract the main readable text
    (nav/footer/ads stripped). This is the FIRST thing to try for any page —
    only escalate to `browse_*` tools if the answer isn't here (e.g. JS-rendered
    site, requires login, requires clicking through to a detail page).

    Args:
        url: Absolute URL to fetch.
        max_chars: Truncate the returned text to this many characters.

    Returns:
        JSON string: {"url", "status", "title", "text", "truncated", "final_url"}
                     or {"url", "status", "error"} on failure.
    """
    try:
        with httpx.Client(headers=DEFAULT_HEADERS, follow_redirects=True, timeout=15.0) as client:
            r = client.get(url)
        if r.status_code >= 400:
            return json.dumps({
                "url": url,
                "status": r.status_code,
                "error": f"HTTP {r.status_code}",
            })

        extracted = trafilatura.extract(
            r.text,
            include_comments=False,
            include_tables=True,
            favor_recall=True,
            url=str(r.url),
        ) or ""

        # Best-effort <title>
        title = ""
        try:
            meta = trafilatura.extract_metadata(r.text, default_url=str(r.url))
            if meta and meta.title:
                title = meta.title
        except Exception:
            pass

        truncated = len(extracted) > max_chars
        text = extracted[:max_chars] if truncated else extracted

        return json.dumps({
            "url": url,
            "final_url": str(r.url),
            "status": r.status_code,
            "title": title,
            "text": text,
            "truncated": truncated,
            "chars": len(extracted),
        }, ensure_ascii=False)
    except httpx.TimeoutException:
        return json.dumps({"url": url, "status": 0, "error": "timeout"})
    except Exception as e:
        return json.dumps({"url": url, "status": 0, "error": str(e)})


# ==================== Progress-aware factory ====================

def make_fetch_tool(progress_callback: Optional[Callable[..., Awaitable[None]]] = None,
                     job_url: Optional[str] = None,
                     goal: Optional[str] = None,
                     image_sink: Optional[list] = None,
                     session: Optional[object] = None):
    """Return a `fetch_url` tool that pushes 'fetching' / 'ok' / 'failed'
    progress events to the live-scrapers grid. Without these, a scraper that
    answers from a static fetch alone would appear stuck on 'queued' for the
    whole run.

    If `image_sink` is provided, each successful fetch appends image refs to it
    (side-channel — bypasses the LLM, which can't be trusted to forward URLs).
    """
    # Defer import to avoid a circular import (browse.py imports from agno too).
    from logic.tools.browse import ProgressEvent
    from logic.tools.images import extract_images

    async def _emit(status: str, current_url: Optional[str] = None, error: Optional[str] = None) -> None:
        if not progress_callback:
            return
        try:
            await progress_callback(ProgressEvent(
                job_url=job_url,
                status=status,
                step=0,
                current_url=current_url or job_url,
                action="fetch",
                error=error,
                goal=goal,
            ))
        except Exception:
            pass

    @tool(show_result=False, name="fetch_url")
    async def fetch_url_with_progress(url: str, max_chars: int = MAX_TEXT_CHARS) -> str:
        """
        Fetch a URL with a normal HTTP client and extract the main readable text
        (nav/footer/ads stripped). Try this FIRST for any page before escalating
        to browse_* tools.
        """
        # Honor the session-level stop signal — if a previous tool already
        # discovered we're blocked or have enough material, short-circuit.
        if session is not None and getattr(session, "abort_signal", None):
            reason = session.abort_signal
            msg = (
                "This site is blocking us (HTTP 403). Emit ScrapeResult NOW with "
                "status='failed' and error='blocked (403)'."
                if reason == "blocked"
                else "You have enough material. Emit ScrapeResult NOW with the facts you have."
            )
            return json.dumps({"ok": False, "stop": True, "reason": reason, "instruction": msg})
        await _emit("navigating", current_url=url)
        # Run blocking httpx in a thread so we don't stall the event loop.
        def _do() -> str:
            try:
                with httpx.Client(headers=DEFAULT_HEADERS, follow_redirects=True, timeout=15.0) as client:
                    r = client.get(url)
                if r.status_code >= 400:
                    return json.dumps({"url": url, "status": r.status_code, "error": f"HTTP {r.status_code}"})
                extracted = trafilatura.extract(r.text, include_comments=False, include_tables=True, favor_recall=True, url=str(r.url)) or ""
                title = ""
                try:
                    meta = trafilatura.extract_metadata(r.text, default_url=str(r.url))
                    if meta and meta.title:
                        title = meta.title
                except Exception:
                    pass
                # Side-channel: collect images (bypass LLM forwarding).
                if image_sink is not None:
                    try:
                        imgs = extract_images(r.text, base_url=str(r.url), source_url=str(r.url), max_imgs=3)
                        if imgs:
                            image_sink.extend(imgs)
                    except Exception:
                        pass
                truncated = len(extracted) > max_chars
                text = extracted[:max_chars] if truncated else extracted
                return json.dumps({
                    "url": url, "final_url": str(r.url), "status": r.status_code,
                    "title": title, "text": text, "truncated": truncated, "chars": len(extracted),
                }, ensure_ascii=False)
            except httpx.TimeoutException:
                return json.dumps({"url": url, "status": 0, "error": "timeout"})
            except Exception as e:
                return json.dumps({"url": url, "status": 0, "error": str(e)})

        if session is not None:
            try:
                session.fetch_calls += 1
                session.last_url = url
            except AttributeError:
                pass
        result_json = await asyncio.to_thread(_do)
        try:
            obj = json.loads(result_json)
            if session is not None:
                try:
                    session.last_fetch_status = str(obj.get("status") or obj.get("error") or "?")
                    session.last_fetch_chars = int(obj.get("chars") or 0)
                    text = obj.get("text") or ""
                    if text and len(text) > 200:
                        session.text_chunks.append({
                            "url": obj.get("final_url") or url,
                            "title": obj.get("title") or "",
                            "text": text,
                        })
                    # 403 from a static fetch: site is blocking bots. Tell the
                    # agent to give up — but still return the raw result so the
                    # current tool call doesn't look like an error.
                    if obj.get("status") == 403 and not session.abort_signal:
                        session.abort_signal = "blocked"
                        obj["stop"] = True
                        obj["instruction"] = (
                            "HTTP 403 — site is blocking us. Emit ScrapeResult NOW with "
                            "status='failed' and error='blocked (403)'."
                        )
                        result_json = json.dumps(obj, ensure_ascii=False)
                    # Word-count budget: if we've accumulated enough across all
                    # fetches/reads, the next tool call will tell the agent to stop.
                    elif session._word_count() >= session.word_cap and not session.abort_signal:
                        session.abort_signal = "budget"
                except AttributeError:
                    pass
            if obj.get("error"):
                await _emit("failed", current_url=url, error=obj["error"])
            else:
                await _emit("reading", current_url=obj.get("final_url") or url)
        except Exception:
            pass
        return result_json

    return fetch_url_with_progress
