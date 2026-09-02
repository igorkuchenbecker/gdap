# GDAP — System Architecture

> **GDAP (Global Data Automation Platform)** is a domain-agnostic engine that connects to data
> wherever it lives, discovers its structure, validates and cleans it, transforms and analyses it,
> explains the result with evidence, and automates the whole loop under governance.

This document is the contract between the architecture and the implementation. Every module in
`src/gdap/` maps to a box below.

---

## 1. Design principles

| # | Principle | Consequence in the code |
|---|-----------|-------------------------|
| 1 | **Ports & adapters (hexagonal)** | `core/ports.py` holds protocols; every concrete backend (DuckDB, SQLAlchemy, local FS, Anthropic) is an adapter registered at composition time in `core/container.py`. |
| 2 | **Deterministic core, probabilistic edge** | Cleaning/validation/aggregation are pure deterministic functions. ML and LLMs only *propose*; they never silently mutate data. |
| 3 | **Everything is a contract** | Pydantic models in `core/contracts.py` are the type system of the platform. Modules communicate through contracts, never through loose dicts. |
| 4 | **Metadata is a first-class product** | Every ingest/profile/run/analysis writes typed metadata + lineage. If it isn't recorded, it didn't happen. |
| 5 | **API-first** | CLI and Web UI are clients of the same service layer; the service layer never imports FastAPI or Typer. |
| 6 | **Progressive scale** | Single process today, multi-worker tomorrow, cluster later — the `JobQueue` port and columnar-on-disk storage are the seams that make that a config change, not a rewrite. |
| 7 | **Secure & auditable defaults** | Deny-by-default SQL policy, no secrets at rest in the DB, RBAC on every write endpoint, append-only audit trail. |

---

## 2. Component map

```text
┌───────────────────────────────────────────────────────────────────────────────────────┐
│                                    CLIENTS                                            │
│    Web UI (SPA)          gdap CLI (Typer)        3rd-party / automation (HTTP)        │
└───────────────┬──────────────────┬─────────────────────────┬──────────────────────────┘
                │                  │                         │
                └──────────────────┴────── same public API ───┘
                                   ▼
┌───────────────────────────────────────────────────────────────────────────────────────┐
│  API LAYER — FastAPI  (gdap.api)                    ── security boundary ──            │
│  authn (API key) → authz (RBAC) → tenant scoping → rate limit → audit → route          │
└───────────────────────────────────┬───────────────────────────────────────────────────┘
                                    ▼
┌───────────────────────────────────────────────────────────────────────────────────────┐
│  APPLICATION / SERVICE LAYER  (gdap.core.services)                                     │
│  SourceService · DatasetService · PipelineService · JobService · AnalysisService        │
│  ReportService · AlertService · GovernanceService · AgentService                        │
│  ── orchestrates domain engines, owns transactions, emits audit + lineage ──            │
└───┬──────────┬──────────┬──────────┬──────────┬──────────┬──────────┬──────────────────┘
    │          │          │          │          │          │          │
    ▼          ▼          ▼          ▼          ▼          ▼          ▼
┌────────┐┌─────────┐┌─────────┐┌─────────┐┌─────────┐┌─────────┐┌──────────────────────┐
│CONNECT ││ INGEST  ││ PROFILE ││ QUALITY ││PIPELINE ││ANALYTICS││  AI LAYER            │
│registry││ batch   ││ schema  ││ 7 dims  ││ DAG     ││descript.││  Orchestrator        │
│+plugins││ increm. ││ stats   ││ score   ││ steps   ││diagnost.││   ├ Data Agent       │
│file/sql││ chunked ││ semantic││ expect. ││ retry   ││predict. ││   ├ Quality Agent    │
│rest/obj││ resume  ││ relation││ gates   ││ approve ││anomaly  ││   ├ Analysis Agent   │
└────┬───┘└────┬────┘└────┬────┘└────┬────┘└────┬────┘└────┬────┘│   └ Reporting Agent  │
     │         │          │          │          │          │     │  tools · policy · log│
     │         │          │          │          │          │     └──────────┬───────────┘
     ▼         ▼          ▼          ▼          ▼          ▼                ▼
┌───────────────────────────────────────────────────────────────────────────────────────┐
│  CROSS-CUTTING                                                                         │
│  observability (structlog + metrics + trace ids) · security (RBAC, SQL guard, secrets)  │
│  governance (lineage, audit, catalog, policies, classification) · config · errors       │
└───────────────────────────────────┬───────────────────────────────────────────────────┘
                                    ▼
┌──────────────────────────────┐   ┌────────────────────────────┐   ┌───────────────────┐
│ OPERATIONAL METADATA         │   │ ANALYTICAL STORAGE         │   │ ARTIFACT STORE    │
│ SQLAlchemy → SQLite | Postgres│   │ Parquet + DuckDB engine    │   │ reports, charts,  │
│ orgs, users, sources, jobs,   │   │ versioned dataset files    │   │ models, exports   │
│ lineage, audit, catalog       │   │ columnar, chunk-streamed   │   │ StorageBackend    │
└──────────────────────────────┘   └────────────────────────────┘   └───────────────────┘
```

