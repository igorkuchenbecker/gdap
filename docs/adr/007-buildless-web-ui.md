# ADR-007 — A build-free web UI as an API client

**Status:** Accepted · **Supersedes** the default stack's "TypeScript + React" suggestion for v1.

## Problem

The platform needs a usable UI. The default stack proposes React + TypeScript, which implies npm,
a bundler, a lockfile, a build step in CI, and a second deployment artifact.

## Decision

Ship the UI as static files (`web/index.html`, one ES module, one stylesheet) served by the API,
with **no build step**. It is a thin client of the public API: it holds no business logic and
calls exactly the endpoints third parties call.

## Rationale

* The UI is a *thin* client. Its job is tables, forms and one AI panel — a build pipeline would
  cost more than it returns at this size.
* Zero build means `pip install gdap && gdap system serve` gives a working UI, which matters for
  operators and for the demo.
* API-first (§32) is enforced structurally: if a screen needs something, the API must expose it,
  because there is no other place to put it.

## Consequences

* No component library, no type checking in the UI, no hot reload. Accepted at this size.
* **When the UI grows past forms and tables** — charts, drag-and-drop pipeline editing, real-time
  job streaming — replace it with a React/TypeScript app. Nothing else changes, because it already
  consumes only the public API. The endpoints are the contract; the UI is disposable.
