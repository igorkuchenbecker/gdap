# Governance

Every question an auditor asks — *where did this number come from, who changed it, may it leave
the building, when should it be deleted* — is answerable from the platform itself.

## Audit trail

Append-only; there is deliberately no update or delete path in the repository layer.

```bash
gdap dataset show transactions --json | jq .quality_score
curl "localhost:8000/api/v1/audit?action=dataset.clean&since_hours=24" -H "X-API-Key: $KEY"
```

Each event records actor, action, resource, result, trace id and redacted details. Actions include
`source.create/test/ingest`, `dataset.profile/validate/clean/query/export`, `pipeline.create/update`,
`job.create/finish/approve/reject`, `agent.ask/plan/tool_call`, `apikey.create/revoke`,
`report.generate/download`.

Auditing never breaks the operation it records: a failed audit write is logged loudly and the
operation proceeds, because losing the work to protect the log would be the worse trade.

## Lineage

Typed nodes (`source`, `dataset`, `dataset_version`, `pipeline`, `job`, `analysis`, `report`,
`model`, `alert`) connected by stamped edges.

```text
source:sales_files ──ingest:full──▶ dataset_version:v1 ──profile──▶ analysis
                                          │
dataset:transactions ──version──────────▶ │ ──clean──▶ dataset_version:v2 ──pipeline_write──▶ report
```

```bash
curl "localhost:8000/api/v1/lineage/dataset/$ID?depth=3" -H "X-API-Key: $KEY"
```

A report is therefore traceable to the exact dataset version and checksum it was built from.

## Classification

Inferred at ingestion from semantics and naming, overridable by an owner:

| Level | Examples |
|---|---|
| `PUBLIC` | published reference data |
| `INTERNAL` | operational columns with no personal or commercial sensitivity |
| `CONFIDENTIAL` | revenue, margin, customer identifiers |
| `RESTRICTED` | e-mail, phone, address, salary |
| `SENSITIVE` | national IDs, credentials, health data |

A dataset is as sensitive as its most sensitive column. Classification drives masking, export
policy and approval thresholds.

## Human-in-the-loop (§38)

| Level | Meaning |
|---|---|
| `AUTO` | runs unattended |
| `AUTO_WITH_VALIDATION` | runs, then is verified (e.g. quality re-checked after cleaning) |
| `REQUIRES_APPROVAL` | parks until a human decides |
| `BLOCKED` | never runs |

Escalation is automatic: an operation on `RESTRICTED`+ data, an AI-suggested change, or one
affecting more than `max_auto_delete_ratio` of rows is promoted to `REQUIRES_APPROVAL`.

```bash
gdap job show <job-id>                        # shows which steps are blocked, and why
gdap job approve <job-id> --note "reviewed by data owner"
gdap job reject  <job-id> --reason "wrong window"
```

Approving resumes the *same* attempt, so a human decision never consumes a retry.

## Policy engine

Tenant-level knobs in `organizations.settings.policy`:

```json
{
  "policy": {
    "auto_apply_cleaning": true,
    "auto_apply_ai_suggestions": false,
    "max_auto_delete_ratio": 0.05,
    "restricted_export_allowed": false,
    "default_retention_days": 365,
    "require_approval_above_classification": "RESTRICTED"
  }
}
```

Compliance regimes are expressed as configuration, never as hardcoded legal logic — which is what
lets the same platform run under different jurisdictions (§44).

## Retention

```bash
curl localhost:8000/api/v1/retention/candidates -H "X-API-Key: $KEY"
```

Candidates are **reported, never auto-deleted**: the current version of a dataset is always
excluded, and deletion is an `ALWAYS_APPROVAL` operation.

## Reproducibility (§42)

Every run records: input dataset version and checksum, pipeline version and fingerprint, parameters,
platform version, timings, and per-step metrics. The same bytes ingested twice produce the same
checksum, and dataset versions are immutable — so an analysis from three months ago can be re-run
against exactly the data it saw.

## Time zones and locale

Naive timestamps are assumed UTC — the only workable default, stated explicitly rather than hidden,
and surfaced as a timeliness finding when it matters. Locale, currency and formats are configured
per deployment (§43); nothing assumes USD or `MM/DD/YYYY`.
