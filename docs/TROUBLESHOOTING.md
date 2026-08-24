# Troubleshooting

Start here, always:

```bash
gdap doctor          # database, storage, connectors, engine, AI runtime, scheduler
gdap system info     # what configuration is actually in effect
```

Every API error carries a `trace_id`; every log line inside that request carries the same one.

```bash
grep <trace-id> /var/log/gdap.log
```

## Common situations

### `GDAP-3000 missing API key`
Auth is on (the default outside `testing`). Issue a key and send it:

```bash
gdap system key create local --role admin
curl -H "X-API-Key: gdap_…" localhost:8000/api/v1/datasets
```

### `GDAP-3003 … blocked by the SQL safety layer`
Working as designed. `DROP`, `COPY`, `ATTACH`, `read_csv()` and friends are never allowed through
the query API. For writes, both `security.sql_write_enabled` **and** the `sql:write` permission are
required.

### `GDAP-4101 breaking schema change detected`
A column was removed or retyped upstream. Inspect the diff, then either fix the source or accept it:

```bash
gdap dataset show <name> --json | jq .schema
gdap source ingest <source> --dataset <name>   # with ingestion.allow_schema_evolution=true
```

### `GDAP-4300 quality gate failed`
The pipeline stopped on purpose. See what failed and decide:

```bash
gdap dataset validate <name>          # dimensions and findings
gdap dataset clean <name>             # proposals, with approval levels
gdap dataset clean <name> --apply --approve fix-004
```

### A job is stuck in `AWAITING_APPROVAL`
A step needs a human (§38).

```bash
gdap job show <job-id>                            # which steps, and why
gdap job approve <job-id> --note "reviewed"
```

### A job is stuck in `RUNNING`
Its worker probably died. The lease (`worker.lease_seconds`, default 300s) expires and another
worker re-runs it. If no worker is running, start one:

```bash
gdap worker start          # or: gdap worker drain   (run queued jobs once and exit)
```

### Schedules never fire
Something must be running the scheduler:

```bash
gdap worker schedule       # what is due
gdap worker start          # scheduler enabled by default
```

Check `enabled` and `next_run_at` in `gdap pipeline list`.

### The AI answers "no language model is configured"
That is the deterministic provider working as intended. For natural-language reasoning:

```bash
export GDAP_AI__PROVIDER=anthropic GDAP_AI__MODEL=claude-opus-5
export ANTHROPIC_API_KEY=sk-ant-…
gdap agent tools           # confirms mode: llm
```

A missing key silently falls back — the warning is in the logs (`llm_credentials_missing`).

### The AI measured the wrong column
It reports what it chose (`columns chosen automatically: metric=…`). Be explicit:

```bash
gdap analysis run transactions trend -p metric=net_revenue -p time_column=order_date
```

If the semantic type is wrong, profile the dataset — meanings are inferred at ingestion from a
sample and refreshed by `gdap dataset profile`.

### Ingestion runs out of memory
Lower the chunk size; ingestion streams, but a single chunk must fit:

```bash
GDAP_INGESTION__CHUNK_ROWS=50000 gdap source ingest big_source --dataset big
```

JSON and XML are parsed whole before slicing — convert very large files to CSV/Parquet upstream.

### `database is locked` (SQLite)
Several processes are writing to one SQLite file. That is what PostgreSQL is for:

```bash
GDAP_DATABASE__URL=postgresql+psycopg://gdap:***@localhost/gdap gdap system init
```

### Reports are large
A self-contained HTML report inlines the chart library (~4 MB). For intranet use where a CDN is
reachable, render with `HtmlRenderer("cdn")`, or export XLSX/CSV instead.

### PDF export fails
`UnsupportedOperationError: PDF rendering requires WeasyPrint`. Install it (`pip install
weasyprint`) or use HTML/XLSX. The platform refuses to emit a fake PDF.

## Error codes

| Range | Area |
|---|---|
| `GDAP-1xxx` | configuration and plugins |
| `GDAP-2xxx` | request/resource (not found, conflict, validation) |
| `GDAP-3xxx` | security (authn, authz, policy, SQL safety, rate limit, approval) |
| `GDAP-4xxx` | data plane (connector, ingestion, storage, quality) |
| `GDAP-5xxx` | orchestration (pipeline, step, job) |
| `GDAP-6xxx` | AI layer |
| `GDAP-7xxx` | models |
