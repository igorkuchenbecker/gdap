# Configuration

Nothing operational is hardcoded, and no secret is ever stored in configuration — only references
to secrets (§15, §29).

## Precedence

Highest wins:

```text
explicit arguments  →  environment variables  →  .env  →  config/<environment>.yaml  →  config/default.yaml  →  field defaults
```

The environment is chosen by `GDAP_ENVIRONMENT` (`development` · `testing` · `staging` ·
`production`) and selects the overlay file.

## Environment variables

Every setting is reachable with the `GDAP_` prefix and `__` for nesting:

```bash
GDAP_ENVIRONMENT=production
GDAP_API__PORT=9000
GDAP_DATABASE__URL=postgresql+psycopg://gdap:***@db:5432/gdap
GDAP_SECURITY__AUTH_ENABLED=true
GDAP_OBSERVABILITY__LOG_FORMAT=json
GDAP_AI__PROVIDER=anthropic
GDAP_HOME=/var/lib/gdap            # where everything the platform writes lives
```

## Settings reference

### `paths`
| Setting | Default | Purpose |
|---|---|---|
| `home` | `~/.gdap` (or `$GDAP_HOME`) | Root of everything the platform writes |
| `warehouse` | `<home>/warehouse` | Versioned dataset files (Parquet) |
| `artifacts` | `<home>/artifacts` | Reports and exports |
| `models` | `<home>/models` | Serialised ML models |
| `staging` | `<home>/staging` | In-flight ingestion buffers |

### `database`
| Setting | Default | Notes |
|---|---|---|
| `url` | `sqlite:///<home>/gdap.db` | Use `postgresql+psycopg://…` in production |
| `pool_size` / `max_overflow` | 5 / 10 | PostgreSQL only |
| `pool_pre_ping` | `true` | Survives database restarts and idle timeouts |

### `api`
| Setting | Default | Notes |
|---|---|---|
| `host` / `port` | `127.0.0.1` / `8000` | `0.0.0.0` in the production overlay |
| `docs_enabled` | `true` (`false` in production) | Serves `/docs` and `/openapi.json` |
| `cors_origins` | `["http://localhost:5173"]` | Empty in production — set it explicitly |
| `rate_limit_per_minute` | 240 | Per API key; a per-node guard, not an edge limiter |
| `serve_web_ui` | `true` | Serves `web/` at `/` |

### `security`
| Setting | Default | Notes |
|---|---|---|
| `auth_enabled` | `true` | Forced `true` by the production overlay |
| `api_key_header` | `X-API-Key` | `Authorization: Bearer …` also works |
| `sql_write_enabled` | `false` | Must be `true` *and* the caller must hold `sql:write` |
| `sql_destructive_enabled` | `false` | Same, for `sql:destructive` |
| `sql_statement_timeout_s` | 30 | Enforced by interrupting the query |
| `sql_max_rows` | 100000 | Injected as a `LIMIT` when the query has none |
| `mask_restricted_columns` | `true` | Masks on the way out; stored data is untouched |
| `secret_backend` | `env` | `env` or `file` |

### `ingestion`
| Setting | Default | Notes |
|---|---|---|
| `chunk_rows` | 250000 | The memory/throughput dial |
| `max_retries` / `retry_backoff_s` | 3 / 2.0 | Transient connector failures only |
| `infer_schema_rows` | 10000 | Sample size for type and semantic inference |
| `allow_schema_evolution` | `true` | Additive changes always allowed; this permits breaking ones |

### `quality`
| Setting | Default | Notes |
|---|---|---|
| `fail_below_score` / `warn_below_score` | 60 / 85 | Status thresholds |
| `weights` | see below | Re-normalised automatically if they do not sum to 1 |

```yaml
quality:
  weights:
    completeness: 0.25
    validity: 0.20
    uniqueness: 0.15
    consistency: 0.15
    accuracy: 0.10
    timeliness: 0.10
    integrity: 0.05
```

### `worker`
| Setting | Default | Notes |
|---|---|---|
| `concurrency` | 2 | Threads per worker process |
| `lease_seconds` / `heartbeat_seconds` | 300 / 30 | A lost heartbeat means the job is re-leased |
| `scheduler_enabled` | `true` | Run at least one worker with the scheduler on |
| `max_job_runtime_s` | 3600 | Graceful-shutdown budget |

### `ai`
| Setting | Default | Notes |
|---|---|---|
| `provider` | `heuristic` | `heuristic` (no credentials) or `anthropic` |
| `model` | `claude-opus-5` | Ignored by the heuristic provider |
| `api_key_ref` | `env:ANTHROPIC_API_KEY` | A *reference*, never a value |
| `thinking` / `effort` | `adaptive` / `high` | Reasoning depth for the LLM provider |
| `max_tool_iterations` | 8 | Hard bound on an agent loop |

### `locale`
No implicit `USD` / `en-US` / `UTC-3` anywhere (§43):

```yaml
locale:
  default_locale: pt_BR
  default_timezone: America/Sao_Paulo
  default_currency: BRL
```

## Secrets

Secrets are referenced and resolved at the last moment:

```yaml
# a source definition
secret_refs:
  password: env:PGPASSWORD          # environment variable
  token:    file:/run/secrets/api   # file (Docker/Kubernetes secret mounts)
```

* `literal:` values are refused in production.
* A source's config is validated on creation and rejects inline secrets outright.
* API responses return secret *names*, never values; logs are redacted.

## Inspecting the effective configuration

```bash
gdap system info            # resolved settings and capabilities
gdap system info --json     # same, machine-readable
gdap doctor                 # database, storage, connectors, engine, AI, scheduler
```
