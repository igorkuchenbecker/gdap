"""Shared test fixtures.

Every test runs against a throwaway platform: its own SQLite database, warehouse and artifact
store under a temporary directory. Nothing touches the developer's ``~/.gdap``.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import polars as pl
import pytest

os.environ.setdefault("GDAP_ENVIRONMENT", "testing")

from gdap.core.config import PathsSettings, Settings, reset_settings  # noqa: E402
from gdap.core.container import Platform, reset_platform  # noqa: E402
from gdap.core.contracts import Principal, SourceSpec  # noqa: E402
from gdap.core.enums import SourceType  # noqa: E402
from gdap.core.services.context import ServiceContext  # noqa: E402
from gdap.demo import generate_demo_files  # noqa: E402
from gdap.ingestion import IngestRequest  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_settings() -> Iterator[None]:
    reset_settings()
    yield
    reset_settings()
    reset_platform()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        environment="testing",
        paths=PathsSettings(home=tmp_path / "gdap"),
        database={"url": f"sqlite:///{tmp_path / 'gdap' / 'test.db'}"},  # type: ignore[arg-type]
    )


@pytest.fixture
def platform(settings: Settings) -> Iterator[Platform]:
    instance = Platform(settings)
    instance.bootstrap()
    yield instance
    instance.shutdown()


@pytest.fixture
def principal(platform: Platform) -> Principal:
    with platform.db.session() as session:
        return platform.resolve_principal(session)


@pytest.fixture
def context(platform: Platform, principal: Principal) -> Iterator[ServiceContext]:
    """A service graph inside one transaction — the unit of work a request would use."""
    with platform.unit_of_work(principal) as ctx:
        yield ctx


@pytest.fixture(scope="session")
def demo_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Demo CSVs generated once per test session (deterministic seed)."""
    directory = tmp_path_factory.mktemp("demo-data")
    generate_demo_files(directory, days=120, seed=7, customers=40, products=15, orders_per_day=6)
    return directory


@pytest.fixture
def sales_frame() -> pl.DataFrame:
    """A small, deliberately imperfect frame used across analytics tests."""
    import datetime as dt
    import math
    import random

    rng = random.Random(11)
    rows: list[dict[str, Any]] = []
    for index in range(360):
        day = dt.date(2025, 1, 1) + dt.timedelta(days=index)
        for region in ("North", "South", "East"):
            multiplier = {"North": 1.4, "South": 1.0, "East": 0.7}[region]
            revenue = (
                (500 + index * 3)
                * (1 + 0.2 * math.sin(index / 20))
                * multiplier
                * rng.uniform(0.85, 1.15)
            )
            rows.append(
                {
                    "order_id": f"O{len(rows):05d}",
                    "order_date": day,
                    "region": region if rng.random() > 0.05 else f"{region} ",
                    "revenue": round(revenue, 2) if rng.random() > 0.04 else None,
                    "quantity": rng.randint(1, 12),
                    "email": f"user{len(rows)}@example.com",
                }
            )
    frame = pl.DataFrame(rows)
    return pl.concat([frame, frame.head(3)])  # a few exact duplicates


@pytest.fixture
def loaded_context(context: ServiceContext, demo_dir: Path) -> ServiceContext:
    """A context with the demo transactions already ingested."""
    context.sources.register(
        SourceSpec(
            name="demo_files",
            type=SourceType.FILE,
            connector="file.csv",
            config={"path": str(demo_dir), "pattern": "*.csv"},
        )
    )
    context.sources.ingest(
        IngestRequest(source="demo_files", object="transactions.csv", dataset="transactions")
    )
    return context


@pytest.fixture
def api_client(platform: Platform) -> Iterator[Any]:
    """FastAPI TestClient wired to the test platform (auth disabled in the testing profile)."""
    from fastapi.testclient import TestClient

    import gdap.core.container as container_module
    from gdap.api.app import create_app

    container_module._PLATFORM = platform
    with TestClient(create_app(platform=platform)) as client:
        yield client
    container_module._PLATFORM = None
