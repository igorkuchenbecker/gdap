"""Profiling must describe data accurately — everything downstream trusts it."""

from __future__ import annotations

import datetime as dt

import polars as pl

from gdap.core.enums import DataClassification, SemanticType
from gdap.core.frames import schema_from_frame
from gdap.profiling import DataProfiler, discover_relationships
from gdap.profiling.semantics import enrich_schema_semantics, infer_semantic_type


def test_semantic_inference_recognises_common_meanings() -> None:
    cases = {
        "email": (["a@b.com", "c@d.org"], SemanticType.EMAIL),
        "revenue": ([10.5, 20.25], SemanticType.CURRENCY),
        "quantity": ([1, 2], SemanticType.QUANTITY),
        "order_date": ([dt.date(2026, 1, 1), dt.date(2026, 1, 2)], SemanticType.DATE),
        "ip": (["192.168.0.1", "10.0.0.1"], SemanticType.IP_ADDRESS),
        "is_active": ([True, False], SemanticType.BOOLEAN),
    }
    for name, (values, expected) in cases.items():
        assert infer_semantic_type(pl.Series(name, values)) is expected, name


def test_identifier_detection_needs_uniqueness() -> None:
    unique = pl.Series("customer_id", [f"C{i}" for i in range(50)])
    assert (
        infer_semantic_type(unique, distinct_ratio=1.0, is_unique=True) is SemanticType.IDENTIFIER
    )


def test_hex_like_codes_are_not_ip_addresses() -> None:
    codes = pl.Series("code", ["C001", "C002", "BEEF", "CAFE"])
    assert (
        infer_semantic_type(codes, distinct_ratio=1.0, is_unique=True)
        is not SemanticType.IP_ADDRESS
    )


def test_profile_reports_nulls_duplicates_and_keys(sales_frame: pl.DataFrame) -> None:
    profile = DataProfiler().profile(sales_frame, dataset="sales")

    assert profile.rows == sales_frame.height
    assert profile.duplicate_rows == 3
    revenue = profile.column("revenue")
    assert revenue is not None
    assert 0 < revenue.null_ratio < 0.1
    assert revenue.numeric is not None
    assert revenue.numeric.median is not None
    assert any("duplicate" in note for note in profile.recommendations)


def test_profile_classifies_sensitive_columns(sales_frame: pl.DataFrame) -> None:
    profile = DataProfiler().profile(sales_frame, dataset="sales")
    email = profile.column("email")
    assert email is not None
    assert email.semantic_type is SemanticType.EMAIL
    assert email.classification is DataClassification.RESTRICTED


def test_profile_samples_large_frames() -> None:
    frame = pl.DataFrame({"value": range(5_000)})
    profile = DataProfiler(sample_rows=1_000).profile(frame, dataset="big")
    assert profile.sampled
    assert profile.sample_rows == 1_000
    assert profile.rows == 5_000  # the true row count is never a sample artifact


def test_relationship_discovery_finds_foreign_keys() -> None:
    orders = pl.DataFrame(
        {"customer_id": [f"C{i % 10:03d}" for i in range(100)], "value": range(100)}
    )
    customers = pl.DataFrame(
        {"customer_id": [f"C{i:03d}" for i in range(10)], "name": list("abcdefghij")}
    )
    profiler = DataProfiler()
    profiles = {
        "orders": profiler.profile(orders, dataset="orders"),
        "customers": profiler.profile(customers, dataset="customers"),
    }
    hints = discover_relationships({"orders": orders, "customers": customers}, profiles)
    assert any(
        hint.right_dataset == "customers" and hint.overlap_ratio == 1.0 for hint in hints["orders"]
    )


def test_enrich_schema_semantics_fills_meanings_and_classification() -> None:
    frame = pl.DataFrame({"email": ["a@b.com"], "revenue": [1.0], "note": ["hello there"]})
    schema = enrich_schema_semantics(frame, schema_from_frame(frame))
    meanings = {column.name: column.semantic_type for column in schema.columns}
    assert meanings["email"] is SemanticType.EMAIL
    assert meanings["revenue"] is SemanticType.CURRENCY
    assert schema.column("email").classification is DataClassification.RESTRICTED  # type: ignore[union-attr]