**Security boundaries** (trust drops at each `║`):

```text
internet ║ API layer (authn/authz/rate-limit) ║ service layer ║ engines ║ storage
                                              ║ AI agents (tool allow-list, SQL guard, read-only conn)
```

An agent never talks to storage directly: it calls a **tool**, the tool calls a **service**, the
service enforces policy. Tool calls are audited with arguments and results.

---

## 3. Data flow (happy path)

```text
 SOURCE ─(Connector.read → RecordBatch stream)─▶ INGEST ─(Parquet vN + checksum)─▶ DATASET VERSION
                                                    │
                                                    ├─▶ PROFILE  (schema, stats, semantics, relations)
                                                    ├─▶ QUALITY  (7 dimensions → score + findings)
                                                    ▼
 PIPELINE RUN ── step DAG ──▶ clean ▶ transform ▶ enrich ▶ aggregate ▶ analyze ▶ report ▶ alert
      │                                                                 │
      ├─ every step: metrics, lineage edge, audit event, artifacts      │
      └─ failure: retry w/ backoff → FAILED (typed error, resumable)    ▼
                                                            AI DATA ANALYST
                                                   evidence · source · query · confidence
```

**Control flow.** `POST /jobs` (or `gdap pipeline run`, or the scheduler) creates a `Job` row in
state `PENDING`. Workers lease jobs atomically (`UPDATE … WHERE state='PENDING' … RETURNING`,
`SKIP LOCKED` on Postgres), run the DAG, heartbeat a lease, and transition
`RUNNING → SUCCESS | FAILED | RETRYING | AWAITING_APPROVAL | CANCELLED`. A crashed worker's lease
expires and the job is re-leased — at-least-once execution, with idempotent step writes.

---

## 4. Repository structure

```text
gdap/
├── src/gdap/
│   ├── core/            contracts, ports, config, errors, container, services/
│   ├── connectors/      Connector protocol, registry, impl/{file,sql,rest,memory}
│   ├── ingestion/       batch/incremental engine, checkpoints, schema evolution
│   ├── profiling/       data profiler, semantic type inference, relationship discovery
│   ├── quality/         expectations, 7-dimension scoring, quality gates
│   ├── cleaning/        deterministic fixes + suggestion engine (never silent)
│   ├── pipelines/       spec model, DAG compiler, executor, steps/ registry
│   ├── analytics/       descriptive, diagnostic, anomaly, forecasting, segmentation
│   ├── ml/              model registry, training/inference abstraction, drift
│   ├── reporting/       renderers (html/pdf-ready/xlsx/csv/json), charts, templates/
│   ├── ai/              llm providers, tools/, agents/, nl2pipeline
│   ├── governance/      lineage, audit, catalog, policy engine, classification, retention
│   ├── security/        rbac, api keys, secrets resolver, sql guard, masking
│   ├── observability/   structured logging, metrics, tracing, health
│   ├── storage/         StorageBackend port + local/object adapters
│   ├── api/             FastAPI app, routers/, deps, middleware, errors
│   ├── cli/             Typer app (mirrors the API surface)
│   ├── worker/          job runner, scheduler, leasing loop
│   ├── plugins/         entry-point discovery for 3rd-party extensions
│   └── demo/            realistic multi-table demo data generator

├── tests/{unit,integration,e2e}
├── config/              default.yaml + per-environment overlays
├── deployment/          Dockerfile, docker-compose, entrypoints
├── docs/                architecture, guides, references, ADRs
└── scripts/             dev helpers, demo runner
```

