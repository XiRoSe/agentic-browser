"""Orchestrator — plans, fans out scrapers in parallel, synthesizes HTML.

Three-stage flow per intent:
  1. PLAN: orchestrator-tier model + search_web tool → ScrapePlan (3-6 jobs)
  2. SCRAPE: asyncio.gather over per-URL scraper sub-agents (cheaper-tier model),
     each with its own Playwright context, capped by a Semaphore.
  3. SYNTHESIZE: orchestrator-tier model, no tools → HTML fragment with citations.

The LLM provider + key + (optional) model overrides come in via LLMCreds — read
from per-request headers in the HTTP layer. No keys are ever persisted server-side.
"""

import asyncio
import json
import logging
import os
import re
from typing import Awaitable, Callable, Optional

from agno.agent import Agent

log = logging.getLogger("orchestrator")

from logic.agent.contracts import ScrapeJob, ScrapePlan, ScrapeResult, hydrate
from logic.agent.models import LLMCreds
from logic.agent.prompts import PLANNER_PROMPT, SYNTHESIZER_PROMPT
from logic.agent.scraper import scrape
from logic.cache import scrape_cache
from logic.tools.browse import ProgressEvent
from logic.tools.search import search_web

CONCURRENCY = int(os.getenv("SCRAPE_CONCURRENCY", "6"))


ProgressCallback = Callable[[ProgressEvent], Awaitable[None]]


class ViewAgentError(Exception):
    """Raised when the orchestrator returns an error or non-HTML response."""


# ==================== Plan ====================

async def plan_jobs(intent: str, creds: LLMCreds) -> ScrapePlan:
    planner = Agent(
        name="Planner",
        model=creds.model("orchestrator"),
        instructions=[PLANNER_PROMPT],
        tools=[search_web],
        output_schema=ScrapePlan,
        markdown=False,
    )
    msg = f"User intent: {intent}\n\nSearch, choose 3-6 URLs, and write a focused goal for each. Return a ScrapePlan."
    response = await planner.arun(msg)
    raw = response.content if response else None
    plan = hydrate(raw, ScrapePlan)
    if plan is None:
        raise ViewAgentError(f"Planner returned non-structured output: {str(raw)[:200]}")
    if not plan.jobs:
        raise ViewAgentError("Planner produced an empty job list — search may have failed.")
    return plan


# ==================== Scrape (parallel) ====================

async def run_scrapers(
    jobs: list[ScrapeJob],
    creds: LLMCreds,
    progress: Optional[ProgressCallback] = None,
    concurrency: Optional[int] = None,
) -> list[ScrapeResult]:
    sem = asyncio.Semaphore(concurrency or CONCURRENCY)

    async def one(job: ScrapeJob) -> ScrapeResult:
        cached = scrape_cache.get(job.url, job.goal)
        if cached is not None:
            cached_result = ScrapeResult(**cached)
            if progress:
                await progress(ProgressEvent(
                    job_url=job.url, status="cache", step=0, current_url=job.url,
                    fact_count=len(cached_result.facts),
                ))
            return cached_result
        async with sem:
            result = await scrape(job, creds, progress)
        if result.status in ("ok", "partial") and result.facts:
            scrape_cache.put(job.url, job.goal, result.model_dump())
        return result

    return await asyncio.gather(*(one(j) for j in jobs))


# ==================== Synthesize ====================

def _strip_fences(text: str) -> str:
    if not text:
        return text
    t = text.strip()
    m = re.match(r"^```(?:html)?\s*\n?(.*?)\n?```\s*$", t, re.DOTALL)
    if m:
        return m.group(1).strip()
    return t


def _validate_html(text: str) -> str:
    if not text or not text.strip():
        raise ViewAgentError("Empty response from synthesizer")
    t = text.strip()
    if "<" not in t or ">" not in t:
        raise ViewAgentError(f"Non-HTML response: {t[:300]}")
    return t


