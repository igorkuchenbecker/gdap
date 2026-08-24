# The AI layer

GDAP's AI layer is an *analyst*, not a chatbot bolted onto a database. Three rules shape it:

1. **It never invents numbers.** Every figure comes from a tool result, and every claim carries the
   source, query or calculation that produced it.
2. **It works with no credentials.** The default provider is deterministic; a model makes answers
   more fluent, never more authoritative.
3. **Its permissions are enforced in code**, not requested in a prompt.

## Providers

| Provider | Needs | Behaviour |
|---|---|---|
| `heuristic` (default) | nothing | Routes the question to a tool by intent, then phrases the answer strictly from tool results. |
| `anthropic` | `ANTHROPIC_API_KEY` | Claude Messages API with tool use (`claude-opus-5`, adaptive thinking). |

```bash
export GDAP_AI__PROVIDER=anthropic
export ANTHROPIC_API_KEY=sk-ant-…
gdap agent ask "why did revenue fall last month?" --dataset transactions
```

A missing or invalid key logs a warning and falls back to `heuristic` — an AI misconfiguration
never becomes an outage. `gdap agent tools` shows the active mode.

## Agents

Rather than one omniscient agent, five specialists with **different tool grants** — a security
property, not a style choice:

| Agent | Role | Tools |
|---|---|---|
| `data` | what exists, where it came from | list/describe/profile datasets, lineage, SQL |
| `quality` | whether it can be trusted | quality report, profile, describe |
| `analysis` | what happened and why | trend, comparison, segmentation, drivers, anomaly, forecast, correlation, metric, SQL, chart |
| `reporting` | turning findings into artifacts | report, chart, quality, alert |
| `governance` | provenance and accountability | lineage, describe, list |

The orchestrator routes by intent **deterministically** — routing decides which tool set is
granted, so it is not delegated to a model.

## Tools

18 tools, each a thin wrapper over a platform *service* (never over raw storage), so RBAC, tenant
scoping, the SQL policy, masking and lineage all apply automatically.

```bash
gdap agent tools          # names, categories, permissions, approval gates
```

Every call is audited (`agent.tool_call`) with its arguments, outcome and duration. Tools that
change state or reach outside the platform require explicit human approval.

### Missing arguments are resolved, not guessed silently

Models omit optional arguments constantly, and the deterministic planner has none to give. Rather
than analysing whichever numeric column comes first, tools score the dataset's *semantic* schema
against the user's wording — and report the choice:

```text
• revenue per month: increasing, 302.94K → 656.58K (+116.7%)
• (columns chosen automatically: metric=revenue, time_column=order_date)
```

## Evidence

```jsonc
{
  "answer": "• revenue fell 29.8% versus the previous month …",
  "insights": [
    { "kind": "fact",       "title": "revenue fell 29.8% versus the previous month" },
    { "kind": "inference",  "title": "North drove the largest decline (-232.15K)" },
    { "kind": "hypothesis", "title": "The latest month is only 74% complete" }
  ],
  "evidence": [
    { "source": "dataset:transactions", "calculation": "sum(revenue) per month",
      "values": { "current": 656580.2, "previous": 935170.5 }, "rows_considered": 3416 }
  ],
  "confidence": 0.8,
  "provider": "heuristic",
  "limitations": ["answered by the 'analysis' agent — the request asks for a measurement"]
}
```

`fact` / `inference` / `hypothesis` / `recommendation` are separated by contract: a `FACT` without
evidence fails validation before it can reach a user (§36.7).

The platform volunteers its own limits — a partial final period, a noisy trend, a sampled profile —
because a confident wrong answer is worse than an honest incomplete one.

## Natural language → pipeline

```bash
gdap pipeline from-text "clean transactions, revenue per region, compare with last month" --create
```

```text
INGEST → CLEAN → DEDUPLICATE → AGGREGATE → COMPARE → REPORT
```

Both planners produce a spec that is validated against the **real** step registry and expression
grammar; an invalid LLM plan is retried once with the error attached and then discarded rather than
partially executed. Plans are always `requires_review: true` — a pipeline writes data, so a human
approves it (§38).

## Using it

```bash
gdap agent ask "where are the anomalies in revenue?" --dataset transactions
gdap agent ask "is this data trustworthy?" --agent quality --json
```

```http
POST /api/v1/agents/ask    {"question": "...", "dataset": "transactions"}
POST /api/v1/agents/plan   {"request": "...", "create": true}
GET  /api/v1/agents/tools
```

## Cost and limits

Bounded by construction: `max_tool_iterations` (8), `max_tool_calls` per agent (12), tool output
capped at 6 000 characters with truncation stated, and the heuristic provider costing nothing at
all. Token usage is accumulated per run and returned with the answer.
