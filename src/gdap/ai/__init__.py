"""AI layer: provider-agnostic LLM access, guarded tools, agents and NL→pipeline (§12–§14)."""

from gdap.ai.providers import HeuristicProvider, build_provider

__all__ = ["HeuristicProvider", "build_provider"]
