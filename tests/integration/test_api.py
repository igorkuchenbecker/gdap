"""The HTTP API: contracts, error envelope, auth and the resource lifecycle."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.integration


@pytest.fixture
def seeded(api_client: Any, demo_dir: Path) -> Any:
    api_client.post(
        "/api/v1/sources",
        json={
            "name": "files",
            "type": "file",
            "connector": "file.csv",
            "config": {"path": str(demo_dir), "pattern": "*.csv"},
        },
    )
    api_client.post(
        "/api/v1/sources/files/ingest",
        json={"object": "transactions.csv", "dataset": "transactions"},
    )
    return api_client


def test_health_and_readiness(api_client: Any) -> None:
    health = api_client.get("/health")
    assert health.status_code == 200
    assert health.json()["ok"] is True
    assert health.headers["X-Request-ID"]

    assert api_client.get("/readyz").json()["ready"] is True
    assert "counters" in api_client.get("/metrics").json()


def test_openapi_documents_every_resource(api_client: Any) -> None:
    paths = api_client.get("/openapi.json").json()["paths"]
    for expected in (
        "/api/v1/sources",
        "/api/v1/datasets",
        "/api/v1/pipelines",
        "/api/v1/jobs/{job_id}",
        "/api/v1/analyses",
        "/api/v1/reports",
        "/api/v1/agents/ask",
        "/api/v1/audit",
    ):
        assert expected in paths, expected


def test_error_envelope_is_uniform(api_client: Any) -> None:
    response = api_client.get("/api/v1/datasets/does-not-exist")
    assert response.status_code == 404
    error = response.json()["error"]
    assert error["code"] == "GDAP-2000"
    assert error["trace_id"]


def test_validation_errors_are_structured(api_client: Any) -> None:
    response = api_client.post("/api/v1/sources", json={"name": "incomplete"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "GDAP-2002"
    assert response.json()["error"]["details"]["errors"]


def test_source_lifecycle(api_client: Any, demo_dir: Path) -> None:
    created = api_client.post(
        "/api/v1/sources",
        json={
            "name": "files",
            "type": "file",
            "connector": "file.csv",
            "config": {"path": str(demo_dir), "pattern": "*.csv"},
        },
    )
    assert created.status_code == 201

    assert api_client.post("/api/v1/sources/files/test").json()["ok"] is True
    discovered = api_client.get("/api/v1/sources/files/discover").json()
    assert any(item["name"] == "transactions.csv" for item in discovered["items"])

    ingested = api_client.post(
        "/api/v1/sources/files/ingest", json={"object": "regions.csv", "dataset": "regions"}
    )
    assert ingested.json()["result"]["rows"] == 4

    # Deleting a source is an ALWAYS_APPROVAL operation (§38): the API says so explicitly
    # instead of silently doing it.
    deletion = api_client.delete("/api/v1/sources/files")
    assert deletion.status_code == 409
    assert deletion.json()["error"]["code"] == "GDAP-3005"


def test_secrets_are_never_returned(api_client: Any, demo_dir: Path) -> None:
    api_client.post(
        "/api/v1/sources",
        json={
            "name": "secure",
            "type": "sql",
            "connector": "sql",
            "config": {"driver": "sqlite", "database": "/tmp/x.db"},
            "secret_refs": {"password": "env:PGPASS"},
        },
    )
    payload = api_client.get("/api/v1/sources/secure").json()
    assert payload["secret_refs"] == ["password"]  # names only, never values


def test_dataset_endpoints(seeded: Any) -> None:
    listing = seeded.get("/api/v1/datasets").json()
    assert listing["count"] == 1
    assert listing["items"][0]["latest_version"]["checksum"]

    preview = seeded.get("/api/v1/datasets/transactions/preview?rows=5").json()
    assert len(preview["records"]) == 5
    assert preview["masked"] is True

    profile = seeded.post("/api/v1/datasets/transactions/profile").json()
    assert profile["rows"] > 0

    quality = seeded.post(
        "/api/v1/datasets/transactions/validate", json={"auto_expectations": True}
    ).json()
    assert 0 <= quality["score"] <= 100

    versions = seeded.get("/api/v1/datasets/transactions/versions").json()
    assert versions["count"] >= 1


def test_query_endpoint_enforces_the_sql_policy(seeded: Any) -> None:
    ok = seeded.post(
        "/api/v1/datasets/query",
        json={"sql": "SELECT region, count(*) AS n FROM transactions GROUP BY region"},
    )
    assert ok.status_code == 200 and ok.json()["rows"] > 0

    blocked = seeded.post("/api/v1/datasets/query", json={"sql": "DELETE FROM transactions"})
    assert blocked.status_code == 403
    assert blocked.json()["error"]["code"] == "GDAP-3003"


def test_pipeline_and_job_lifecycle(seeded: Any) -> None:
    spec = {
        "name": "api_flow",
        "steps": [
            {"id": "read", "uses": "read.dataset", "with": {"dataset": "transactions"}},
            {
                "id": "agg",
                "uses": "aggregate",
                "output": "m",
                "with": {"group_by": ["region"], "metrics": {"revenue": "sum(revenue)"}},
            },
            {"id": "write", "uses": "write.dataset", "with": {"dataset": "by_region"}},
        ],
    }
    assert seeded.post("/api/v1/pipelines", json={"spec": spec}).status_code == 201
    assert "steps" in seeded.get("/api/v1/pipelines/api_flow").json()["spec"]
    assert seeded.get("/api/v1/pipelines/api_flow/yaml").text.startswith("name: api_flow")

    run = seeded.post("/api/v1/pipelines/api_flow/run", json={"wait": True})
    assert run.json()["state"] == "SUCCESS"

    job_id = run.json()["job_id"]
    job = seeded.get(f"/api/v1/jobs/{job_id}").json()
    assert [step["state"] for step in job["steps"]] == ["SUCCESS"] * 3
    assert seeded.get("/api/v1/jobs?state=SUCCESS").json()["count"] >= 1


def test_queued_job_can_be_executed_and_cancelled(seeded: Any) -> None:
    spec = {
        "name": "queued",
        "steps": [{"id": "read", "uses": "read.dataset", "with": {"dataset": "transactions"}}],
    }
    seeded.post("/api/v1/pipelines", json={"spec": spec})
    queued = seeded.post("/api/v1/pipelines/queued/run", json={"wait": False}).json()
    assert queued["state"] == "PENDING"

    cancelled = seeded.post(f"/api/v1/jobs/{queued['job_id']}/cancel").json()
    assert cancelled["state"] == "CANCELLED"

    conflict = seeded.post(f"/api/v1/jobs/{queued['job_id']}/cancel")
    assert conflict.status_code == 409


def test_analysis_and_report_endpoints(seeded: Any) -> None:
    analysis = seeded.post(
        "/api/v1/analyses",
        json={
            "dataset": "transactions",
            "kind": "trend",
            "params": {"metric": "revenue", "time_column": "order_date"},
        },
    )
    assert analysis.status_code == 200
    assert analysis.json()["insights"]

    report = seeded.post(
        "/api/v1/reports", json={"dataset": "transactions", "formats": ["html", "json"]}
    )
    assert {item["format"] for item in report.json()["items"]} == {"html", "json"}

    report_id = report.json()["items"][0]["id"]
    download = seeded.get(f"/api/v1/reports/{report_id}/download")
    assert download.status_code == 200
    assert b"<html" in download.content.lower()


def test_agent_endpoints(seeded: Any) -> None:
    tools = seeded.get("/api/v1/agents/tools").json()
    assert tools["count"] >= 15

    answer = seeded.post(
        "/api/v1/agents/ask",
        json={"question": "What is the revenue trend?", "dataset": "transactions"},
    ).json()
    assert answer["answer"]
    assert answer["tool_calls"]
    assert answer["evidence"], "an answer must carry evidence"

    plan = seeded.post(
        "/api/v1/agents/plan",
        json={"request": "clean transactions and report revenue per region", "create": True},
    ).json()
    assert plan["plan"]["requires_review"] is True
    assert plan["plan"]["spec"]["steps"][0]["with"]  # aliased for round-tripping
    assert plan["pipeline"]["name"]


def test_governance_endpoints(seeded: Any) -> None:
    catalog = seeded.get("/api/v1/catalog").json()
    assert catalog["counts"]["datasets"] >= 1

    audit = seeded.get("/api/v1/audit?limit=10").json()
    assert audit["count"] > 0
    assert {"source.create", "source.ingest"} & {event["action"] for event in audit["items"]}

    dataset_id = seeded.get("/api/v1/datasets/transactions").json()["id"]
    lineage = seeded.get(f"/api/v1/lineage/dataset/{dataset_id}").json()
    assert lineage["nodes"]

    assert "counts" in seeded.get("/api/v1/system/dashboard").json()


def test_api_key_authentication(platform: Any, demo_dir: Path) -> None:
    """With auth enabled, an unauthenticated call is rejected and a valid key works."""
    from fastapi.testclient import TestClient

    import gdap.core.container as container_module
    from gdap.api.app import create_app

    platform.settings.security.auth_enabled = True
    container_module._PLATFORM = platform

    with TestClient(create_app(platform=platform)) as client:
        assert client.get("/api/v1/datasets").status_code == 401
        assert (
            client.get("/api/v1/datasets", headers={"X-API-Key": "gdap_bad_key"}).status_code == 401
        )

        # issue a key through the service layer (auth is required to reach the admin endpoint)
        from gdap.core.enums import Role
        from gdap.security import api_keys
        from gdap.security.rbac import permissions_for
        from gdap.storage.repositories import ApiKeyRepository, UserRepository

        with platform.db.session() as session:
            principal = platform.resolve_principal(session)
            issued = api_keys.generate()
            user = UserRepository(session, principal.org_id).by_email(principal.email)
            ApiKeyRepository(session, principal.org_id).create(
                user_id=user.id,
                name="test",
                prefix=issued.prefix,
                key_hash=issued.key_hash,
                scopes=[p.value for p in permissions_for(Role.ADMIN)],
            )

        authorised = client.get("/api/v1/datasets", headers={"X-API-Key": issued.plaintext})
        assert authorised.status_code == 200

        bearer = client.get(
            "/api/v1/datasets", headers={"Authorization": f"Bearer {issued.plaintext}"}
        )
        assert bearer.status_code == 200

    platform.settings.security.auth_enabled = False
    container_module._PLATFORM = None


def test_reissuing_a_key_for_the_same_name_updates_the_role(platform: Any, demo_dir: Path) -> None:
    """Regression: `create_api_key` used to reuse an existing user by e-mail without updating
    their stored role, so a second `system key create <name> --role X` for the same name was a
    silent no-op — the issued key's *scopes* claimed the new role, but the principal's actual
    permissions still came from the user's stale role (scopes only ever narrow a user's
    permissions, never widen them — see security/rbac.py), so nothing the new role granted
    actually worked. Found running the GDAP↔JARVIS integration against a live server: a key
    re-issued with --role engineer still could not ingest data because the underlying user was
    still "analyst" from the first issuance. Needs auth genuinely enforced (not the testing
    profile's auth_enabled=false) or every request would run as the org owner regardless of
    which key's scopes are sent, and the bug would never be exercised.
    """
    from fastapi.testclient import TestClient

    import gdap.core.container as container_module
    from gdap.api.app import create_app
    from gdap.core.enums import Role
    from gdap.security import api_keys
    from gdap.security.rbac import permissions_for
    from gdap.storage.repositories import ApiKeyRepository, UserRepository

    platform.settings.security.auth_enabled = True
    container_module._PLATFORM = platform

    with platform.db.session() as session:
        principal = platform.resolve_principal(session)
        bootstrap = api_keys.generate()
        owner = UserRepository(session, principal.org_id).by_email(principal.email)
        ApiKeyRepository(session, principal.org_id).create(
            user_id=owner.id,
            name="bootstrap-admin",
            prefix=bootstrap.prefix,
            key_hash=bootstrap.key_hash,
            scopes=[p.value for p in permissions_for(Role.ADMIN)],
        )
    admin_headers = {"X-API-Key": bootstrap.plaintext}

    with TestClient(create_app(platform=platform)) as client:
        # user_email is explicit and distinct from the admin's own account on purpose: the bug
        # only manifests for a *named service identity* reused across calls. Omitting user_email
        # here would make this endpoint's "issue myself a key" fallback resolve to the admin's
        # own already-privileged account, masking the bug entirely (its permissions are a
        # superset of every role, so the buggy intersection would "accidentally" look correct).
        first = client.post(
            "/api/v1/admin/api-keys",
            json={"name": "svc", "user_email": "svc@service.local", "role": "analyst"},
            headers=admin_headers,
        ).json()
        assert "dataset:write" not in first["scopes"]

        second = client.post(
            "/api/v1/admin/api-keys",
            json={"name": "svc", "user_email": "svc@service.local", "role": "engineer"},
            headers=admin_headers,
        ).json()
        assert "dataset:write" in second["scopes"]

        # The second key must actually be able to do what "engineer" implies (ingest data), not
        # just carry the right scope list — the bug was specifically that the *principal* stayed
        # capped to the old role despite the key's own scopes looking correct.
        svc_headers = {"X-API-Key": second["api_key"]}
        created = client.post(
            "/api/v1/sources",
            json={
                "name": "svc_source",
                "type": "file",
                "connector": "file.csv",
                "config": {"path": str(demo_dir), "pattern": "*.csv"},
            },
            headers=svc_headers,
        )
        assert created.status_code == 201
        ingested = client.post(
            "/api/v1/sources/svc_source/ingest",
            json={"object": "regions.csv", "dataset": "svc_regions"},
            headers=svc_headers,
        )
        assert ingested.status_code == 200, ingested.json()

    platform.settings.security.auth_enabled = False
    container_module._PLATFORM = None
