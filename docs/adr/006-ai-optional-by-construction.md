# ADR-006 — The AI layer is optional by construction

**Status:** Accepted

## Problem

An "AI-native" platform that stops working without an API key is not a data platform — it is a
model wrapper with a database attached. But an AI layer that is merely bolted on is not useful
either.

## Decision

Two providers implement one port:

* `HeuristicProvider` — deterministic intent routing to the same tools, answers assembled *only*
  from tool results. No network, no credentials, no model.
* `AnthropicProvider` — Claude Messages API with tool use, for natural-language reasoning.

The **agent loop stays in the platform** (`ai/agents/base.py`) rather than being delegated to a
vendor helper, because the tool allow-list, approval gates, audit trail and evidence capture are
platform guarantees, not SDK features.

## Consequences

* Every AI feature is testable and demonstrable offline; the test suite covers the AI paths without
  a network call or a mocked HTTP client.
* A missing or invalid API key degrades to the heuristic provider with a warning, instead of an
  outage.
* The heuristic provider is a real fallback, not a stub: it is what `gdap demo run` uses.
* The cost: two code paths to keep honest. They are kept honest by sharing the tools, the evidence
  contract and the tests.
