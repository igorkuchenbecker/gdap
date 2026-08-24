# Pipelines

A pipeline is **data**: a YAML (or JSON) document describing steps. It is validated when it is
created, versioned every time it changes, and executed by a worker that records what each step did.

```yaml
name: sales_daily                  # unique per organisation
description: Daily revenue review
owner: data-team@example.com
tags: [sales, daily]

params:                            # defaults; overridable per run
  source: sales_files
  min_quality: 70

schedule:                          # optional; cron OR every, never both
  cron: "0 6 * * *"
  timezone: America/Sao_Paulo      # a real IANA zone, honoured across DST

retry:
  max_attempts: 3
  backoff_seconds: 30
  backoff_multiplier: 2

quality_gate: 60                   # abort the run if quality collapses
on_failure: stop                   # stop | continue
depends_on: [upstream_pipeline]    # scheduler waits for a recent successful run

steps:
  - id: ingest                     # optional; defaults to "<uses>#<position>"
    uses: read.source              # a registered step key
    with:                          # step options
      source: ${params.source}     # parameter interpolation
      object: transactions.csv
      dataset: transactions
    input: transactions            # which frame to read (default: the previous step's output)
    output: transactions           # name to publish the result under
    when: "min_quality > 0"        # optional guard over params and accumulated metrics
    approval: AUTO                 # AUTO | AUTO_WITH_VALIDATION | REQUIRES_APPROVAL | BLOCKED
    continue_on_error: false
```

## How frames flow between steps

Steps pass **named frames**, not files. `read.*` publishes a frame named after the dataset;
transformation steps rewrite the current frame in place unless you set `output:`; analysis steps
read `input:` (or the current frame) without changing it.

```yaml
- { id: read,    uses: read.dataset, with: { dataset: transactions } }   # publishes "transactions"
- { id: filter,  uses: transform.filter, with: { where: "status == 'completed'" } }  # rewrites it
- { id: monthly, uses: aggregate, output: monthly, with: { ... } }       # publishes "monthly"
- { id: trend,   uses: analyze.trend, input: transactions, with: { ... } }  # reads the row-level frame
```

## Step reference

`gdap pipeline steps` prints this table from the live registry. **writes** marks a step that
changes state (a dataset version, an artifact, an alert); everything else is read-only.

### io

| step | approval | writes | what it does | key options |
|---|---|---|---|---|
| `export.file` | auto with validation | **yes** | Export the working frame to a file artifact (csv, parquet, json, xlsx). | `format`, `name` |
| `read.dataset` | auto | no | Load an existing dataset version into the pipeline. | `dataset`, `version`, `columns`, `limit` |
| `read.query` | auto | no | Run guarded SQL across datasets and use the result as the working frame. | `sql`, `datasets` |
| `read.source` | auto | **yes** | Ingest from a registered source into a dataset version, then load it. | `source`, `object`, `dataset`, `mode`, `incremental_column` |
| `write.dataset` | auto | **yes** | Publish the working frame as a new immutable dataset version. | `dataset`, `description` |

### quality

| step | approval | writes | what it does | key options |
|---|---|---|---|---|
| `profile` | auto | no | Profile the working frame (schema, statistics, semantics, recommendations). | `dataset` |
| `quality.gate` | auto | no | Stop the pipeline when the recorded quality score is too low. | `min_score` |
| `validate.expectations` | auto | no | Evaluate data expectations and score quality across seven dimensions. | `expectations`, `auto`, `min_score`, `dataset` |
| `validate.schema` | auto | no | Assert the working frame has the expected columns and types. | `columns`, `types`, `mode` |

### cleaning

| step | approval | writes | what it does | key options |
|---|---|---|---|---|
| `clean.auto` | auto with validation | **yes** | Propose cleaning fixes from the profile and apply the approved ones. | `apply`, `actions`, `dataset` |
| `clean.missing` | auto | **yes** | Fill missing values with an explicit strategy. | `strategy`, `columns`, `value` |
| `clean.outliers` | auto with validation | **yes** | Clip or remove statistical outliers (explicit, never silent). | `columns`, `action`, `factor` |

### transform

| step | approval | writes | what it does | key options |
|---|---|---|---|---|
| `aggregate` | auto | no | Group rows and compute metrics. | `group_by`, `metrics`, `having` |
| `enrich.datetime` | auto | no | Derive calendar parts (year, quarter, month, week, weekday) from a date column. | `column`, `parts` |
| `join` | auto | no | Join the working frame with another frame or dataset. | `with_dataset`, `with_frame`, `on`, `left_on`, `right_on` |
| `transform.calculate` | auto | no | Add or replace columns from safe expressions (no Python eval). | `calculate` |
| `transform.cast` | auto | no | Cast columns to explicit types (non-strict: unparseable values become null). | `cast` |
| `transform.deduplicate` | auto | **yes** | Remove duplicate rows, optionally by a subset of key columns. | `subset`, `keep` |
| `transform.filter` | auto | no | Keep rows matching a safe boolean expression. | `where` |
| `transform.rename` | auto | no | Rename columns. | `rename` |
| `transform.select` | auto | no | Keep (or drop) a subset of columns. | `columns`, `drop` |
| `transform.sort` | auto | no | Sort rows. | `by`, `descending` |

