# API reference

Generated from the live OpenAPI schema (`GET /openapi.json`; interactive docs at `/docs`).

## Conventions

* **Authentication** — `X-API-Key: gdap_…` or `Authorization: Bearer gdap_…`.
* **Errors** — always `{"error": {"code", "message", "details", "trace_id"}}`, with a stable
  `GDAP-XXXX` code. See [TROUBLESHOOTING.md](TROUBLESHOOTING.md) for the code ranges.
* **Tracing** — every response carries `X-Request-ID` and `X-Response-Time-ms`; send your own
  `X-Request-ID` to correlate with your systems.
* **Pagination** — `?limit=&offset=` on list endpoints; responses are `{"items": [...], "count": n}`.
* **Tenancy** — the API key determines the organisation; there is no tenant parameter to forge.


## system

| method | path | does |
|---|---|---|
| `GET` | `/api/v1/admin/api-keys` | List API keys (never the secrets) |
| `POST` | `/api/v1/admin/api-keys` | Issue an API key (shown once) |
| `DELETE` | `/api/v1/admin/api-keys/{key_id}` | Revoke an API key |
| `GET` | `/api/v1/system/connectors` | Connector catalogue with config schemas |
| `GET` | `/api/v1/system/dashboard` | Aggregate view for the home screen |
| `GET` | `/api/v1/system/doctor` | Full self-diagnostic |
| `GET` | `/api/v1/system/info` | Platform capabilities |
| `GET` | `/health` | Liveness and (optionally) deep health checks |
| `GET` | `/metrics` | In-process metrics snapshot |
| `GET` | `/readyz` | Readiness probe |

## sources

| method | path | does |
|---|---|---|
| `GET` | `/api/v1/sources` | List registered sources |
| `POST` | `/api/v1/sources` | Register a source |
| `DELETE` | `/api/v1/sources/{reference}` | Delete a source |
| `GET` | `/api/v1/sources/{reference}` | Get one source |
| `GET` | `/api/v1/sources/{reference}/discover` | List objects available in the source |
| `POST` | `/api/v1/sources/{reference}/ingest` | Ingest data from the source into a dataset |
| `POST` | `/api/v1/sources/{reference}/test` | Probe connectivity and permissions |

## datasets

| method | path | does |
|---|---|---|
| `GET` | `/api/v1/datasets` | List datasets in the catalog |
| `POST` | `/api/v1/datasets/query` | Run guarded SQL across datasets |
| `GET` | `/api/v1/datasets/{reference}` | Get a dataset with its latest version |
| `POST` | `/api/v1/datasets/{reference}/cleaning` | Propose (and optionally apply) cleaning fixes |
| `GET` | `/api/v1/datasets/{reference}/preview` | Preview rows (masking applied) |
| `GET` | `/api/v1/datasets/{reference}/profile` | Get the most recent stored profile |
| `POST` | `/api/v1/datasets/{reference}/profile` | Profile a dataset version |
| `GET` | `/api/v1/datasets/{reference}/schema` | Get the schema of a version |
| `POST` | `/api/v1/datasets/{reference}/validate` | Evaluate data quality |
| `GET` | `/api/v1/datasets/{reference}/versions` | List dataset versions |
| `DELETE` | `/api/v1/datasets/{reference}/versions/{version}` | Delete one dataset version |

## pipelines

| method | path | does |
|---|---|---|
| `GET` | `/api/v1/pipelines` | List pipelines |
| `POST` | `/api/v1/pipelines` | Create a pipeline |
| `GET` | `/api/v1/pipelines/steps` | Catalogue of available pipeline steps |
| `DELETE` | `/api/v1/pipelines/{reference}` | Delete a pipeline |
| `GET` | `/api/v1/pipelines/{reference}` | Get a pipeline |
| `PUT` | `/api/v1/pipelines/{reference}` | Publish a new pipeline version |
| `POST` | `/api/v1/pipelines/{reference}/enable` | Enable or disable a pipeline |
| `POST` | `/api/v1/pipelines/{reference}/run` | Queue a pipeline run |
| `GET` | `/api/v1/pipelines/{reference}/versions` | List published pipeline versions |
| `GET` | `/api/v1/pipelines/{reference}/yaml` | Get the pipeline as YAML |

## jobs

