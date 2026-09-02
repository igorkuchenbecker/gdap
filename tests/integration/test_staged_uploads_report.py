"""Staged uploads are surfaced, not silently accumulated — and not silently deleted.

Every upload leaves its file in staging and nothing removes it: this platform reports
retention candidates and leaves acting on them to a human (§38), and `retention.purge` is an
always-approval operation. What was missing was the report — the only way to learn how much
upload traffic a deployment had accumulated was to look at the disk.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.integration

_CSV = b"id,name\n1,alpha\n2,beta\n"


def test_nothing_is_reported_before_anything_is_uploaded(api_client: Any) -> None:
    body = api_client.get("/api/v1/retention/uploads").json()

    assert body["items"] == []
    assert body["count"] == 0
    assert body["total_bytes"] == 0


def test_an_upload_is_reported_with_its_owner_and_size(api_client: Any) -> None:
    created = api_client.post(
        "/api/v1/sources/upload",
        files={"file": ("ledger.csv", _CSV, "text/csv")},
    ).json()

    body = api_client.get("/api/v1/retention/uploads").json()

    assert body["count"] == 1
    assert body["total_bytes"] == len(_CSV)
    assert body["orphaned"] == 0

    entry = body["items"][0]
    assert entry["source"] == created["source"]["name"]
    assert entry["orphaned"] is False
    assert entry["key"].endswith("ledger.csv")
    # A path relative to staging, not an absolute one: the key identifies the object under any
    # backend, while the absolute path is a detail of this host.
    assert not entry["key"].startswith("/")
    assert entry["age_days"] >= 0


def test_every_upload_is_counted_because_none_are_ever_removed(api_client: Any) -> None:
    """The accumulation this report exists to make visible."""
    for _ in range(3):
        api_client.post(
            "/api/v1/sources/upload",
            files={"file": ("same-name.csv", _CSV, "text/csv")},
        )

    body = api_client.get("/api/v1/retention/uploads").json()

    assert body["count"] == 3, "re-uploading a filename keeps every copy"
    assert body["total_bytes"] == len(_CSV) * 3


def test_a_file_no_source_points_at_is_flagged_as_orphaned(api_client: Any) -> None:
    """No path produces one today, so a non-empty count is itself the finding."""
    api_client.post(
        "/api/v1/sources/upload",
        files={"file": ("owned.csv", _CSV, "text/csv")},
    )
    owned_path = Path(api_client.get("/api/v1/sources").json()["items"][0]["config"]["path"])

    # Beside the owned upload, inside the same uploads/ root, with no source pointing at it.
    stray = owned_path.parent.parent / "stray-upload" / "leftover.csv"
    stray.parent.mkdir(parents=True, exist_ok=True)
    stray.write_bytes(_CSV)

    body = api_client.get("/api/v1/retention/uploads").json()

    assert body["count"] == 2
    assert body["orphaned"] == 1
    orphan = next(item for item in body["items"] if item["orphaned"])
    assert orphan["source"] is None
    assert orphan["key"].endswith("leftover.csv")
