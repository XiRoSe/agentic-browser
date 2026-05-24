"""Typed contracts between the orchestrator and its scraper sub-agents.

Keeping these as Pydantic models means we can ask AGNO for structured output
(`response_model=`) and the orchestrator never has to parse raw HTML or
prose from the scrapers — only Facts with citations.
"""

import json
import re
from typing import Literal, Optional, Type, TypeVar

from pydantic import BaseModel, Field, ValidationError

T = TypeVar("T", bound=BaseModel)


def hydrate(content: object, model_cls: Type[T]) -> Optional[T]:
    """Coerce an agno response.content into a Pydantic instance.

    Why: agno's structured-output path silently leaves `content` as a string
    when the model returns prose, fenced JSON, or a near-miss schema. A strict
    isinstance() check on the caller side then drops the entire run and we
    lose all the facts the scraper actually collected.
    """
    if isinstance(content, model_cls):
        return content
    if isinstance(content, dict):
        try:
            return model_cls.model_validate(content)
        except ValidationError:
            return None
    if isinstance(content, str):
        s = content.strip()
        m = re.match(r"^```(?:json)?\s*\n?(.*?)\n?```\s*$", s, re.DOTALL)
        if m:
            s = m.group(1).strip()
        try:
            return model_cls.model_validate_json(s)
        except (ValidationError, ValueError):
            pass
        try:
            return model_cls.model_validate(json.loads(s))
        except (ValidationError, ValueError, json.JSONDecodeError):
            return None
    return None


class ScrapeJob(BaseModel):
    """A single 'go to URL X and find Y' assignment for one scraper sub-agent."""
    url: str = Field(..., description="The seed URL the scraper should start on.")
    goal: str = Field(..., description="Focused, one-sentence extraction goal — what facts to look for.")
    max_steps: int = Field(12, description="Max browser navigation actions before the sub-agent must stop.")
    max_seconds: int = Field(120, description="Wall-clock budget for the whole sub-agent run.")


class ScrapePlan(BaseModel):
    """The planner's output — which URLs to scrape and what to look for at each."""
    rationale: str = Field("", description="One sentence on why these sources together answer the intent.")
    jobs: list[ScrapeJob] = Field(default_factory=list)


class Fact(BaseModel):
    """One atomic claim extracted from a page, with the snippet it came from."""
    claim: str = Field(..., description="The fact, stated plainly. E.g. 'Price is $1,299' or 'Released 2024-03-14'.")
    evidence: str = Field("", description="Short snippet from the page that supports the claim.")
    source_url: str = Field(..., description="The page URL the fact was actually found on (may differ from job.url after navigation).")


class ImageRef(BaseModel):
    """One image scraped from a source page — passed to the synthesizer for layout."""
    url: str = Field(..., description="Absolute image URL.")
    alt: str = Field("", description="Alt text (caption-like). Empty if the page didn't provide one.")
    source_url: str = Field("", description="The page the image was found on.")


class ScrapeResult(BaseModel):
    """A scraper sub-agent's final report back to the orchestrator."""
    job_url: str
    status: Literal["ok", "partial", "failed", "timeout", "dropped"]
    facts: list[Fact] = Field(default_factory=list)
    images: list[ImageRef] = Field(default_factory=list, description="Images pulled from the page(s) the sub-agent visited. Populated side-channel — NOT by the LLM.")
    steps_used: int = 0
    elapsed_ms: int = 0
    relevance_score: int = Field(0, ge=0, le=100, description="0-100 estimate of how relevant the fetched page was to the goal. Used by the orchestrator to drop low-relevance sources before the expensive extraction phase.")
    error: str | None = None
    notes: str = Field("", description="Anything the synthesizer should know — e.g. 'price varies by region'.")
