# ADR-002 — Polars as the in-memory frame engine

**Status:** Accepted

## Problem

Every engine in the platform passes tabular data around. The choice sets the ceiling on memory
behaviour, type safety and throughput.

## Options

| Option | For | Against |
|---|---|---|
| **pandas** | Ubiquitous; every data person knows it | Single-threaded, eager, high memory overhead, silent dtype coercion (`NaN` for missing integers), no lazy pushdown |
| **Polars** | Arrow-backed, multithreaded, lazy + streaming, strict nullability, predicate/projection pushdown into Parquet | Smaller ecosystem; some analysts must learn a new API |
| **Dask / Spark** | Scales past one machine | Cluster complexity for datasets that overwhelmingly fit on one node |

## Decision

Polars, with pandas accepted only at the edges (a user's notebook, an optional export).

## Consequences

* Bounded memory is achievable: `scan_parquet` + `slice` streams a dataset larger than RAM.
* Nullability is explicit, so "missing" never silently becomes `NaN` and corrupts an aggregate.
* Chunked ingestion, profiling on a sample and lazy reads are natural rather than bolted on.
* The cost is real: contributors used to pandas need the Polars expression API, and a few
  third-party libraries need an Arrow conversion at the boundary.