### analytics

| step | approval | writes | what it does | key options |
|---|---|---|---|---|
| `analyze` | auto | no | Run an analysis by kind (describe, trend, anomaly, segmentation, …). | `kind`, `metric`, `dimension`, `time_column`, `granularity` |
| `analyze.anomaly` | auto | no | Detect anomalous values, periods or rows. | — |
| `analyze.comparison` | auto | no | Compare the latest period with the previous one. | — |
| `analyze.correlation` | auto | no | Correlation matrix over numeric columns. | — |
| `analyze.describe` | auto | no | Descriptive statistics for every column. | — |
| `analyze.drivers` | auto | no | Rank dimensions by explained variance. | — |
| `analyze.forecast` | auto | no | Project the metric forward with an interval. | — |
| `analyze.segmentation` | auto | no | Break a metric down by a dimension. | — |
| `analyze.trend` | auto | no | Trend, growth rate and moving average over time. | — |

### ai

| step | approval | writes | what it does | key options |
|---|---|---|---|---|
| `ai.insights` | auto | no | Summarise the run's analyses into an evidence-backed narrative. | `question`, `max_insights` |

### reporting

| step | approval | writes | what it does | key options |
|---|---|---|---|---|
| `report.generate` | auto | **yes** | Assemble the analyses produced so far into a report artifact. | `title`, `formats`, `dataset`, `include_profile` |

### alerting

| step | approval | writes | what it does | key options |
|---|---|---|---|---|
| `alert.raise` | auto | **yes** | Raise an alert unconditionally (useful after a manual condition step). | `title`, `message`, `severity` |
| `alert.threshold` | auto | **yes** | Raise an alert when a pipeline metric crosses a threshold. | `metric`, `operator`, `threshold`, `severity`, `title` |

## The expression language

Steps that take an expression (`transform.calculate`, `transform.filter`, `aggregate.having`) use a
**restricted grammar parsed with `ast`**, never `eval`. There is no attribute access, no imports, no
comprehensions, no lambdas, and no way to reach a Python object — an unknown name is an error, not a
lookup. This matters because pipelines can be authored by the AI planner as well as by people.

```yaml
- uses: transform.calculate
  with:
    calculate:
      net_revenue: "revenue * (1 - discount_pct)"
      segment:     "if(quantity > 100, 'wholesale', 'retail')"
      month:       "date_part(order_date, 'month')"
      region_key:  "upper(trim(region))"
      safe_margin: "round(coalesce(margin, 0), 2)"
```

**Supported:** column references, numeric/string/boolean/null literals, `+ - * / // % **`,
comparisons, `and`/`or`/`not`, `in [ ... ]`, and these functions:

`abs()`, `ceil()`, `clip()`, `coalesce()`, `concat()`, `contains()`, `count()`, `cum_sum()`, `date_part()`, `date_trunc()`, `exp()`, `fill_null()`, `floor()`, `if_else()`, `is_not_null()`, `is_null()`, `length()`, `log()`, `lower()`, `max()`, `mean()`, `median()`, `min()`, `replace()`, `round()`, `sqrt()`, `starts_with()`, `std()`, `sum()`, `to_date()`, `to_int()`, `to_number()`, `to_text()`, `trim()`, `upper()`

`if(cond, a, b)` is sugar for `if_else(cond, a, b)` (`if` is a Python keyword, so it is rewritten
before parsing).

**Guards** (`when:`) use the same discipline over scalars — run parameters and metrics recorded by
earlier steps:

```yaml
- uses: alert.threshold
  when: "quality_score < 100 and rows_out > 0"
```

## Running pipelines

```bash
gdap pipeline create examples/pipelines/sales_daily.yaml
gdap pipeline validate examples/pipelines/sales_daily.yaml   # parse and check without creating
gdap pipeline run sales_daily --param min_quality=80         # inline, prints every step
gdap pipeline run sales_daily --queue                        # hand it to a worker
gdap worker start                                            # runner + scheduler
gdap job show <job-id>                                       # steps, metrics, artifacts, errors
```

## Failure, retry and approval

| Situation | What happens |
|---|---|
| A step raises | The job fails (`on_failure: stop`) or continues (`continue_on_error: true` on that step). |
| The job fails | It is re-queued as `RETRYING` with exponential backoff until `retry.max_attempts`, then `FAILED` — and a critical alert is raised. |
| A step needs approval | The job parks in `AWAITING_APPROVAL`; `gdap job approve <id>` resumes it *within the same attempt* (approval does not consume a retry). |
| The worker crashes | The lease expires and another worker re-runs the job. Writes create new versions, so re-running is safe. |
| Quality collapses | `quality.gate` or the pipeline-level `quality_gate` stops the run before bad data is published. |

## Generating a pipeline from a description

```bash
gdap pipeline from-text "clean the transactions, revenue per region, compare with last month" --create
```

The plan is validated against the real step registry and the expression grammar, and is stored for
review — never executed automatically (§38, §41). See [AI.md](AI.md).
