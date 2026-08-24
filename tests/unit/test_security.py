"""Security primitives: keys, roles, secrets and masking."""

from __future__ import annotations

import polars as pl
import pytest

from gdap.core.config import Settings
from gdap.core.contracts import ColumnSchema, DatasetSchema, Principal
from gdap.core.enums import DataClassification, Permission, Role, SemanticType
from gdap.core.errors import AuthorizationError, ConfigurationError
from gdap.security import api_keys
from gdap.security.masking import apply_masking, mask_value, pseudonymize
from gdap.security.rbac import permissions_for, require, require_same_tenant
from gdap.security.secrets import SecretsResolver


def test_api_key_roundtrip_and_rejection() -> None:
    issued = api_keys.generate()
    assert issued.plaintext.startswith("gdap_")
    assert api_keys.verify(issued.plaintext, issued.key_hash)
    assert not api_keys.verify("gdap_deadbeef_wrongsecret", issued.key_hash)
    assert not api_keys.verify("not-a-key", issued.key_hash)


def test_api_key_hash_never_contains_the_secret() -> None:
    issued = api_keys.generate()
    _prefix, secret = api_keys.split(issued.plaintext)
    assert secret is not None and secret not in issued.key_hash


def test_role_permissions_are_ordered_by_privilege() -> None:
    viewer = permissions_for(Role.VIEWER)
    analyst = permissions_for(Role.ANALYST)
    engineer = permissions_for(Role.ENGINEER)
    admin = permissions_for(Role.ADMIN)
    assert viewer < analyst < engineer < admin
    assert Permission.SQL_DESTRUCTIVE not in engineer
    assert Permission.SQL_DESTRUCTIVE in admin


def test_require_raises_for_missing_permission() -> None:
    analyst = Principal(
        org_id="o", user_id="u", role=Role.ANALYST, permissions=permissions_for(Role.ANALYST)
    )
    require(analyst, Permission.ANALYSIS_RUN)
    with pytest.raises(AuthorizationError, match="sql:destructive"):
        require(analyst, Permission.SQL_DESTRUCTIVE)


def test_tenant_isolation_assertion() -> None:
    principal = Principal(org_id="org-a", user_id="u", role=Role.ADMIN)
    with pytest.raises(AuthorizationError, match="another organization"):
        require_same_tenant(principal, "org-b", resource="dataset")


def test_secret_resolution_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GDAP_TEST_SECRET", "s3cr3t")
    resolver = SecretsResolver(Settings())
    assert resolver.resolve("env:GDAP_TEST_SECRET") == "s3cr3t"
    with pytest.raises(ConfigurationError, match="not set"):
        resolver.resolve("env:DOES_NOT_EXIST_XYZ")
    with pytest.raises(ConfigurationError, match="reference"):
        resolver.resolve("just-a-value")


def test_literal_secrets_are_forbidden_in_production() -> None:
    production = Settings(environment="production")
    with pytest.raises(ConfigurationError, match="forbidden in production"):
        SecretsResolver(production).resolve("literal:oops")


def test_redaction_hides_credentials() -> None:
    redacted = SecretsResolver.redact(
        {"host": "db", "password": "abc", "nested": {"api_key": "xyz"}}
    )
    assert redacted["host"] == "db"
    assert redacted["password"] == "***"
    assert redacted["nested"]["api_key"] == "***"  # type: ignore[index]


def test_masking_applies_to_sensitive_columns_only() -> None:
    frame = pl.DataFrame({"email": ["igor@example.com"], "region": ["North"]})
    schema = DatasetSchema(
        columns=[
            ColumnSchema(
                name="email",
                dtype="String",
                semantic_type=SemanticType.EMAIL,
                classification=DataClassification.RESTRICTED,
            ),
            ColumnSchema(name="region", dtype="String"),
        ]
    )
    masked = apply_masking(frame, schema)
    assert masked["email"][0] != "igor@example.com"
    assert "@example.com" in masked["email"][0]
    assert masked["region"][0] == "North"


def test_masking_can_be_disabled_explicitly() -> None:
    frame = pl.DataFrame({"email": ["igor@example.com"]})
    schema = DatasetSchema(
        columns=[ColumnSchema(name="email", dtype="String", semantic_type=SemanticType.EMAIL)]
    )
    assert apply_masking(frame, schema, enabled=False)["email"][0] == "igor@example.com"


def test_pseudonymisation_is_stable_and_irreversible() -> None:
    first = pseudonymize("igor@example.com", salt="tenant-a")
    second = pseudonymize("igor@example.com", salt="tenant-a")
    other_tenant = pseudonymize("igor@example.com", salt="tenant-b")
    assert first == second
    assert first != other_tenant
    assert "igor" not in first


def test_mask_value_keeps_shape_without_content() -> None:
    assert mask_value("12345678", SemanticType.PHONE).endswith("78")
    assert mask_value(None, SemanticType.EMAIL) is None
