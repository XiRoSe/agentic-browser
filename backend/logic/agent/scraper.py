"""One scraper sub-agent per URL.

Each call to `scrape(job, creds, progress)` spins up:
  - a fresh AGNO Agent (per-provider scraper model, e.g. gpt-4o-mini)
  - its own isolated Playwright BrowserContext (via BrowseSession)
  - browse_* tools bound to that session + the static fetch_url tool
  - structured output via response_model=ScrapeResult
  - an optional progress callback that fires on goto/click/type

Wraps the run in asyncio.wait_for so a stuck site can't block the whole render.
"""

import asyncio
import logging
import time
from typing import Awaitable, Callable, Optional

from agno.agent import Agent

from logic.agent.contracts import Fact, ImageRef, ScrapeJob, ScrapeResult, hydrate
from logic.agent.models import LLMCreds
from logic.agent.prompts import SCRAPER_PROMPT
from logic.tools.browse import BrowseSession, ProgressEvent, make_browse_tools
from logic.tools.fetch import make_fetch_tool

log = logging.getLogger("scraper")


ProgressCallback = Callable[[ProgressEvent], Awaitable[None]]


_TAG_RE = __import__("re").compile(r"<[^>]+>")
_WS_RE = __import__("re").compile(r"\s+")


def _strip_html(text: str) -> str:
    """Belt-and-suspenders HTML strip — trafilatura already produces plaintext,
    but defensively drop any straggling tags + collapse whitespace before
    salvage Facts hit the synthesizer."""
    if not text or "<" not in text:
        return text
    return _WS_RE.sub(" ", _TAG_RE.sub(" ", text)).strip()


def _salvage_facts(text_chunks: list[dict], max_facts: int = 3) -> list[Fact]:
    """Build best-effort Facts from raw page text the agent fetched but
    didn't get to structure into ScrapeResult before timing out.

    Each chunk → one Fact: claim = first sentence-ish, evidence = next ~400
    chars, source_url = the page URL. The synthesizer treats these as quotable
    raw material instead of falling to an empty state.
    """
    import re
    out: list[Fact] = []
    seen: set[str] = set()
    for chunk in text_chunks:
        url = chunk.get("url") or ""
        text = _strip_html((chunk.get("text") or "").strip())
        if not text or url in seen:
            continue
        seen.add(url)
        # Trim to a usable chunk; first paragraph is usually the lead.
        paragraphs = [p.strip() for p in re.split(r"\n{2,}", text) if p.strip()]
        lead = paragraphs[0] if paragraphs else text[:800]
        # First sentence-ish as the claim, capped.
        sent_match = re.search(r"(.{30,240}?[.!?])(\s|$)", lead)
        claim = (sent_match.group(1) if sent_match else lead[:200]).strip()
        evidence = lead[:600]
        title = _strip_html((chunk.get("title") or "").strip())
        if title and title.lower() not in claim.lower():
            claim = f"{title} — {claim}" if len(claim) < 180 else claim
        out.append(Fact(claim=claim[:300], evidence=evidence, source_url=url))
        if len(out) >= max_facts:
            break
    return out


def _side_images(collected: list[dict]) -> list[ImageRef]:
    """Dedupe + cap collected image dicts into ImageRef list."""
    out: list[ImageRef] = []
    seen: set[str] = set()
    for d in collected or []:
        u = d.get("url") or ""
        if not u or u in seen:
            continue
        seen.add(u)
        out.append(ImageRef(url=u, alt=d.get("alt", ""), source_url=d.get("source_url", "")))
        if len(out) >= 4:
            break
    return out


