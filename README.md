# GDAP — Global Data Automation Platform

Connect to data anywhere, discover what it means, validate and clean it, transform and analyse it,
explain the result with evidence, and automate the whole loop under governance.

GDAP is **domain-agnostic**: it profiles the data it is given and adapts, instead of hardcoding
rules for one dataset. It runs on a laptop with SQLite and Parquet, and on a cluster with
PostgreSQL, object storage and multiple workers — the same code, different adapters.

```text
SOURCE → CONNECT → INGEST → DISCOVER → PROFILE → VALIDATE → CLEAN → NORMALIZE → TRANSFORM
       → ENRICH → ANALYZE → DETECT → PREDICT → EXPLAIN → VISUALIZE → REPORT → ALERT → ACT → AUDIT
```

---

## Quick start

```bash
git clone <this-repo> && cd gdap
uv venv --python 3.13 && uv pip install -e ".[dev]"     # or: pip install -e ".[dev]"

gdap system init          # create the schema and the default organisation
gdap demo run             # generate data and run the entire loop end to end
gdap system serve         # API + web UI on http://127.0.0.1:8000
```

`gdap demo run` takes a clean machine — **no cloud account, no API key** — from nothing to:
data generated with realistic defects, ingested and versioned, profiled, validated, cleaned,
transformed, analysed, explained, reported (HTML + XLSX), alerted on, and fully audited. It
finishes in a couple of seconds and prints what it found.

### Your own data, in four commands

```bash
gdap source add sales --connector file.csv --set path=/data/sales --set pattern='*.csv'
gdap source ingest sales --object sales_2026.csv --dataset sales
gdap dataset validate sales
gdap agent ask "why did revenue fall last month?" --dataset sales
```

---

## What it does

| Capability | What that means in practice |
|---|---|
| **Connect** | CSV/TSV, JSON/NDJSON, Parquet, XML, Excel, gzip/zip; PostgreSQL, MySQL, SQL Server, Oracle, SQLite; REST APIs with pagination, auth and retries. New connectors are plugins, not core changes. |
| **Ingest** | Chunked and streaming — dataset size is bounded by disk, not RAM. Full, incremental (high-water mark) and append modes, with checkpoints, deduplication and schema-evolution detection. |
| **Discover** | Profiles every column: distribution, cardinality, missing values, outliers, candidate keys, cross-dataset relationships, and **semantic type** (currency, e-mail, identifier, …) which drives masking, charts and analysis. |
| **Validate** | Seven quality dimensions (completeness, validity, uniqueness, consistency, accuracy, timeliness, integrity) scored 0–100, plus declarative expectations. Quality gates can stop a pipeline. |
| **Clean** | Proposes fixes with a rationale, an affected-row count and an approval level. Deterministic fixes apply automatically; destructive ones wait for a human. Nothing is ever changed silently. |
| **Transform** | Declarative YAML pipelines with a **safe expression language** (no `eval`, no imports, no attribute access — see [docs/PIPELINES.md](docs/PIPELINES.md)). |
| **Analyse** | Descriptive, correlation, segmentation, period comparison, driver analysis (η²), trend, forecasting with prediction intervals, and four anomaly-detection methods. |
| **Explain** | The AI Data Analyst answers questions with **evidence attached** — source, query, calculation, row count — and labels every statement as fact, inference, hypothesis or recommendation. |
| **Automate** | Job state machine with retries and backoff, cron/interval schedules with timezones, pipeline dependencies, approval gates, alerting with deduplication. |
| **Govern** | Multi-tenant isolation, RBAC, API keys, audit trail, lineage graph, automatic data classification, masking, retention reporting, SQL safety layer. |

---

## The AI layer works without an API key

The platform ships two interchangeable providers behind one port:

* **`heuristic`** (default) — no network, no credentials, no model. Questions are routed to the
  right tool deterministically and answers are assembled *only* from tool results.
* **`anthropic`** — the Claude Messages API with tool use, for natural-language reasoning.

