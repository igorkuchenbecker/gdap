"""Step registry and the execution context handed to every step.

A step is a small, typed, side-effect-declaring unit: it receives frames and parameters, returns a
frame plus metrics/artifacts/insights, and declares whether it is read-only and what approval it
needs. Third-party steps register through the ``gdap.pipeline_steps`` entry point group (§33).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from typing import TYPE_CHECKING, Any

import polars as pl

from gdap.core.contracts import Insight, Principal, StepSpec
from gdap.core.enums import ApprovalMode
from gdap.core.errors import NotFoundError, PluginError, ValidationFailedError
from gdap.observability.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from gdap.core.services.context import ServiceContext

log = get_logger(__name__)

ENTRY_POINT_GROUP = "gdap.pipeline_steps"


@dataclass(slots=True)
class StepOutcome:
    """What a step produced. Everything is optional except the fact that it ran."""

    frame: pl.DataFrame | None = None
    message: str = ""
    metrics: dict[str, Any] = field(default_factory=dict)
    artifacts: list[str] = field(default_factory=list)
    insights: list[Insight] = field(default_factory=list)
    outputs: dict[str, pl.DataFrame] = field(default_factory=dict)
    requires_approval: str | None = None  # set to request a human decision


@dataclass(slots=True)
class StepContext:
    """Everything a step is allowed to touch — deliberately narrow."""

    services: ServiceContext
    params: dict[str, Any]
    frames: dict[str, pl.DataFrame] = field(default_factory=dict)
    current: str | None = None
    job_id: str | None = None
    pipeline: str = ""
    approved_steps: set[str] = field(default_factory=set)
    metrics: dict[str, Any] = field(default_factory=dict)
    artifacts: list[str] = field(default_factory=list)
    insights: list[Insight] = field(default_factory=list)
    analyses: list[Any] = field(default_factory=list)  # AnalysisResult, kept for the report step
    datasets_written: list[str] = field(default_factory=list)
    dry_run: bool = False

    @property
    def principal(self) -> Principal:
        return self.services.principal

    def frame(self, name: str | None = None) -> pl.DataFrame:
        """Resolve the frame a step should operate on (explicit name, else the current one)."""
        key = name or self.current
        if key is None or key not in self.frames:
            raise ValidationFailedError(
                "no input frame available — add a read step before this one",
                details={"requested": key, "available": sorted(self.frames)},
            )
        return self.frames[key]

    def publish(self, name: str, frame: pl.DataFrame) -> None:
        self.frames[name] = frame
        self.current = name

    def option(
        self, step: StepSpec, key: str, default: Any = None, *, required: bool = False
    ) -> Any:
        value = step.with_.get(key, default)
        if required and value is None:
            raise ValidationFailedError(
                f"step '{step.uses}' requires option '{key}'",
                details={"provided": sorted(step.with_)},
            )
        return value


StepHandler = Callable[[StepContext, StepSpec], StepOutcome]


@dataclass(slots=True)
class StepDefinition:
    key: str
    handler: StepHandler
    description: str = ""
    approval: ApprovalMode = ApprovalMode.AUTO
    read_only: bool = True
    options: dict[str, str] = field(default_factory=dict)
    category: str = "general"

    def describe(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "description": self.description,
            "category": self.category,
            "read_only": self.read_only,
            "approval": self.approval.value,
            "options": self.options,
        }


_REGISTRY: dict[str, StepDefinition] = {}
_EXTERNAL_LOADED = False


def register_step(
    key: str,
    *,
    description: str = "",
    approval: ApprovalMode = ApprovalMode.AUTO,
    read_only: bool = True,
    options: dict[str, str] | None = None,
    category: str = "general",
    replace: bool = False,
) -> Callable[[StepHandler], StepHandler]:
    def decorator(handler: StepHandler) -> StepHandler:
        if key in _REGISTRY and not replace:
            raise PluginError(f"step '{key}' is already registered")
        _REGISTRY[key] = StepDefinition(
            key=key,
            handler=handler,
            description=description,
            approval=approval,
            read_only=read_only,
            options=options or {},
            category=category,
        )
        return handler

    return decorator


def get_step(key: str) -> StepDefinition:
    _load_external()
    definition = _REGISTRY.get(key)
    if definition is None:
        raise NotFoundError(
            f"unknown pipeline step '{key}'",
            details={"available": known_steps()},
        )
    return definition


def known_steps() -> list[str]:
    _load_external()
    return sorted(_REGISTRY)


def step_catalog() -> list[dict[str, Any]]:
    _load_external()
    return [definition.describe() for definition in sorted(_REGISTRY.values(), key=lambda d: d.key)]


def _load_external() -> None:
    global _EXTERNAL_LOADED
    if _EXTERNAL_LOADED:
        return
    _EXTERNAL_LOADED = True
    try:
        discovered = entry_points(group=ENTRY_POINT_GROUP)
    except Exception:  # pragma: no cover
        return
    for entry in discovered:
        try:
            entry.load()  # the module registers its steps on import
            log.info("external_steps_loaded", plugin=entry.name)
        except Exception as exc:
            log.error("external_steps_failed", plugin=entry.name, error=str(exc))
