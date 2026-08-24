# ADR-004 — A database-backed job queue instead of Celery, Airflow or Temporal

**Status:** Accepted

## Problem

Pipelines must run asynchronously, survive worker crashes, retry with backoff, and be observable.

## Options

| Option | Cost | Benefit |
|---|---|---|
| **Celery + Redis/RabbitMQ** | Two more services to run, monitor and back up | Mature, horizontal |
| **Airflow** | A platform in its own right; DAGs live outside our model | Rich scheduling ecosystem |
| **Temporal** | Excellent durability semantics | A cluster, a new programming model |
| **A `jobs` table with atomic leasing** | Hand-written leasing; throughput bounded by the database | Zero extra infrastructure; jobs are queryable rows; one backup covers everything |

## Decision

A `jobs` table leased with a conditional `UPDATE` (`SKIP LOCKED` on PostgreSQL), a lease deadline,
and a heartbeat. Workers are plain processes; running more of them *is* the scaling story.

## Consequences

* **At-least-once execution.** A crashed worker's lease expires and another worker re-runs the job,
  which is why dataset writes create new versions instead of mutating in place.
* Job state, steps, metrics and errors are rows: the monitoring UI is a `SELECT`, not an add-on.
* Throughput is bounded by the metadata database — fine for thousands of jobs/day, not for
  millions of tiny messages. `JobQueue` in `core/ports.py` is the seam for swapping in a broker.