> Deviation from the requested layout: engines live under one installable package (`src/gdap/…`)
> instead of top-level sibling folders. Rationale in **ADR-001** — a single distribution keeps
> imports, packaging, typing and plugin entry points coherent; the folder *names* and boundaries
> requested are preserved one level down.

---

## 5. Technology decisions (summary — full ADRs in `docs/adr/`)

| Decision | Chosen | Alternatives considered | Why |
|---|---|---|---|
| Language / runtime | Python 3.11+ | Go, JVM | Data + ML ecosystem gravity; typed with mypy strict-ish. |
| API | FastAPI | Flask, Litestar, Django | Pydantic-native contracts → OpenAPI for free, async, mature. |
| Dataframe engine | **Polars** | pandas, Dask | Arrow-backed, multithreaded, **lazy + streaming** (bounded memory), strict typing. pandas stays an optional edge format. |
| Analytical SQL | **DuckDB** | SQLite, ClickHouse, Spark | Vectorised OLAP in-process over Parquet, zero ops; the same SQL scales out later to a warehouse adapter. |
| Metadata store | **SQLAlchemy 2.0** → SQLite (dev) / Postgres (prod) | Mongo, raw SQL | Transactions + migrations; one ORM, two deployments, no code change. |
| Job execution | DB-backed queue + worker leases | Celery+Redis, Airflow, Temporal | No extra infra for the 95% case; the `JobQueue` port keeps Celery/Temporal a drop-in later (ADR-004). |
| Scheduling | croniter inside the worker | cron, APScheduler, Airflow | Schedules must be tenant-scoped rows, not OS state. |
| Reporting | Jinja2 + Plotly (self-contained HTML) + XlsxWriter | matplotlib+weasyprint | Interactive, dependency-free artifacts; HTML→PDF via headless print adapter. |
| ML | scikit-learn behind a `Model` port | PyTorch, XGBoost | Right size for tabular; the port keeps heavier engines pluggable. |
| LLM | provider-agnostic `LLMProvider`; Anthropic adapter + deterministic `HeuristicProvider` | direct SDK calls | The platform must be fully functional with **zero** AI credentials (ADR-006). |
| Config | pydantic-settings (`env` > `config/*.yaml` > defaults) | bare dotenv | Typed, validated, per-environment, no hardcoding. |

---

## 6. Data model (operational metadata)

```text
Organization ──1:N── User ──1:N── ApiKey
     │
     ├──1:N── Source ──1:N── Ingestion ──1:1── DatasetVersion
     │                                              ▲
     ├──1:N── Dataset ──1:N── DatasetVersion ───────┘
     │                            ├──1:1── Profile
     │                            └──1:N── QualityReport
     │
     ├──1:N── Pipeline ──1:N── PipelineVersion
     │             └──1:N── Job ──1:N── JobStep
     │                        ├──1:N── Report
     │                        └──1:N── Analysis
     │
     ├──1:N── AlertRule ──1:N── Alert
     ├──1:N── Model (registry, versioned)
     ├──1:N── LineageEdge   (any node → any node, stamped with job_id)
     └──1:N── AuditEvent    (append-only: actor, action, resource, result, details)
```

