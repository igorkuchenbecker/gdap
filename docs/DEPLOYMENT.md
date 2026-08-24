# Deployment

## Shape

```text
            ┌───────────┐        ┌──────────────┐
  clients ─▶│  API (n)  │───────▶│  PostgreSQL  │◀──── metadata, jobs, audit, lineage
            └───────────┘        └──────────────┘
                  │                     ▲
                  ▼                     │
            ┌───────────┐               │
            │ Worker(n) │───────────────┘   leases jobs, runs pipelines, fires schedules
            └───────────┘
                  │
                  ▼
            ┌───────────────────────────┐
            │ shared volume / object     │  warehouse (Parquet) + artifacts (reports)
            │ storage                    │
            └───────────────────────────┘
```

API processes are stateless. Workers are the unit of scale: run more of them.
**At least one worker must have the scheduler enabled.**

## Docker Compose

```bash
export POSTGRES_PASSWORD=$(openssl rand -hex 24)
docker compose -f deployment/docker-compose.yml up --build
```

Brings up PostgreSQL, runs `alembic upgrade head`, then starts the API (`:8000`) and a worker,
sharing one data volume.

## Configuration for production

```bash
GDAP_ENVIRONMENT=production                     # forces auth on, docs off, JSON logs
GDAP_DATABASE__URL=postgresql+psycopg://gdap:***@db:5432/gdap
GDAP_HOME=/data
GDAP_API__CORS_ORIGINS='["https://gdap.example.com"]'
GDAP_WORKER__CONCURRENCY=8
```

Then issue the first key:

```bash
docker compose exec api gdap system init
docker compose exec api gdap system key create ops --role admin
```

## Migrations

Schema changes ship as Alembic revisions; `create_all()` is for development only.

```bash
alembic upgrade head          # apply
alembic downgrade -1          # roll back one revision
alembic revision --autogenerate -m "add x"    # author a new one
```

Run migrations as a separate step *before* rolling the API and workers (the compose file does this
with a `migrate` service and `service_completed_successfully`).

## Upgrade and rollback

1. `alembic upgrade head` (backwards-compatible revisions first).
2. Roll API instances, then workers — mixed versions are safe for one minor release because jobs
   carry the pipeline spec they were created with.
3. To roll back: deploy the previous image, then `alembic downgrade <revision>` **only** if the new
   revision was not backwards-compatible.

## Backups

Two things must be backed up together:

| What | Where | How |
|---|---|---|
| Metadata | PostgreSQL | `pg_dump` (or managed snapshots) |
| Warehouse + artifacts | `$GDAP_HOME` | Filesystem/volume snapshot or object-store versioning |

They reference each other: metadata holds pointers (URIs, checksums) to warehouse files. A restore
that mixes eras leaves dangling pointers — the checksum in each version row is how you detect it.

## Health and monitoring

| Endpoint | Use |
|---|---|
| `/health` | Liveness; `?deep=true` adds engine, AI runtime and scheduler checks |
| `/readyz` | Readiness — fails while the database is unreachable |
| `/metrics` | Counters, gauges and latency percentiles (JSON) |

Logs are structured; set `GDAP_OBSERVABILITY__LOG_FORMAT=json` and ship them. Every line inside a
request or job carries `trace_id`, `org_id` and `job_id`.

```bash
gdap doctor                 # the same checks from the command line
gdap worker schedule        # what is scheduled next
gdap job list --state FAILED
```

## Scaling

| Symptom | Move |
|---|---|
| Jobs queue up | More worker processes (or `--concurrency`) |
| API latency under load | More API replicas behind a load balancer |
| Ingestion is memory-hungry | Lower `ingestion.chunk_rows` |
| Queries are slow | Fewer, wider datasets; project columns; consider a warehouse adapter |
| Metadata contention | PostgreSQL (not SQLite), tune `pool_size` |

SQLite is fine for a single node; use PostgreSQL as soon as more than one process writes.

## Hardening checklist

- [ ] `GDAP_ENVIRONMENT=production` (auth on, docs off, JSON logs)
- [ ] TLS terminated at the ingress; rate limiting at the edge
- [ ] Secrets injected as `env:` / `file:` references, never inline
- [ ] Container runs as the non-root `gdap` user (the shipped image does)
- [ ] `$GDAP_HOME` on an encrypted volume
- [ ] Backups tested by restoring, not by existing
- [ ] `sql_write_enabled` / `sql_destructive_enabled` left `false` unless a workload requires them