```bash
export GDAP_AI__PROVIDER=anthropic
export ANTHROPIC_API_KEY=sk-ant-…       # missing key ⇒ automatic, logged fallback to heuristic
```

Either way the rules are the same and enforced in code, not in a prompt: an agent can only call
tools it was granted, every call is audited, SQL runs under the strictest policy, outward-facing
tools (`send_alert`, `schedule_pipeline`) require human approval, and **no claim is made without
evidence**. See [docs/AI.md](docs/AI.md).

---

## Interfaces

Everything is available three ways, over one service layer:

```bash
gdap dataset validate sales --json          # CLI (scriptable, --json everywhere)
curl -X POST localhost:8000/api/v1/datasets/sales/validate -H "X-API-Key: $KEY"   # API
open http://localhost:8000                  # Web UI (a client of the same API)
```

The OpenAPI schema is at `/openapi.json`, interactive docs at `/docs`.

---

## Example pipeline

```yaml
name: sales_daily
schedule: { cron: "0 6 * * *", timezone: UTC }
quality_gate: 60

steps:
  - { id: ingest,   uses: read.source,  with: { source: sales_files, object: transactions.csv, dataset: transactions } }
  - { id: clean,    uses: clean.auto,   with: { apply: validated } }
  - { id: revenue,  uses: transform.calculate, with: { calculate: { net: "revenue * (1 - discount_pct)" } } }
  - { id: validate, uses: validate.expectations, with: { auto: true } }
  - { id: monthly,  uses: aggregate, output: monthly,
      with: { group_by: [region], metrics: { revenue: "sum(net)", orders: count } } }
  - { id: publish,  uses: write.dataset, with: { dataset: sales_monthly } }
  - { id: trend,    uses: analyze.trend, input: transactions,
      with: { metric: net, time_column: order_date, granularity: month } }
  - { id: report,   uses: report.generate, with: { title: "Daily sales review", formats: [html, xlsx] } }
  - { id: alert,    uses: alert.threshold,
      with: { metric: quality_score, operator: lt, threshold: 85, severity: warning } }
```

```bash
gdap pipeline create examples/pipelines/sales_daily.yaml
gdap pipeline run sales_daily
gdap worker start        # or let the scheduler fire it at 06:00
```

There are **35 step types** (`gdap pipeline steps`) and you can add your own through the
`gdap.pipeline_steps` entry point.

---

## Documentation

| Guide | Contents |
|---|---|
| [Architecture](docs/ARCHITECTURE.md) | Components, data flow, security boundaries, technology decisions, data model, API contracts |
| [Installation](docs/INSTALLATION.md) | Local install, extras, first run |
| [Configuration](docs/CONFIGURATION.md) | Every setting, precedence, environments, secrets |
| [Pipelines](docs/PIPELINES.md) | Spec reference, all step types, the expression language |
| [AI layer](docs/AI.md) | Providers, agents, tools, safety rules, NL→pipeline |
| [Security](docs/SECURITY.md) | AuthN/Z, RBAC, secrets, SQL safety, masking, threat model |
| [Governance](docs/GOVERNANCE.md) | Lineage, audit, classification, retention, approvals |
| [Deployment](docs/DEPLOYMENT.md) | Docker, Postgres, workers, migrations, backups, scaling |
| [Development](docs/DEVELOPMENT.md) | Project layout, testing, writing plugins |
| [Troubleshooting](docs/TROUBLESHOOTING.md) | Symptoms, causes, fixes |
| [ADRs](docs/adr/) | Why each significant decision was made |

---

## Project status

Phases 1–6 of the roadmap are implemented and tested; phase 7 (distributed execution, object
storage, OTel export) is *designed* — the ports exist and the local adapters exercise them — but
deliberately not built until a workload justifies it.

```bash
pytest                    # 158 tests: unit, integration, end-to-end
ruff check src tests      # lint
mypy                      # types
gdap doctor               # runtime self-diagnostic
```

## Licence

Apache-2.0.