Every table carries `org_id`; every repository query is tenant-filtered at the session level
(`TenantScope`) — isolation is enforced in one place, not in each query.

**Analytical storage** is deliberately separate: `warehouse/{org}/{dataset}/v{n}/data.parquet`
plus a `_manifest.json` (schema, checksum, row count, lineage pointer). Metadata DB stores
*pointers*, never dataset rows.

---

## 7. API contract (v1)

```text
POST   /api/v1/sources                 register a source (config validated per connector)
POST   /api/v1/sources/{id}/test       connectivity + permission probe
GET    /api/v1/sources/{id}/discover   list objects/tables/files + inferred schemas
POST   /api/v1/sources/{id}/ingest     → 202 {job_id}  (batch|incremental)

GET    /api/v1/datasets                catalog listing (filter, search, classification)
GET    /api/v1/datasets/{id}           dataset + latest version + quality snapshot
GET    /api/v1/datasets/{id}/preview   bounded row preview (masking applied)
POST   /api/v1/datasets/{id}/profile   → profile report
POST   /api/v1/datasets/{id}/validate  → quality report + score
POST   /api/v1/datasets/{id}/query     guarded SQL (SELECT-only by default)

POST   /api/v1/pipelines               create from YAML/JSON spec (validated + versioned)
POST   /api/v1/pipelines/{id}/run      → 202 {job_id}
GET    /api/v1/jobs/{id}               state machine + steps + metrics
POST   /api/v1/jobs/{id}/cancel|approve|retry

POST   /api/v1/analyses                describe|correlate|anomaly|trend|segment|forecast
POST   /api/v1/reports                 render a report artifact
GET    /api/v1/reports/{id}/download

POST   /api/v1/agents/ask              AI Data Analyst (evidence-backed answer)
POST   /api/v1/agents/plan             natural language → reviewable pipeline spec
GET    /api/v1/lineage/{type}/{id}     upstream/downstream graph
GET    /api/v1/audit                   filtered audit trail
GET    /api/v1/health  /readyz  /metrics
```

Uniform error envelope: `{"error": {"code","message","details","trace_id"}}` with a stable
machine-readable `code` (`GDAP-XXXX`) per exception class.

---

## 8. Execution plan

| Phase | Scope | Status |
|---|---|---|
| **1 — Foundation** | structure, config, errors, logging, contracts, ports, metadata DB, API skeleton, CLI skeleton, test harness | ✅ delivered |
| **2 — Data engine** | connectors, ingestion, profiling, quality, cleaning, transformation steps | ✅ delivered |
| **3 — Analytics** | descriptive, diagnostic, anomaly, trend, charts, reporting | ✅ delivered |
| **4 — Automation** | pipeline engine, scheduler, retries, job monitoring, alerts | ✅ delivered |
| **5 — AI** | tool registry, agents, AI Data Analyst, NL→pipeline, insights | ✅ delivered |
| **6 — Enterprise** | RBAC, API keys, multi-tenancy, lineage, audit, policy engine, classification | ✅ delivered |
| **7 — Scale** | distributed workers, object storage adapter, caching, OTel exporter, warehouse pushdown | 🔜 seams in place (ports + docs) |

Phase 7 is deliberately *designed but not built*: the ports (`JobQueue`, `StorageBackend`,
`QueryEngine`) exist and are exercised by the local adapters, so scaling out is an adapter, not a
rewrite. Building it now would be complexity without a workload to justify it.

## 9. MVP definition (Definition of Done for v0.1)

`gdap demo run` must, on a clean machine, with no cloud account and no API key:
connect → ingest → version → profile → validate → clean → transform → aggregate → analyse →
detect anomalies → chart → report (HTML+XLSX) → alert → record lineage & audit → expose all of it
through the API and the CLI, with structured logs and a passing test suite.
