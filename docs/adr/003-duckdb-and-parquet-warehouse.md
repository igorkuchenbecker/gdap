# ADR-003 — DuckDB over versioned Parquet as the analytical store

**Status:** Accepted

## Problem

The platform needs an analytical query surface (`SELECT … GROUP BY …` over datasets) and a place to
put dataset bytes, without requiring anyone to run a warehouse to try it.

## Options

1. **Store rows in the metadata database (SQLite/Postgres)** — one system, but it conflates
   operational metadata with analytical data (§30) and collapses under columnar workloads.
2. **Parquet files + DuckDB** — columnar on disk, vectorised OLAP in-process, zero operations.
3. **A real warehouse (ClickHouse, BigQuery, Snowflake)** — the right answer at scale, an absurd
   prerequisite for `pip install`.

## Decision

Option 2: each dataset version is an immutable Parquet file plus a manifest; DuckDB queries those
files directly as views.

## Consequences

* **Immutability buys reproducibility.** A job result can always be traced to the exact bytes it
  read, and rollback is "point at v3 again".
* Predicate and projection pushdown means a `WHERE` clause never materialises the whole file.
* Queries never touch user-supplied file functions — `read_csv`, `ATTACH` and friends are blocked
  by the SQL guard, so the engine cannot be turned into a file reader.
* Growing past one node means implementing `QueryEngine` against a warehouse; the port already
  isolates every call site.
