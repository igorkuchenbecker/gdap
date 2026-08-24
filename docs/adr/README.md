# Architecture Decision Records

Each ADR records a decision that was expensive to make and would be expensive to reverse: the
problem, the options considered, the choice, and what it costs us.

| # | Decision | Status |
|---|---|---|
| [001](001-single-package-layout.md) | One installable package instead of sibling top-level folders | Accepted |
| [002](002-polars-as-the-frame-engine.md) | Polars as the in-memory frame engine | Accepted |
| [003](003-duckdb-and-parquet-warehouse.md) | DuckDB over versioned Parquet as the analytical store | Accepted |
| [004](004-database-backed-job-queue.md) | Database-backed job queue instead of Celery/Airflow | Accepted |
| [005](005-storage-and-ports.md) | Ports and adapters for storage, queue and query engine | Accepted |
| [006](006-ai-optional-by-construction.md) | The AI layer is optional by construction | Accepted |
| [007](007-buildless-web-ui.md) | A build-free web UI as an API client | Accepted |
