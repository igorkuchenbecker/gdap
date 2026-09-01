"""POST /api/v1/sources/upload: the no-JSON, no-file-prep local import flow."""

from __future__ import annotations

from typing import Any

import pytest

pytestmark = pytest.mark.integration

_CSV = b"id,name,amount\n1,alpha,10\n2,beta,20\n3,gamma,30\n"


def test_upload_csv_derives_names_from_filename(api_client: Any) -> None:
    response = api_client.post(
        "/api/v1/sources/upload",
        files={"file": ("Sales Report.csv", _CSV, "text/csv")},
    )
    assert response.status_code == 201, response.text
    body = response.json()

    assert body["status"] == "completed"
    # The source name gets a per-upload suffix (see the repeated-upload test below), but the
    # dataset name — the stable, versioned catalog entry — stays exactly the derived slug.
    assert body["source"]["name"].startswith("sales-report-")
    assert body["source"]["connector"] == "file"
    assert body["source"]["type"] == "file"
    assert body["result"]["dataset"] == "sales-report"
    assert body["result"]["rows"] == 3

    dataset = api_client.get("/api/v1/datasets/sales-report").json()
    assert dataset["name"] == "sales-report"


def test_upload_honours_explicit_source_and_dataset_names(api_client: Any) -> None:
    response = api_client.post(
        "/api/v1/sources/upload",
        files={"file": ("data.csv", _CSV, "text/csv")},
        data={"source": "my-custom-source", "dataset": "my_custom_dataset"},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["source"]["name"] == "my-custom-source"
    assert body["result"]["dataset"] == "my_custom_dataset"


def test_upload_rejects_unsupported_extension(api_client: Any) -> None:
    response = api_client.post(
        "/api/v1/sources/upload",
        files={"file": ("payload.exe", b"MZ...", "application/octet-stream")},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "GDAP-2003"


def test_upload_rejects_empty_file(api_client: Any, platform: Any) -> None:
    response = api_client.post(
        "/api/v1/sources/upload",
        files={"file": ("empty.csv", b"", "text/csv")},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "GDAP-2002"

    # nothing should have been left behind in staging
    staging = platform.settings.paths.staging
    leftovers = [p for p in staging.rglob("*") if p.is_file()]
    assert leftovers == []

    # and no source was registered for it either
    assert api_client.get("/api/v1/sources/empty").status_code == 404


def test_upload_neutralises_path_traversal_in_filename(api_client: Any, platform: Any) -> None:
    response = api_client.post(
        "/api/v1/sources/upload",
        files={"file": ("../../../etc/passwd.csv", _CSV, "text/csv")},
    )
    assert response.status_code == 201, response.text
    body = response.json()

    # the basename survives, sanitised — never the traversal path
    assert body["source"]["name"].startswith("passwd-")

    stored_path = body["source"]["config"]["path"]
    staging = platform.settings.paths.staging.resolve()
    assert str(staging) in stored_path
    assert ".." not in stored_path.split("/")


def test_upload_enforces_max_upload_mb(api_client: Any, platform: Any) -> None:
    platform.settings.api.max_upload_mb = 1  # 1 MiB ceiling for this test only
    oversized = b"id,name\n" + b"1,x\n" * 300_000  # well over 1 MiB

    response = api_client.post(
        "/api/v1/sources/upload",
        files={"file": ("big.csv", oversized, "text/csv")},
    )
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "GDAP-2004"

    staging = platform.settings.paths.staging
    leftovers = [p for p in staging.rglob("*") if p.is_file()]
    assert leftovers == []


def test_upload_requires_source_write_permission(platform: Any) -> None:
    """A viewer-scoped key cannot upload — RBAC applies to this endpoint like any other.

    The permission check must run before any bytes are streamed to staging: a principal without
    write access should not be able to burn I/O and bandwidth up to max_upload_mb by uploading
    files it will never be allowed to register.
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

    try:
        with platform.db.session() as session:
            principal = platform.resolve_principal(session)
            issued = api_keys.generate()
            user = UserRepository(session, principal.org_id).by_email(principal.email)
            ApiKeyRepository(session, principal.org_id).create(
                user_id=user.id,
                name="viewer-key",
                prefix=issued.prefix,
                key_hash=issued.key_hash,
                scopes=[p.value for p in permissions_for(Role.VIEWER)],
            )

        with TestClient(create_app(platform=platform)) as client:
            response = client.post(
                "/api/v1/sources/upload",
                files={"file": ("data.csv", _CSV, "text/csv")},
                headers={"X-API-Key": issued.plaintext},
            )
            assert response.status_code == 403
            assert response.json()["error"]["code"] == "GDAP-3001"

        staging = platform.settings.paths.staging
        assert list(staging.rglob("*")) == []
    finally:
        platform.settings.security.auth_enabled = False
        container_module._PLATFORM = None


def test_upload_repeated_filename_without_explicit_source_versions_same_dataset(
    api_client: Any,
) -> None:
    """Re-importing the same file (no explicit `source`) must not 409 on the source name."""
    first = api_client.post(
        "/api/v1/sources/upload",
        files={"file": ("data.csv", _CSV, "text/csv")},
    )
    assert first.status_code == 201, first.text
    second = api_client.post(
        "/api/v1/sources/upload",
        files={"file": ("data.csv", _CSV, "text/csv")},
    )
    assert second.status_code == 201, second.text

    first_body, second_body = first.json(), second.json()
    assert first_body["source"]["name"] != second_body["source"]["name"]
    assert first_body["result"]["dataset"] == second_body["result"]["dataset"] == "data"

    dataset = api_client.get("/api/v1/datasets/data").json()
    assert dataset["name"] == "data"
