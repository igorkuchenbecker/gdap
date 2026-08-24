"""Warehouse immutability, storage safety and schedule arithmetic."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import polars as pl
import pytest

from gdap.core.contracts import ScheduleSpec
from gdap.core.errors import StorageError, ValidationFailedError
from gdap.pipelines.schedule import describe, next_run, parse_interval
from gdap.storage.backends import LocalFileStorage
from gdap.storage.warehouse import Warehouse


@pytest.fixture
def warehouse(tmp_path: Path) -> Warehouse:
    return Warehouse(LocalFileStorage(tmp_path / "warehouse"))


def test_versions_are_immutable_and_checksummed(warehouse: Warehouse) -> None:
    frame = pl.DataFrame({"a": range(100), "b": ["x"] * 100})
    first = warehouse.write_frame("org", "sales", 1, frame)
    second = warehouse.write_frame("org", "sales", 2, frame.head(50))

    assert first.rows == 100 and second.rows == 50
    assert first.uri != second.uri
    assert warehouse.read(first.uri).height == 100  # v1 is untouched by writing v2
    assert len(first.checksum) == 64


def test_manifest_records_provenance(warehouse: Warehouse) -> None:
    warehouse.write_frame("org", "sales", 1, pl.DataFrame({"a": [1]}), metadata={"source": "csv"})
    manifest = warehouse.manifest("org", "sales", 1)
    assert manifest["rows"] == 1
    assert manifest["source"] == "csv"
    assert manifest["compression"] == "zstd"
    assert manifest["schema_fingerprint"]


def test_chunked_write_produces_one_version(warehouse: Warehouse) -> None:
    chunks = (pl.DataFrame({"a": [i]}) for i in range(10))
    result = warehouse.write("org", "chunked", 1, chunks)
    assert result.rows == 10
    assert warehouse.read(result.uri).height == 10


def test_scan_is_lazy_and_supports_projection(warehouse: Warehouse) -> None:
    result = warehouse.write_frame("org", "wide", 1, pl.DataFrame({"a": [1, 2], "b": [3, 4]}))
    lazy = warehouse.scan(result.uri)
    assert isinstance(lazy, pl.LazyFrame)
    assert warehouse.read(result.uri, columns=["a"]).columns == ["a"]


def test_storage_rejects_path_traversal(tmp_path: Path) -> None:
    storage = LocalFileStorage(tmp_path)
    with pytest.raises(StorageError):
        storage.write_bytes("../escape.txt", b"nope")
    with pytest.raises(StorageError):
        storage.read_bytes("missing.txt")


def test_interval_parsing() -> None:
    assert parse_interval("5m").total_seconds() == 300
    assert parse_interval("2 hours").total_seconds() == 7200
    assert parse_interval("daily").days == 1
    with pytest.raises(ValidationFailedError):
        parse_interval("whenever")


def test_cron_respects_timezone() -> None:
    now = datetime(2026, 8, 24, 10, 30, tzinfo=UTC)
    utc = next_run(ScheduleSpec(cron="0 6 * * *"), after=now)
    sao_paulo = next_run(ScheduleSpec(cron="0 6 * * *", timezone="America/Sao_Paulo"), after=now)
    assert utc is not None and sao_paulo is not None
    assert utc.hour == 6
    assert sao_paulo.hour == 9  # 06:00 UTC-3 expressed in UTC


def test_disabled_schedule_has_no_next_run() -> None:
    assert next_run(ScheduleSpec(cron="* * * * *", enabled=False)) is None
    assert describe(ScheduleSpec(every="5m")) == "every 5m (UTC)"


def test_invalid_cron_and_timezone_are_rejected() -> None:
    with pytest.raises(ValidationFailedError):
        next_run(ScheduleSpec(cron="not a cron"))
    with pytest.raises(ValidationFailedError):
        next_run(ScheduleSpec(cron="0 6 * * *", timezone="Mars/Olympus"))