def _facts_payload(results: list[ScrapeResult]) -> str:
    sources = []
    all_facts = []
    all_images = []
    for r in results:
        sources.append({
            "job_url": r.job_url,
            "status": r.status,
            "fact_count": len(r.facts),
            "relevance_score": r.relevance_score,
            "error": r.error,
            "notes": r.notes,
        })
        # Dropped sources contribute no facts even if the agent put some in (defensive).
        if r.status == "dropped":
            continue
        for f in r.facts:
            all_facts.append(f.model_dump())
        for im in r.images:
            all_images.append(im.model_dump())
    dropped_count = sum(1 for r in results if r.status == "dropped")
    # Cap images to keep prompt cost bounded.
    return json.dumps({
        "sources": sources,
        "dropped_count": dropped_count,
        "facts": all_facts,
        "images": all_images[:12],
    }, ensure_ascii=False, indent=2)


def _empty_state_html(intent: str, results: list[ScrapeResult]) -> str:
    """Honest empty-state when every scraper returned zero facts.

    Why deterministic instead of asking the synthesizer: when given zero facts
    the LLM falls back to general knowledge and produces a confident-sounding
    "here's a jumping-off hub" answer — a lie. This template tells the user
    exactly which sources were attempted and what each one's outcome was.
    """
    from html import escape

    rows = []
    for r in results:
        host = (r.job_url or "").split("/")[2] if "://" in (r.job_url or "") else (r.job_url or "?")
        label = {
            "timeout": "timed out",
            "failed":  "failed",
            "dropped": "dropped — not relevant",
            "partial": "partial",
            "ok":      "ok (no extractable facts)",
        }.get(r.status, r.status)
        detail = escape(r.error or r.notes or "")
        rows.append(
            f'<li class="flex items-center gap-3 py-2 border-b border-slate-100 last:border-0">'
            f'<img src="https://www.google.com/s2/favicons?domain={escape(host)}&sz=32" class="w-4 h-4 rounded">'
            f'<a href="{escape(r.job_url or "#")}" target="_blank" '
            f'onclick="window.openExternal && window.openExternal(\'{escape(r.job_url or "")}\'); return false;" '
            f'class="text-sm text-slate-700 hover:text-indigo-700 truncate flex-1">{escape(host)}</a>'
            f'<span class="text-xs text-slate-500 shrink-0">{escape(label)}</span>'
            f'</li>'
        )

    chips = []
    for alt in (
        f"{intent} — only facts from Wikipedia",
        f"{intent} — overview only",
        f"compare top sources for: {intent}",
    ):
        chips.append(
            f'<button onclick="window.askIntent && window.askIntent({json.dumps(alt)})" '
            f'class="px-3 py-1.5 rounded-full bg-white border border-slate-200 text-xs font-medium text-slate-700 '
            f'hover:bg-slate-50 hover:border-indigo-300 hover:text-indigo-700 transition">{escape(alt)}</button>'
        )

    return (
        '<div class="space-y-6 ab-stage">'
        '<script src="https://cdn.tailwindcss.com"></script>'
        '<style>.ab-stage>*{animation:abIn .45s cubic-bezier(.2,.7,.2,1) both}'
        '@keyframes abIn{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}</style>'
        '<div class="rounded-2xl bg-amber-50 border border-amber-200 p-6 md:p-8">'
        '<div class="flex items-start gap-4">'
        '<div class="w-12 h-12 rounded-full bg-amber-200 flex items-center justify-center text-2xl shrink-0">🪹</div>'
        '<div class="flex-1">'
        '<h1 class="text-2xl md:text-3xl font-bold tracking-tight text-slate-900">I came up empty on this one</h1>'
        f'<p class="text-sm text-slate-600 mt-1">I tried {len(results)} sources for you and every single one either timed out, blocked the bot, or didn\'t actually contain the answer. I\'m not going to make something up — here\'s exactly what happened, in case you want to try a different angle.</p>'
        '</div></div></div>'
        '<div class="rounded-2xl bg-white border border-slate-200/70 p-6 md:p-8 shadow-sm">'
        '<h2 class="text-lg font-semibold text-slate-900 mb-3">What we tried</h2>'
        f'<ul class="divide-y divide-slate-100">{"".join(rows)}</ul>'
        '</div>'
        '<div class="rounded-2xl bg-white border border-slate-200/70 p-6 md:p-8 shadow-sm">'
        '<h2 class="text-lg font-semibold text-slate-900 mb-3">Try a different angle</h2>'
        f'<div class="flex flex-wrap gap-2">{"".join(chips)}</div>'
        '</div>'
        '</div>'
    )


