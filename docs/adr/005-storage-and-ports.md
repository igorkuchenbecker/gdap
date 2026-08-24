# ADR-005 — Ports and adapters for storage, queue and query engine

**Status:** Accepted

## Problem

The platform must run unchanged on a laptop (local filesystem, SQLite, in-process worker) and in a
cluster (object storage, PostgreSQL, many workers) — without a rewrite, and without pretending to
support what has not been built.

## Decision

Every infrastructure dependency sits behind a `Protocol` in `core/ports.py`
(`StorageBackend`, `QueryEngine`, `JobQueue`, `LLMProvider`, `SecretResolver`, `ModelBackend`,
`NotificationChannel`). Concrete adapters are bound in `core/container.py` and nowhere else.

## Consequences

* Tests inject a temporary local storage and an in-memory database; no mocking framework is needed.
* Adding S3 support means writing one adapter, not touching thirty call sites.
* **Unimplemented adapters fail loudly rather than silently.** `ObjectStorageBackend` raises with an
  actionable message instead of pretending to work (§63) — a fake implementation is worse than a
  missing one, because it fails in production instead of at configuration time.