async def scrape(
    job: ScrapeJob,
    creds: LLMCreds,
    progress: Optional[ProgressCallback] = None,
    word_cap: Optional[int] = None,
) -> ScrapeResult:
    """Run one focused sub-agent on one URL with its own browser context.

    word_cap: when the agent has accumulated this many words of page text
    (post HTML-strip), the next tool call returns a STOP instruction and the
    agent commits its ScrapeResult instead of spinning further.
    """
    started = time.monotonic()
    session: Optional[BrowseSession] = None
    try:
        session = await BrowseSession.create(progress=progress, job_url=job.url, goal=job.goal)
        if word_cap:
            session.word_cap = word_cap
        fetch_tool = make_fetch_tool(
            progress_callback=progress, job_url=job.url, goal=job.goal,
            image_sink=session.collected_images,
            session=session,
        )
        agent = Agent(
            name="Scraper",
            model=creds.model("scraper"),
            instructions=[SCRAPER_PROMPT],
            tools=[fetch_tool, *make_browse_tools(session)],
            output_schema=ScrapeResult,
            markdown=False,
        )
        msg = (
            f"URL: {job.url}\n"
            f"Goal: {job.goal}\n"
            f"Max browser actions: {job.max_steps}\n"
            f"Wall-clock budget: {job.max_seconds}s\n\n"
            f"Try fetch_url first. Only escalate to browse_* if needed. "
            f"Return a ScrapeResult."
        )

        try:
            response = await asyncio.wait_for(agent.arun(msg), timeout=job.max_seconds)
        except asyncio.TimeoutError:
            elapsed = int((time.monotonic() - started) * 1000)
            salvage = _salvage_facts(session.text_chunks)
            log.warning(
                "scrape TIMEOUT %s elapsed=%dms fetch_calls=%d goto_calls=%d read_calls=%d "
                "last_url=%r last_fetch_status=%r last_fetch_chars=%d steps=%d salvaged_facts=%d salvaged_chunks=%d",
                job.url, elapsed,
                session.fetch_calls, session.goto_calls, session.read_calls,
                session.last_url, session.last_fetch_status, session.last_fetch_chars,
                session.steps, len(salvage), len(session.text_chunks),
            )
            # Salvage: even on timeout, hand the synthesizer the raw text the
            # agent already pulled. Better imperfect data than empty state.
            side_images = _side_images(session.collected_images)
            if progress:
                await progress(ProgressEvent(
                    job_url=job.url,
                    status="partial" if salvage else "timeout",
                    step=session.steps, current_url=None, fact_count=len(salvage),
                ))
            return ScrapeResult(
                job_url=job.url,
                status="partial" if salvage else "timeout",
                facts=salvage,
                images=side_images,
                steps_used=session.steps if session else 0,
                elapsed_ms=elapsed,
                relevance_score=60 if salvage else 0,
                error=f"timed out after {job.max_seconds}s — returning {len(salvage)} salvage fact(s) from raw page text" if salvage else f"exceeded {job.max_seconds}s budget with no usable text",
                notes="Salvaged from raw page text — claims are paraphrased excerpts, not LLM-extracted facts." if salvage else "",
            )
        except Exception as e:
            elapsed = int((time.monotonic() - started) * 1000)
            log.error(
                "scrape AGENT-CRASH %s elapsed=%dms err=%r fetch_calls=%d goto_calls=%d",
                job.url, elapsed, e, session.fetch_calls, session.goto_calls,
            )
            raise

        raw = response.content if response else None
        result = hydrate(raw, ScrapeResult)
        elapsed = int((time.monotonic() - started) * 1000)

        # Pull collected images side-channel, dedupe, cap.
        side_images = _side_images(session.collected_images if session else [])

        if result is not None:
            result.steps_used = session.steps if session else result.steps_used
            result.elapsed_ms = elapsed
            if not result.job_url:
                result.job_url = job.url
            if side_images and not result.images:
                result.images = side_images
            log.info(
                "scrape %s status=%s facts=%d images=%d steps=%d elapsed=%dms err=%r",
                job.url, result.status, len(result.facts), len(result.images),
                result.steps_used, elapsed, result.error,
            )
            if progress:
                await progress(ProgressEvent(
                    job_url=job.url, status=result.status, step=result.steps_used,
                    current_url=None, fact_count=len(result.facts),
                ))
            return result

        log.warning(
            "scrape %s NON-STRUCTURED raw=%r elapsed=%dms",
            job.url, str(raw)[:300], elapsed,
        )
        if progress:
            await progress(ProgressEvent(
                job_url=job.url, status="failed", step=session.steps if session else 0,
                current_url=None, error="non-structured response",
            ))
        return ScrapeResult(
            job_url=job.url,
            status="failed",
            facts=[],
            images=side_images,
            steps_used=session.steps if session else 0,
            elapsed_ms=elapsed,
            error=f"non-structured response: {str(raw)[:200]}",
        )

    except Exception as e:
        elapsed = int((time.monotonic() - started) * 1000)
        if progress:
            await progress(ProgressEvent(
                job_url=job.url, status="failed", step=session.steps if session else 0,
                current_url=None, error=str(e),
            ))
        return ScrapeResult(
            job_url=job.url,
            status="failed",
            facts=[],
            steps_used=session.steps if session else 0,
            elapsed_ms=elapsed,
            error=str(e),
        )
    finally:
        if session is not None:
            await session.close()
