"""Policy engine (§38, §44).

Two questions, one place to answer them:

1. *May this operation run at all?*  → :meth:`PolicyEngine.decide`
2. *May it run without a human?*     → the returned :class:`ApprovalMode`

Compliance regimes are expressed as configuration (retention windows, classification rules,
approval thresholds), never as hardcoded legal logic — that is what keeps the platform usable in
different jurisdictions (§44).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from gdap.core.contracts import Principal
from gdap.core.enums import ApprovalMode, DataClassification, Permission
from gdap.core.errors import PolicyViolationError
from gdap.observability.logging import get_logger

log = get_logger(__name__)

#: Operations that change or destroy data. Anything not listed here is read-only by default.
MUTATING_OPERATIONS = {
    "dataset.write",
    "dataset.delete",
    "dataset.version.delete",
    "clean.apply",
    "sql.write",
    "sql.destructive",
    "pipeline.run",
    "model.promote",
    "source.delete",
    "retention.purge",
}

#: Operations that are never automatic, regardless of role.
ALWAYS_APPROVAL = {"dataset.delete", "sql.destructive", "retention.purge", "source.delete"}


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    allowed: bool
    approval: ApprovalMode
    reason: str
    required_permissions: tuple[Permission, ...] = ()
    details: dict[str, Any] = field(default_factory=dict)

    def enforce(self) -> None:
        if not self.allowed:
            raise PolicyViolationError(self.reason, details=self.details)

    @property
    def needs_human(self) -> bool:
        return self.approval is ApprovalMode.REQUIRES_APPROVAL


@dataclass(slots=True)
class PolicySettings:
    """Tenant-level knobs. Persisted in ``organizations.settings`` and overridable per pipeline."""

    auto_apply_cleaning: bool = True
    auto_apply_ai_suggestions: bool = False
    max_auto_delete_ratio: float = 0.05
    restricted_export_allowed: bool = False
    default_retention_days: int | None = None
    require_approval_above_classification: DataClassification = DataClassification.RESTRICTED

    @classmethod
    def from_mapping(cls, payload: dict[str, Any] | None) -> PolicySettings:
        payload = payload or {}
        known: dict[str, Any] = {
            key: payload[key]
            for key in (
                "auto_apply_cleaning",
                "auto_apply_ai_suggestions",
                "max_auto_delete_ratio",
                "restricted_export_allowed",
                "default_retention_days",
            )
            if key in payload
        }
        level = payload.get("require_approval_above_classification")
        if level:
            known["require_approval_above_classification"] = DataClassification(level)
        return cls(**known)


class PolicyEngine:
    def __init__(self, settings: PolicySettings | None = None) -> None:
        self.settings = settings or PolicySettings()

    def decide(
        self,
        principal: Principal,
        operation: str,
        *,
        classification: DataClassification = DataClassification.INTERNAL,
        affected_ratio: float = 0.0,
        origin: str = "deterministic",
        context: dict[str, Any] | None = None,
    ) -> PolicyDecision:
        context = context or {}
        required = _permissions_for(operation)
        missing = [p for p in required if not principal.has(p)]
        if missing:
            return PolicyDecision(
                allowed=False,
                approval=ApprovalMode.BLOCKED,
                reason=f"'{operation}' requires: {', '.join(p.value for p in missing)}",
                required_permissions=tuple(required),
                details={"operation": operation, "role": principal.role.value},
            )

        if operation in ALWAYS_APPROVAL:
            return PolicyDecision(
                allowed=True,
                approval=ApprovalMode.REQUIRES_APPROVAL,
                reason=f"'{operation}' always requires explicit human approval",
                required_permissions=tuple(required),
            )

        if operation not in MUTATING_OPERATIONS:
            return PolicyDecision(True, ApprovalMode.AUTO, "read-only operation")

        if origin == "ai_suggested" and not self.settings.auto_apply_ai_suggestions:
            return PolicyDecision(
                allowed=True,
                approval=ApprovalMode.REQUIRES_APPROVAL,
                reason="AI-suggested changes require review (auto_apply_ai_suggestions=false)",
                details={"operation": operation, "origin": origin},
            )

        if classification.rank >= self.settings.require_approval_above_classification.rank:
            return PolicyDecision(
                allowed=True,
                approval=ApprovalMode.REQUIRES_APPROVAL,
                reason=f"data classified {classification.value} requires approval to modify",
                details={"classification": classification.value},
            )

        if affected_ratio > self.settings.max_auto_delete_ratio and operation in {
            "clean.apply",
            "dataset.write",
        }:
            return PolicyDecision(
                allowed=True,
                approval=ApprovalMode.REQUIRES_APPROVAL,
                reason=(
                    f"operation affects {affected_ratio:.1%} of rows, above the "
                    f"{self.settings.max_auto_delete_ratio:.1%} auto threshold"
                ),
                details={"affected_ratio": affected_ratio},
            )

        if operation == "clean.apply" and not self.settings.auto_apply_cleaning:
            return PolicyDecision(
                True, ApprovalMode.AUTO_WITH_VALIDATION, "cleaning runs with post-validation"
            )

        return PolicyDecision(True, ApprovalMode.AUTO, "within automatic limits")

    def may_export(
        self, principal: Principal, classification: DataClassification
    ) -> PolicyDecision:
        if (
            classification.rank >= DataClassification.RESTRICTED.rank
            and not self.settings.restricted_export_allowed
        ):
            return PolicyDecision(
                allowed=False,
                approval=ApprovalMode.BLOCKED,
                reason=f"exporting {classification.value} data is disabled for this organization",
                details={"classification": classification.value},
            )
        return PolicyDecision(True, ApprovalMode.AUTO, "export allowed")


def _permissions_for(operation: str) -> list[Permission]:
    mapping = {
        "dataset.write": [Permission.DATASET_WRITE],
        "dataset.delete": [Permission.DATASET_WRITE, Permission.ADMIN],
        "dataset.version.delete": [Permission.DATASET_WRITE],
        "clean.apply": [Permission.DATASET_WRITE],
        "sql.write": [Permission.SQL_WRITE],
        "sql.destructive": [Permission.SQL_DESTRUCTIVE],
        "pipeline.run": [Permission.PIPELINE_RUN],
        "pipeline.write": [Permission.PIPELINE_WRITE],
        "source.write": [Permission.SOURCE_WRITE],
        "source.delete": [Permission.SOURCE_WRITE],
        "model.promote": [Permission.DATASET_WRITE],
        "retention.purge": [Permission.ADMIN],
        "agent.use": [Permission.AGENT_USE],
    }
    return mapping.get(operation, [])
