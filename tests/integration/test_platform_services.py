"""Services against a real database, warehouse and query engine."""

from __future__ import annotations

from pathlib import Path

import pytest

from gdap.core.contracts import Principal, SourceSpec
from gdap.core.enums import DataClassification, IngestionMode, Role, SourceType
from gdap.core.errors import AuthorizationError, ConflictError, NotFoundError, SqlSafetyError
from gdap.core.services.context import ServiceContext
from gdap.ingestion import IngestRequest
from gdap.security.rbac import permissions_for

pytestmark = pytest.mark.integration


def test_ingestion_creates_immutable_versions(context: ServiceContext, demo_dir: Path) -> None:
    context.sources.register(
        SourceSpec(
            name="files",
            type=SourceType.FILE,
            connector="file.csv",
            config={"path": str(demo_dir), "pattern": "*.csv"},
        )
    )
    first = context.sources.ingest(
        IngestRequest(source="files", object="transactions.csv", dataset="tx")
    )
    second = context.sources.ingest(
        IngestRequest(source="files", object="transactions.csv", dataset="tx")
    )

    assert first.version == 1 and second.version == 2
    assert first.checksum == second.checksum  # same bytes in, same checksum out
    assert context.datasets.frame("tx", version=1).height == first.rows
    assert len(context.datasets.versions("tx")) == 2


def test_ingestion_infers_semantics_and_classification(
    context: ServiceContext, demo_dir: Path
) -> None:
    context.sources.register(
        SourceSpec(
            name="files",
            type=SourceType.FILE,
            connector="file.csv",
            config={"path": str(demo_dir), "pattern": "*.csv"},
        )
    )
    result = context.sources.ingest(
        IngestRequest(source="files", object="customers.csv", dataset="customers")
    )
    meanings = {column.name: column.semantic_type.value for column in result.schema_.columns}
    assert meanings["email"] == "email"
    dataset = context.datasets.get("customers")
    assert DataClassification(dataset.classification).rank >= DataClassification.RESTRICTED.rank


def test_append_mode_deduplicates_on_keys(context: ServiceContext, demo_dir: Path) -> None:
    context.sources.register(
        SourceSpec(
            name="files",
            type=SourceType.FILE,
            connector="file.csv",
            config={"path": str(demo_dir), "pattern": "*.csv"},
        )
    )
    first = context.sources.ingest(
        IngestRequest(source="files", object="products.csv", dataset="products")
    )
    appended = context.sources.ingest(
        IngestRequest(
            source="files",
            object="products.csv",
            dataset="products",
            mode=IngestionMode.APPEND,
            dedupe_keys=["product_id"],
        )
    )
    assert appended.rows == first.rows  # re-ingesting the same rows must not duplicate them


def test_profile_validate_and_quality_history(loaded_context: ServiceContext) -> None:
    profile = loaded_context.datasets.profile("transactions")
    report = loaded_context.datasets.validate("transactions", auto_expectations=True)

    assert profile.rows > 0
    assert 0 <= report.score <= 100
    history = loaded_context.datasets.quality_history("transactions")
    assert history and history[0]["score"] == report.score


def test_cleaning_publishes_a_new_version(loaded_context: ServiceContext) -> None:
    proposals, _profile = loaded_context.datasets.propose_cleaning("transactions")
    before = loaded_context.datasets.get("transactions").current_version
    version, result = loaded_context.datasets.apply_cleaning("transactions", proposals)

    assert version.version == before + 1
    assert result.applied
    assert result.rows_after <= result.rows_before
    assert (
        loaded_context.datasets.frame("transactions", version=before).height == result.rows_before
    )


def test_query_engine_registers_datasets(loaded_context: ServiceContext) -> None:
    result = loaded_context.datasets.query(
        "SELECT region, count(*) AS n FROM transactions GROUP BY region ORDER BY n DESC"
    )
    assert result["rows"] > 0
    assert "transactions" in result["registered"]


def test_query_engine_blocks_destructive_sql(loaded_context: ServiceContext) -> None:
    with pytest.raises(SqlSafetyError):
        loaded_context.datasets.query("DROP TABLE transactions")


def test_preview_masks_restricted_columns(context: ServiceContext, demo_dir: Path) -> None:
    context.sources.register(
        SourceSpec(
            name="files",
            type=SourceType.FILE,
            connector="file.csv",
            config={"path": str(demo_dir), "pattern": "*.csv"},
        )
    )
    context.sources.ingest(
        IngestRequest(source="files", object="customers.csv", dataset="customers")
    )
    preview = context.datasets.preview("customers", rows=5)
    emails = [record["email"] for record in preview["records"] if record["email"]]
    assert emails and all("*" in email for email in emails)


def test_tenant_isolation_between_organisations(platform, principal) -> None:  # type: ignore[no-untyped-def]
    from gdap.storage.repositories import OrganizationRepository, UserRepository

    with platform.db.session() as session:
        other = OrganizationRepository(session).create(slug="other", name="Other Co")
        user = UserRepository(session, other.id).create(
            email="owner@other.local", name="Other", role="owner"
        )
        other_principal = Principal(
            org_id=other.id,
            user_id=user.id,
            role=Role.OWNER,
            permissions=permissions_for(Role.OWNER),
        )

    with platform.unit_of_work(principal) as first:
        first.datasets.write_frame("private", __import__("polars").DataFrame({"a": [1]}))

    with platform.unit_of_work(other_principal) as second, pytest.raises(NotFoundError):
        assert second.datasets.list() == []
        second.datasets.get("private")


def test_rbac_blocks_writes_for_viewers(platform, principal) -> None:  # type: ignore[no-untyped-def]
    viewer = Principal(
        org_id=principal.org_id,
        user_id=principal.user_id,
        role=Role.VIEWER,
        permissions=permissions_for(Role.VIEWER),
    )
    with platform.unit_of_work(viewer) as context, pytest.raises(AuthorizationError):
        context.sources.register(
            SourceSpec(
                name="nope", type=SourceType.FILE, connector="file.csv", config={"path": "/tmp"}
            )
        )


def test_duplicate_source_name_conflicts(context: ServiceContext, demo_dir: Path) -> None:
    spec = SourceSpec(
        name="files",
        type=SourceType.FILE,
        connector="file.csv",
        config={"path": str(demo_dir)},
    )
    context.sources.register(spec)
    with pytest.raises(ConflictError):
        context.sources.register(spec)


def test_audit_and_lineage_are_recorded(loaded_context: ServiceContext) -> None:
    actions = {event["action"] for event in loaded_context.governance.audit(limit=50)}
    assert {"source.create", "source.ingest"} <= actions

    dataset = loaded_context.datasets.get("transactions")
    graph = loaded_context.governance.lineage("dataset", dataset.id, depth=3)
    assert any(node["type"] == "source" for node in graph["nodes"])
