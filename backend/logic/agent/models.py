"""Provider-agnostic model factory.

The renderer sends the user's choice of provider + their own API key as
per-request headers, so we never persist keys server-side. This factory
turns (provider, role, key, override) into the right AGNO model wrapper.

Roles:
  - "orchestrator" — planner + synthesizer. Smart, fast, no chain-of-thought needed.
  - "scraper"      — per-URL sub-agent. Cheap, willing to tool-loop.
"""

import os
from typing import Literal

# Lazy provider imports so a user missing one SDK doesn't crash the others.

Role = Literal["orchestrator", "scraper"]
Provider = Literal["openai", "anthropic", "google"]

DEFAULT_MODELS: dict[str, dict[Role, str]] = {
    "openai":    {"orchestrator": "gpt-5.1",          "scraper": "gpt-4o-mini"},
    "anthropic": {"orchestrator": "claude-sonnet-4-6", "scraper": "claude-haiku-4-5"},
    "google":    {"orchestrator": "gemini-2.5-pro",   "scraper": "gemini-2.5-flash"},
}


class UnsupportedProviderError(Exception):
    pass


def resolve_model_id(provider: Provider, role: Role, override: str | None = None) -> str:
    if override:
        return override
    try:
        return DEFAULT_MODELS[provider][role]
    except KeyError as e:
        raise UnsupportedProviderError(f"Unknown provider/role: {provider}/{role}") from e


def make_model(
    provider: Provider,
    role: Role,
    api_key: str,
    override: str | None = None,
):
    """Build an AGNO model wrapper for the given provider + role + key."""
    if not api_key:
        raise ValueError("api_key is required")

    model_id = resolve_model_id(provider, role, override)

    if provider == "openai":
        from agno.models.openai import OpenAIChat
        kwargs = {"id": model_id, "api_key": api_key}
        # GPT-5.x supports reasoning_effort. For the orchestrator we want "minimal"
        # (no thinking step — fast). For the scraper, omit it (cheaper models don't
        # accept it). Older AGNO/openai-sdk combos may not accept the kwarg — drop
        # gracefully.
        if role == "orchestrator" and model_id.startswith(("gpt-5", "o")):
            # GPT-5.x: 'none' disables the reasoning step (was 'minimal' on early
            # GPT-5 previews — current API only accepts none/low/medium/high).
            effort = os.getenv("ORCHESTRATOR_REASONING_EFFORT", "none")
            try:
                return OpenAIChat(**kwargs, reasoning_effort=effort)
            except TypeError:
                pass
        return OpenAIChat(**kwargs)

    if provider == "anthropic":
        from agno.models.anthropic import Claude
        # No "no thinking" knob needed — non-thinking is the default for Claude.
        return Claude(id=model_id, api_key=api_key)

    if provider == "google":
        from agno.models.google import Gemini
        kwargs = {"id": model_id, "api_key": api_key}
        # Gemini supports thinking_budget. 0 = no thinking step.
        if role == "orchestrator":
            try:
                return Gemini(**kwargs, thinking_budget=0)
            except TypeError:
                pass
        return Gemini(**kwargs)

    raise UnsupportedProviderError(f"Unknown provider: {provider!r}")


# ==================== Per-request creds bundle ====================

from dataclasses import dataclass


@dataclass
class LLMCreds:
    """Carried from the HTTP layer through the orchestrator into each sub-agent."""
    provider: Provider
    api_key: str
    orchestrator_model: str | None = None  # override; None → DEFAULT_MODELS
    scraper_model: str | None = None

    def model(self, role: Role):
        override = self.orchestrator_model if role == "orchestrator" else self.scraper_model
        return make_model(self.provider, role, self.api_key, override)