async def synthesize(intent: str, results: list[ScrapeResult], creds: LLMCreds) -> str:
    synth = Agent(
        name="Synthesizer",
        model=creds.model("orchestrator"),
        instructions=[SYNTHESIZER_PROMPT],
        markdown=False,
    )
    payload = _facts_payload(results)
    msg = (
        f"User intent:\n{intent}\n\n"
        f"Scraper results (facts + per-source diagnostics):\n{payload}\n\n"
        f"Compose ONE HTML fragment that answers the intent, with numbered citations. "
        f"Output ONLY HTML — no markdown fences, no explanations."
    )
    response = await synth.arun(msg)
    content = response.content if response else ""
    return _validate_html(_strip_fences(content))


# ==================== Public API ====================

async def generate_view(
    intent: str,
    creds: LLMCreds,
    user_id: str = "demo",
    progress: Optional[ProgressCallback] = None,
    scrape_timeout: Optional[int] = None,
    scrape_concurrency: Optional[int] = None,
) -> tuple[str, int]:
    """Returns (html, total_facts_collected_across_all_scrapers).

    scrape_timeout (seconds, per sub-agent) and scrape_concurrency (max parallel
    sub-agents) override the planner defaults and the env-derived CONCURRENCY.
    """
    if progress:
        await progress(ProgressEvent(job_url=None, status="planning", step=0, current_url=None))
    plan = await plan_jobs(intent, creds)
    # Honor the user's per-request timeout — overrides whatever the planner picked.
    if scrape_timeout:
        for j in plan.jobs:
            j.max_seconds = scrape_timeout
    if progress:
        # Tell the UI how many sub-agents to expect (for the N-of-M counter).
        await progress(ProgressEvent(
            job_url=None, status="planned", step=0, current_url=None,
            fact_count=len(plan.jobs),
        ))
        for j in plan.jobs:
            await progress(ProgressEvent(
                job_url=j.url, status="queued", step=0, current_url=j.url, goal=j.goal,
            ))
    results = await run_scrapers(plan.jobs, creds, progress, concurrency=scrape_concurrency)
    total_facts = sum(len(r.facts) for r in results)
    total_images = sum(len(r.images) for r in results)
    successes = sum(1 for r in results if r.status in ("ok", "partial") and r.facts)
    log.info(
        "render intent=%r jobs=%d facts=%d images=%d successful_sources=%d",
        intent[:80], len(results), total_facts, total_images, successes,
    )
    if total_facts == 0:
        log.warning(
            "render EMPTY-STATE intent=%r statuses=%s errors=%s",
            intent[:80],
            [r.status for r in results],
            [r.error for r in results],
        )
        return _empty_state_html(intent, results), 0
    if progress:
        await progress(ProgressEvent(job_url=None, status="synthesizing", step=0, current_url=None))
    html = await synthesize(intent, results, creds)
    return html, total_facts


async def edit_view(
    current_html: str,
    instruction: str,
    intent: str,
    creds: LLMCreds,
    user_id: str = "demo",
) -> str:
    synth = Agent(
        name="Synthesizer",
        model=creds.model("orchestrator"),
        instructions=[SYNTHESIZER_PROMPT],
        markdown=False,
    )
    msg = (
        f"You are editing an existing view.\n\n"
        f"Original intent: {intent}\n\n"
        f"Current view HTML:\n```html\n{current_html}\n```\n\n"
        f"User instruction: {instruction}\n\n"
        f"Apply ONLY the requested change. Preserve everything else, INCLUDING all citations. "
        f"Return the COMPLETE updated HTML fragment — no markdown fences."
    )
    response = await synth.arun(msg)
    content = response.content if response else ""
    return _validate_html(_strip_fences(content))


def clear_history(intent: str, user_id: str = "demo") -> bool:
    return True
