# Installation

## Requirements

* Python 3.11+ (3.13 recommended)
* ~200 MB of disk for the platform and its dependencies
* Nothing else — SQLite, the Parquet warehouse and the query engine are in-process

## Install

```bash
git clone <repo> && cd gdap
uv venv --python 3.13 && source .venv/bin/activate
uv pip install -e ".[dev]"          # or: pip install -e ".[dev]"
```

### Extras

| Extra | Adds | Install when |
|---|---|---|
| `postgres` | `psycopg` | The metadata store is PostgreSQL |
| `excel` | `fastexcel`, `openpyxl` | You ingest `.xlsx` sources |
| `ai` | `anthropic` | You enable the LLM provider |
| `dev` | pytest, ruff, mypy | You are developing or running the tests |

```bash
uv pip install -e ".[postgres,excel,ai]"
```

Other database drivers are installed as needed: `pymysql` (MySQL/MariaDB), `pyodbc` (SQL Server),
`oracledb` (Oracle). SQLite needs nothing.

## First run

```bash
gdap system init            # schema + default organisation
gdap doctor                 # verify every subsystem
gdap demo run               # the full loop, end to end, in seconds
gdap system serve           # API + web UI at http://127.0.0.1:8000
```

Everything the platform writes lives under `~/.gdap` (override with `GDAP_HOME` or `--home`), so
uninstalling is `rm -rf ~/.gdap`.

## Optional: PDF reports

HTML, XLSX, CSV, JSON and Markdown work out of the box. PDF needs a layout engine:

```bash
pip install weasyprint
```

Without it, `format=pdf` fails with an actionable message instead of producing a broken file.

## Verify

```bash
pytest -q                 # 158 tests
gdap system info          # resolved configuration and capabilities
gdap pipeline steps       # the 35 available step types
```
