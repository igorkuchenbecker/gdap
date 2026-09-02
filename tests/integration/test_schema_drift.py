"""The schema-evolution guard, which stock settings used to disable.

`allow_schema_evolution` and `allow_breaking_schema_change` answer different questions — "may
this dataset's shape change at all" and "may a column be dropped or retyped" — and the guard
joined them with `or`. Since the first ships as True, the second never mattered: a load that
replaced every column landed as a new version with a warning, while the module docstring
promised breaking changes were refused.

The tests are written against a real ingestion so they exercise the guard where it lives,
rather than asserting on the diff object in isolation.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gdap.core.contracts import SourceSpec
from gdap.core.enums import SourceType
from gdap.core.errors import SchemaDriftError
from gdap.core.services.context import ServiceContext
from gdap.ingestion import IngestRequest

pytestmark = pytest.mark.integration

_ORIGINAL = "id,region,amount\n1,North,100\n2,South,250\n"
_ADDITIVE = "id,region,amount,note\n3,East,50,ok\n"
_UNRELATED = "sku,vendor,price\nA1,Acme,9.5\n"


@pytest.fixture
def loaded(context: ServiceContext, tmp_path: Path) -> ServiceContext:
    """A source directory plus one ingested version to evolve away from."""
    (tmp_path / "first.csv").write_text(_ORIGINAL)
    context.sources.register(
        SourceSpec(
            name="drop-box",
            type=SourceType.FILE,
            connector="file.csv",
            config={"path": str(tmp_path), "pattern": "*.csv"},
        )
    )
    context.sources.ingest(IngestRequest(source="drop-box", object="first.csv", dataset="tx"))
    return context


def test_an_added_column_is_allowed_and_recorded(loaded: ServiceContext, tmp_path: Path) -> None:
    """Additive evolution is the case `allow_schema_evolution` exists to permit."""
    (tmp_path / "second.csv").write_text(_ADDITIVE)

    result = loaded.sources.ingest(
        IngestRequest(source="drop-box", object="second.csv", dataset="tx")
    )

    assert result.version == 2
    assert any("schema evolved" in warning for warning in result.warnings)


def test_dropping_every_column_is_refused_with_stock_settings(
    loaded: ServiceContext, tmp_path: Path
) -> None:
    """The regression. This passed before the fix, landing unrelated data as version 2."""
    (tmp_path / "other.csv").write_text(_UNRELATED)

    with pytest.raises(SchemaDriftError) as raised:
        loaded.sources.ingest(
            IngestRequest(source="drop-box", object="other.csv", dataset="tx")
        )

    assert "column(s) dropped" in str(raised.value)
    # The rejected load leaves no half-written version behind.
    assert len(loaded.datasets.versions("tx")) == 1


def test_a_refused_drift_is_a_client_error_not_a_server_fault(
    loaded: ServiceContext, tmp_path: Path
) -> None:
    """409, not the 500 IngestionError otherwise carries.

    The caller's data conflicts with the dataset; nothing on this side failed. A 500 pages
    someone and tells every client to retry a request that cannot succeed unchanged.
    """
    (tmp_path / "other.csv").write_text(_UNRELATED)

    with pytest.raises(SchemaDriftError) as raised:
        loaded.sources.ingest(
            IngestRequest(source="drop-box", object="other.csv", dataset="tx")
        )

    assert raised.value.http_status == 409


def test_the_caller_can_still_opt_into_a_breaking_change(
    loaded: ServiceContext, tmp_path: Path
) -> None:
    """Refusing by default is the point; refusing always would be a different tool."""
    (tmp_path / "other.csv").write_text(_UNRELATED)

    result = loaded.sources.ingest(
        IngestRequest(
            source="drop-box",
            object="other.csv",
            dataset="tx",
            allow_breaking_schema_change=True,
        )
    )

    assert result.version == 2


def test_evolution_can_be_switched_off_entirely(
    loaded: ServiceContext, tmp_path: Path
) -> None:
    """With evolution disabled, even an added column is refused."""
    loaded.platform.settings.ingestion.allow_schema_evolution = False
    (tmp_path / "second.csv").write_text(_ADDITIVE)

    with pytest.raises(SchemaDriftError) as raised:
        loaded.sources.ingest(
            IngestRequest(source="drop-box", object="second.csv", dataset="tx")
        )

    assert "evolution is disabled" in str(raised.value)
