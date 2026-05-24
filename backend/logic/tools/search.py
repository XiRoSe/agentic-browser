"""Web search tool — DuckDuckGo via the `ddgs` library (no API key).

Returned shape is normalised so the orchestrator's planner can pick URLs
without worrying about the upstream payload format.
"""

import json
from typing import Optional

from agno.tools import tool

try:
    from ddgs import DDGS  # newer package name
except ImportError:  # pragma: no cover — fall back to legacy package
    from duckduckgo_search import DDGS  # type: ignore


@tool(show_result=False)
def search_web(query: str, max_results: int = 6, region: str = "wt-wt") -> str:
    """
    Search the web with DuckDuckGo (no API key needed).

    Args:
        query: The search query.
        max_results: Number of results to return (default 6).
        region: DDG region code (default 'wt-wt' = no region).

    Returns:
        JSON string: {"results": [{"url", "title", "snippet"}, ...]}
    """
    try:
        with DDGS() as ddgs:
            raw = list(ddgs.text(query, region=region, max_results=max_results)) or []
        results = [
            {
                "url": r.get("href") or r.get("url"),
                "title": r.get("title", ""),
                "snippet": r.get("body") or r.get("snippet", ""),
            }
            for r in raw
            if (r.get("href") or r.get("url"))
        ]
        return json.dumps({"query": query, "results": results}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"query": query, "results": [], "error": str(e)})
