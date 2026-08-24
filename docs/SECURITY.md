# Security

Security is enforced in code paths, not in documentation or prompts. This page states what is
enforced, where, and what is explicitly *not* covered.

## Authentication

API keys look like `gdap_<prefix>_<secret>`. Only a PBKDF2-SHA256 hash of the secret half is
stored, so a database dump leaks nothing usable; verification is constant-time.

```bash
gdap system key create ci-runner --role engineer --expires-in-days 90
curl -H "X-API-Key: gdap_…" http://localhost:8000/api/v1/datasets
curl -H "Authorization: Bearer gdap_…" http://localhost:8000/api/v1/datasets   # equivalent
```

* A key may hold **fewer** permissions than its user (`scopes`), never more.
* Expired or revoked keys fail closed.
* `auth_enabled=false` exists for local development and tests; the production overlay forces it on.

## Authorisation

Roles are coarse; permissions are the unit checked in code.

| Role | Read | Analyse & report | Write data & pipelines | SQL write | Destructive |
|---|---|---|---|---|---|
| `viewer` | ✅ | | | | |
| `analyst` | ✅ | ✅ | | | |
| `engineer` | ✅ | ✅ | ✅ | ✅ | |
| `admin` / `owner` | ✅ | ✅ | ✅ | ✅ | ✅ |
| `service` | ✅ | ✅ | ✅ | ✅ | |

`gdap.security.rbac.require(principal, *permissions)` is the single choke point; services call it
before doing anything that changes state.

## Multi-tenancy

Every tenant-owned table carries `org_id`, and **repositories inject that filter** — a service
cannot forget it because it never writes the query. Services additionally assert tenancy
(`require_same_tenant`) as defence in depth. Cross-tenant reads return "not found", not "forbidden",
so the API does not confirm the existence of another tenant's resources.

## SQL safety (§37)

Every SQL string passes `gdap.security.sql_guard.guard()` before reaching an engine:

| Statement | Default |
|---|---|
| `SELECT` / `WITH` | allowed, with an injected `LIMIT` |
| `INSERT` / `UPDATE` | blocked — needs `sql_write_enabled` **and** `sql:write` |
| `DELETE` | blocked — needs `sql_destructive_enabled` **and** `sql:destructive` |
| `DROP` / `TRUNCATE` / `ALTER` / `CREATE` | always blocked through the query API |
| `ATTACH` / `COPY` / `INSTALL` / `LOAD` / `PRAGMA` / `SET` | always blocked |
| `read_csv()`, `read_parquet()`, `glob()`, `getenv()`, … | always blocked |

Also blocked: statement stacking (`;`), comment-hidden writes, and writes smuggled inside a CTE.
Agents get the strictest policy (`SqlPolicy.agent()`): read-only, 10 000 rows, 20 seconds.

## Expression safety

Pipeline expressions are parsed with `ast` against an allow-list — no attribute access, no calls
except whitelisted functions, no comprehensions, no lambdas. There is no code path from a pipeline
(or an AI-generated plan) to arbitrary Python. See [PIPELINES.md](PIPELINES.md).

## Secrets

Referenced (`env:` / `file:`), resolved at point of use, held only for the lifetime of a connector,
never persisted in the metadata database, never returned by the API, redacted in logs and audit
details. `literal:` is refused in production.

## Data protection

* **Classification** is inferred from semantics and naming (`PUBLIC` → `SENSITIVE`) and can be
  overridden by an owner.
* **Masking** applies on the way out — previews, reports, agent tool results — never by mutating
  stored data.
* **Pseudonymisation** (`pseudonymize`) is stable per salt and not reversible.
* **Export** of `RESTRICTED`+ data is refused unless the tenant policy allows it.

## AI-specific controls (§36)

1. Agents may only call tools they were granted — enforced by the registry, not the prompt.
2. Every tool call is audited with arguments, result and duration.
3. Outward-facing or state-changing tools (`send_alert`, `schedule_pipeline`) require human approval.
4. Tools run as the *calling user*: no privilege escalation, no service account.
5. Tool output handed to a model is size-capped, and truncation is stated rather than hidden.
6. Answers carry evidence; a `FACT` without evidence is rejected by the contract itself.

## Transport and deployment

TLS termination, network policy and edge rate limiting belong to your ingress — the built-in rate
limiter is a per-node guard, not a substitute. Run the container as the non-root `gdap` user
(the shipped Dockerfile does).

## Not covered yet

Stated plainly rather than implied: no SSO/OIDC, no field-level encryption at rest (use encrypted
volumes), no per-column ACLs (classification is dataset-and-column-level but not per-principal),
no signed audit log. The seams exist; the features are not built.

## Reporting a vulnerability

Open a private security advisory rather than a public issue.
