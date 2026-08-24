# CLI reference

Generated from the live command tree. Every command accepts `--home <path>` and `--org <slug>`;
most support `--json` for machine-readable output.

```bash
gdap --home /var/lib/gdap dataset list --json | jq '.[].name'
```


## Top level

| command | does | options |
|---|---|---|
| `gdap version` | Show the platform version. | — |

## `gdap system`

| command | does | options |
|---|---|---|
| `gdap system doctor` | Full self-diagnostic: database, storage, connectors, engine, AI runtime, scheduler. | `--json` |
| `gdap system health` | Check that the platform's subsystems are reachable. | `--deep`, `--json` |
| `gdap system info` | Show configuration and capabilities. | `--json` |
| `gdap system init` | Create the database schema and the default organisation. | `--org`, `--name` |
| `gdap system key create <name>` | Issue an API key. The secret is shown once and never stored in clear text. | `--role`, `--expires-in-days` |
| `gdap system key list` | List issued API keys (secrets are never shown). | `--json` |
| `gdap system key revoke <key_id>` | Revoke an API key immediately. | — |
| `gdap system serve` | Run the HTTP API (and the bundled web UI). | `--host`, `--port`, `--reload`, `--workers` |

## `gdap source`

| command | does | options |
|---|---|---|
| `gdap source add <name>` | Register a source. Secrets are referenced, never stored: --secret password=env:PGPASS. | `--connector`, `--set`, `--secret`, `--description`, `--tag` |
| `gdap source discover <name>` | List the objects (files, tables, endpoints) a source exposes. | `--json` |
| `gdap source ingest <name>` | Ingest data into a versioned dataset. | `--object`, `--dataset`, `--mode`, `--incremental-column`, `--dedupe-key`, `--limit`, `--json` |
| `gdap source list` | List registered sources. | `--json` |
| `gdap source rm <name>` | Delete a source (datasets it produced are kept). | `--yes` |
| `gdap source test <name>` | Probe connectivity and permissions without moving data. | — |

## `gdap dataset`

| command | does | options |
|---|---|---|
| `gdap dataset clean <name>` | Propose cleaning fixes; apply only what is approved. | `--apply`, `--approve`, `--json` |
| `gdap dataset list` | List datasets in the catalog. | `--json` |
| `gdap dataset preview <name>` | Preview rows (sensitive columns are masked). | `--rows`, `--json` |
| `gdap dataset profile <name>` | Profile a dataset: distributions, missing values, keys, recommendations. | `--json` |
| `gdap dataset query <sql>` | Run guarded SQL across the workspace datasets. | `--limit`, `--json` |
| `gdap dataset show <name>` | Show a dataset with its schema and version history. | `--json` |
| `gdap dataset validate <name>` | Score data quality across seven dimensions. | `--auto`, `--json` |
| `gdap dataset versions <name>` | List the immutable versions of a dataset. | — |

## `gdap pipeline`

| command | does | options |
|---|---|---|
| `gdap pipeline create <file>` | Create (or update) a pipeline from a YAML specification. | `--update` |
| `gdap pipeline delete <name>` | Delete a pipeline definition (its run history is kept). | `--yes` |
| `gdap pipeline enable <name>` | Enable or disable a pipeline's schedule. | `--disable` |
| `gdap pipeline from-text <request>` | Generate a reviewable pipeline from a natural-language request (§41). | `--dataset`, `--create` |
| `gdap pipeline list` | List pipelines. | `--json` |
| `gdap pipeline run <name>` | Run a pipeline now. | `--param`, `--wait`, `--json` |
| `gdap pipeline show <name>` | Show a pipeline and its steps. | `--json` |
| `gdap pipeline steps` | List the step types available to pipelines. | `--json` |
| `gdap pipeline validate <file>` | Validate a pipeline file without creating it. | — |

## `gdap job`

| command | does | options |
|---|---|---|
| `gdap job approve <job_id>` | Approve blocked steps and re-queue the job. | `--step`, `--note` |
| `gdap job cancel <job_id>` | Cancel a job. | `--reason` |
| `gdap job list` | List recent jobs. | `--state`, `--pipeline`, `--limit`, `--json` |
| `gdap job reject <job_id>` | Reject a pending approval and cancel the job. | `--reason` |
| `gdap job retry <job_id>` | Re-queue a failed job. | — |
| `gdap job run <job_id>` | Execute a queued job in this process (no worker needed). | — |
| `gdap job show <job_id>` | Show a job with its steps, metrics and artifacts. | `--json` |

## `gdap analysis`

| command | does | options |
|---|---|---|
| `gdap analysis auto <dataset>` | Run every analysis that applies to the dataset. | `--json` |
| `gdap analysis insights <dataset>` | Show the most important recent insights for a dataset. | `--limit`, `--json` |
| `gdap analysis run <dataset> <kind>` | Run one analysis over a dataset. | `--param`, `--json` |

## `gdap report`

| command | does | options |
|---|---|---|
| `gdap report create <dataset>` | Profile, validate, analyse and render a report for a dataset. | `--title`, `--format`, `--open` |
| `gdap report download <report_id>` | Download a report artifact. | `--output` |
| `gdap report list` | List report artifacts. | `--json` |

## `gdap agent`

| command | does | options |
|---|---|---|
| `gdap agent ask <question>` | Ask the AI Data Analyst. Every claim comes with its evidence. | `--dataset`, `--agent`, `--json` |
| `gdap agent plan <request>` | Turn a natural-language request into a reviewable pipeline (§41). | `--dataset`, `--create` |
| `gdap agent tools` | List the tools agents may use, with their permissions and approval gates. | `--json` |

## `gdap worker`

| command | does | options |
|---|---|---|
| `gdap worker drain` | Run every queued job, then exit (useful in CI and cron). | `--max-jobs` |
| `gdap worker schedule` | Show upcoming schedules, or fire the ones that are due. | `--tick` |
| `gdap worker start` | Run the worker until interrupted (Ctrl-C for a graceful stop). | `--concurrency`, `--scheduler`, `--worker-id` |

## `gdap demo`

| command | does | options |
|---|---|---|
| `gdap demo reset` | Delete the demo workspace (database, warehouse, artifacts and generated data). | `--yes` |
| `gdap demo run` | Generate data and run the full platform loop end to end. | `--days`, `--keep`, `--ask` |


## Typical sessions

**Bootstrap and explore**
```bash
gdap system init
gdap demo run
gdap dataset list && gdap dataset show transactions
```

**Connect real data**
```bash
gdap source add warehouse --connector sql \
    --set driver=postgres --set host=db.internal --set database=analytics --set table=orders \
    --secret password=env:PGPASSWORD
gdap source test warehouse && gdap source discover warehouse
gdap source ingest warehouse --object public.orders --dataset orders \
    --mode incremental --incremental-column updated_at
```

**Automate**
```bash
gdap pipeline create examples/pipelines/sales_daily.yaml
gdap pipeline run sales_daily
gdap worker start                  # runner + scheduler
gdap job list --state FAILED
```

**Ask**
```bash
gdap agent ask "why did revenue fall last month?" --dataset orders
gdap pipeline from-text "clean orders and report revenue per region" --create
```

## Exit codes

| Code | Meaning |
|---|---|
| 0 | success |
| 1 | a domain error (printed with its `GDAP-XXXX` code), a failed pipeline run, or a degraded `doctor` |