| method | path | does |
|---|---|---|
| `GET` | `/api/v1/jobs` | List jobs |
| `GET` | `/api/v1/jobs/{job_id}` | Get a job with its steps |
| `POST` | `/api/v1/jobs/{job_id}/approve` | Approve blocked steps and resume |
| `POST` | `/api/v1/jobs/{job_id}/cancel` | Cancel a job |
| `POST` | `/api/v1/jobs/{job_id}/execute` | Run a queued job inline (no worker required) |
| `POST` | `/api/v1/jobs/{job_id}/reject` | Reject a pending approval |
| `POST` | `/api/v1/jobs/{job_id}/retry` | Re-queue a failed job |

## analyses

| method | path | does |
|---|---|---|
| `GET` | `/api/v1/analyses` | List stored analyses |
| `POST` | `/api/v1/analyses` | Run an analysis |
| `POST` | `/api/v1/analyses/auto` | Run every analysis that applies to a dataset |
| `GET` | `/api/v1/analyses/insights/{dataset}` | Recent insights for a dataset |
| `GET` | `/api/v1/analyses/kinds` | Analyses the platform can run |
| `GET` | `/api/v1/analyses/{analysis_id}` | Get a stored analysis |

## reports

| method | path | does |
|---|---|---|
| `GET` | `/api/v1/reports` | List report artifacts |
| `POST` | `/api/v1/reports` | Generate a dataset report |
| `GET` | `/api/v1/reports/{report_id}` | Get report metadata |
| `GET` | `/api/v1/reports/{report_id}/download` | Download the artifact |
| `GET` | `/api/v1/reports/{report_id}/view` | View an HTML report inline |

## ai

| method | path | does |
|---|---|---|
| `GET` | `/api/v1/agents` | Available agents and their tool grants |
| `POST` | `/api/v1/agents/ask` | Ask the AI Data Analyst a question about the data |
| `POST` | `/api/v1/agents/plan` | Turn a natural-language request into a reviewable pipeline |
| `GET` | `/api/v1/agents/tools` | Tool catalogue with permissions and approval modes |

## alerts

| method | path | does |
|---|---|---|
| `GET` | `/api/v1/alerts` | List alerts |
| `POST` | `/api/v1/alerts/{alert_id}/acknowledge` | Acknowledge an alert |

## governance

| method | path | does |
|---|---|---|
| `GET` | `/api/v1/audit` | Query the audit trail |
| `GET` | `/api/v1/catalog` | Data catalog with ownership and classification |
| `GET` | `/api/v1/classification` | Datasets grouped by classification level |
| `GET` | `/api/v1/lineage/{node_type}/{node_id}` | Lineage graph around a node |
| `GET` | `/api/v1/retention/candidates` | Dataset versions past their retention window |


## Examples

```bash
KEY=$(gdap system key create ops --role admin | grep -o 'gdap_[^ ]*')
BASE=http://localhost:8000/api/v1

# register a source and ingest from it
curl -X POST $BASE/sources -H "X-API-Key: $KEY" -H 'Content-Type: application/json' -d '{
  "name": "sales_files", "type": "file", "connector": "file.csv",
  "config": {"path": "/data/sales", "pattern": "*.csv"}
}'
curl -X POST $BASE/sources/sales_files/ingest -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"object": "transactions.csv", "dataset": "transactions"}'

# validate, query, analyse
curl -X POST $BASE/datasets/transactions/validate -H "X-API-Key: $KEY" -d '{"auto_expectations": true}' -H 'Content-Type: application/json'
curl -X POST $BASE/datasets/query -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"sql": "SELECT region, sum(revenue) AS revenue FROM transactions GROUP BY region"}'

# run a pipeline and follow the job
JOB=$(curl -s -X POST $BASE/pipelines/sales_daily/run -H "X-API-Key: $KEY" -d '{}' -H 'Content-Type: application/json' | jq -r .job_id)
curl -s $BASE/jobs/$JOB -H "X-API-Key: $KEY" | jq '.state, .steps[].step_id'

# ask the analyst
curl -X POST $BASE/agents/ask -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"question": "why did revenue fall last month?", "dataset": "transactions"}'
```

## Asynchronous operations

`POST /sources/{name}/ingest` with `{"async": true}` and `POST /pipelines/{name}/run` return
`202` with a `job_id`. Poll `GET /jobs/{id}` for state, steps, metrics and artifacts. A worker must
be running (`gdap worker start`), or execute the job inline with `POST /jobs/{id}/execute`.
