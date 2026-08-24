# Development

## Layout

```text
src/gdap/
  core/         contracts, ports, config, errors, container, services/   ← the application layer
  connectors/   Connector protocol + registry + implementations
  ingestion/    chunked/incremental loading, checkpoints, schema evolution
  profiling/    profiler + semantic inference + relationship discovery
  quality/      expectations + seven-dimension scoring + gates
  cleaning/     proposals and their guarded application
  pipelines/    spec, expressions, executor, steps/, schedule
  analytics/    descriptive, diagnostic, anomaly, trend/forecast
  ml/           model registry and backends
  reporting/    builder, charts, renderers, templates
  ai/           providers, tools/, agents/, nl2pipeline
  governance/   audit, lineage, classification, policy
  security/     rbac, api keys, secrets, sql guard, masking
  storage/      database, models, repositories, warehouse, query, backends
  observability/logging, metrics, health, notifications
  api/ cli/ worker/ demo/
```

## Rules that keep it coherent

1. **Services never import FastAPI or Typer.** Interfaces are clients of the service layer.
2. **Repositories are the only place that writes SQL** for metadata, and they are tenant-scoped.
3. **Infrastructure is reached through `core/ports.py`**, bound in `core/container.py`.
4. **Contracts, not dicts**, between modules (`core/contracts.py`).
5. **Nothing destructive happens silently** — propose, gate, audit.

## Testing

```bash
pytest -q                          # everything (158 tests, ~10s)
pytest tests/unit -q               # pure logic
pytest -m integration              # database, files, HTTP, worker
pytest -m e2e                      # the MVP acceptance flow
pytest --cov=gdap --cov-report=term
```

Every test gets a throwaway platform (temporary SQLite, warehouse and artifact store) via the
`platform` / `context` fixtures — no mocking framework, because the ports make the real thing cheap.

## Quality gates

```bash
ruff check src tests && ruff format --check src tests
mypy
```

## Writing a plugin

Extensions register through entry points, so nothing in the core changes.

### A connector

```python
# mypackage/connector.py
from gdap.connectors.base import BaseConnector, ConnectorPlugin
from gdap.core.enums import SourceType

class KafkaConnector(BaseConnector):
    key = "kafka"
    source_type = SourceType.STREAM

    def discover(self): ...
    def read(self, options): ...        # yield polars DataFrames

class KafkaPlugin(ConnectorPlugin):
    key = "kafka"
    source_type = SourceType.STREAM
    title = "Apache Kafka"

    def config_schema(self) -> dict:
        return {"type": "object", "required": ["brokers", "topic"], "properties": {...}}

    def create(self, spec, secrets) -> BaseConnector:
        return KafkaConnector(spec, secrets)
```

```toml
[project.entry-points."gdap.connectors"]
kafka = "mypackage.connector:KafkaPlugin"
```

### A pipeline step

```python
from gdap.core.contracts import StepSpec
from gdap.pipelines.steps import StepContext, StepOutcome, register_step

@register_step(
    "transform.geocode",
    description="Add latitude/longitude from a postal code.",
    category="transform",
    options={"column": "postal code column"},
)
def geocode(context: StepContext, step: StepSpec) -> StepOutcome:
    frame = context.frame(step.input)
    enriched = ...
    context.publish(step.output or context.current or "data", enriched)
    return StepOutcome(frame=enriched, message=f"geocoded {enriched.height} rows")
```

```toml
[project.entry-points."gdap.pipeline_steps"]
geo = "mypackage.steps"
```

### An agent tool or a notification channel

`gdap.ai.tools.registry.tool(...)` registers a tool (declare its permissions and approval mode);
`gdap.notification_channels` is the entry-point group for channels with a `send(alert) -> bool`.

A broken plugin is logged and skipped — it never prevents the platform from starting.

## Conventions

* Type hints everywhere; `from __future__ import annotations` at the top.
* Errors are typed (`GdapError` subclasses) with a stable `GDAP-XXXX` code.
* Log with structured key/values, never f-strings: `log.info("job_finished", job_id=…, state=…)`.
* Comments explain *why*; the code already says what.
