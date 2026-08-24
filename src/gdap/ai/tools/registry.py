"""Tool registry and guarded execution.

Every capability an agent has is a registered tool with:

* a JSON schema the model sees;
* the permissions the *caller* must hold (tools never escalate privilege);
* an approval mode — outward-facing or destructive tools are never auto-executed;
* an audit record of the call, its arguments and its outcome.

An agent that is not granted a tool cannot call it: the allow-list is enforced here, not in the
prompt. Prompts are advice; this is enforcement.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from gdap.core.contracts import Evidence, ToolResult, ToolSpec
from gdap.core.enums import ApprovalMode, Permission
from gdap.core.errors import (
    GdapError,
    NotFoundError,
    PluginError,
    ToolNotAllowedError,
    ValidationFailedError,
)
from gdap.observability.logging import get_logger
from gdap.observability.metrics import METRICS
from gdap.security.rbac import require

if TYPE_CHECKING:  # pragma: no cover
    from gdap.core.services.context import ServiceContext

log = get_logger(__name__)

#: Tool output handed back to the model is truncated to keep the context bounded and the cost
#: predictable. The full result stays in the ToolResult for the API response and the audit trail.
MAX_CONTENT_CHARS = 6_000


@dataclass(slots=True)
class ToolContext:
    """What a tool may use. Deliberately just the service graph plus run scope."""

    services: ServiceContext
    dataset: str | None = None
    job_id: str | None = None
    approved_tools: set[str] = field(default_factory=set)
    max_rows: int = 1_000
    question: str | None = None  # the user's wording, used to disambiguate column choices
    _hints_cache: dict[str, dict[str, str | None]] = field(default_factory=dict, repr=False)

    def default_dataset(self, provided: str | None = None) -> str:
        name = provided or self.dataset
        if not name:
            available = [row.name for row in self.services.datasets.repo.list(limit=25)]
            raise ValidationFailedError(
                "no dataset specified",
                details={"hint": "pass 'dataset'", "available": available},
            )
        return name

    def hints(self, dataset: str | None = None) -> dict[str, str | None]:
        """Best-guess metric / dimension / time column for a dataset.

        Models omit optional arguments constantly, and a deterministic planner has none to give.
        Rather than failing — or silently measuring whichever numeric column happens to come first
        — tools resolve the missing argument by scoring the dataset's *semantic* schema against
        the user's own wording. The choice is always reported in the evidence.
        """
        name = self.default_dataset(dataset)
        cache_key = f"{name}::{(self.question or '')[:120]}"
        if cache_key in self._hints_cache:
            return self._hints_cache[cache_key]

        schema = self.services.datasets.schema(name)
        question = (self.question or "").lower()
        hints = {
            "metric": _best_metric(schema, question),
            "dimension": _best(schema, {"categorical", "ordinal", "country"}, question),
            "time_column": _best(schema, {"date", "datetime", "timestamp"}, question),
        }
        self._hints_cache[cache_key] = hints
        return hints


#: Words that mark a column as the thing a business question is usually *about*, versus a
#: supporting attribute (a unit price is rarely the answer to "how did revenue move?").
_PRIMARY_MEASURE = re.compile(
    r"(?i)(revenue|receita|faturamento|amount|valor|sales|vendas|total|net|gross|profit|margin|score|balance)"
)
_SUPPORTING_MEASURE = re.compile(
    r"(?i)(unit_|_id$|^id$|price|cost|rate|pct|percent|discount|index|version|_key$)"
)

#: Question wording that points at a *kind* of measure rather than a column name.
_MEANING_SYNONYMS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?i)\b(units?|volume|qty|quantit\w*|pieces|itens|unidades)\b"), "quantity"),
    (
        re.compile(r"(?i)\b(revenue|receita|faturamento|sales|vendas|money|money|billing)\b"),
        "currency",
    ),
    (re.compile(r"(?i)\b(rate|percent\w*|share|taxa|percentual)\b"), "percentage"),
]


def _best(schema: Any, meanings: set[str], question: str) -> str | None:
    """Pick the column of the requested meaning that the question most plausibly refers to."""
    candidates = [column for column in schema.columns if column.semantic_type.value in meanings]
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0].name

    def score(column: Any) -> tuple[int, int]:
        points = 0
        if question and column.name.lower() in question:
            points += 10
        if column.semantic_type.value == "date":
            points += 1  # a date column is a safer time axis than a raw timestamp
        return (points, -len(column.name))

    return max(candidates, key=score).name


def _best_metric(schema: Any, question: str) -> str | None:
    numeric = [
        column
        for column in schema.columns
        if column.semantic_type.value in {"currency", "quantity", "numeric", "percentage"}
    ]
    if not numeric:
        return None

    def score(column: Any) -> tuple[int, int]:
        points = 0
        name = column.name.lower()
        if question and name in question:
            points += 10
        if _PRIMARY_MEASURE.search(name):
            points += 4
        if _SUPPORTING_MEASURE.search(name):
            points -= 3
        if column.semantic_type.value == "currency":
            points += 2
        elif column.semantic_type.value == "quantity":
            points += 1
        for pattern, meaning in _MEANING_SYNONYMS:
            if question and pattern.search(question) and column.semantic_type.value == meaning:
                points += 6
        return (points, -len(name))

    return max(numeric, key=score).name


ToolHandler = Callable[..., Any]


@dataclass(slots=True)
class AgentTool:
    spec: ToolSpec
    handler: ToolHandler
    category: str = "data"

    @property
    def name(self) -> str:
        return self.spec.name


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, AgentTool] = {}

    def register(self, agent_tool: AgentTool, *, replace: bool = False) -> None:
        if agent_tool.name in self._tools and not replace:
            raise PluginError(f"tool '{agent_tool.name}' is already registered")
        self._tools[agent_tool.name] = agent_tool

    def get(self, name: str) -> AgentTool:
        if name not in self._tools:
            raise NotFoundError(
                f"unknown tool '{name}'", details={"available": sorted(self._tools)}
            )
        return self._tools[name]

    def names(self) -> list[str]:
        return sorted(self._tools)

    def specs(self, allowed: set[str] | None = None) -> list[ToolSpec]:
        return [
            agent_tool.spec
            for name, agent_tool in sorted(self._tools.items())
            if allowed is None or name in allowed
        ]

    def catalog(self) -> list[dict[str, Any]]:
        return [
            {
                "name": agent_tool.name,
                "description": agent_tool.spec.description,
                "category": agent_tool.category,
                "read_only": agent_tool.spec.read_only,
                "approval": agent_tool.spec.approval.value,
                "permissions": [p.value for p in agent_tool.spec.required_permissions],
                "parameters": agent_tool.spec.parameters,
            }
            for agent_tool in sorted(self._tools.values(), key=lambda t: t.name)
        ]

    # ------------------------------------------------------------------ execution
    def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        context: ToolContext,
        *,
        allowed: set[str] | None = None,
        call_id: str | None = None,
        agent: str = "agent",
    ) -> ToolResult:
        started = time.perf_counter()
        principal = context.services.principal

        try:
            if allowed is not None and name not in allowed:
                raise ToolNotAllowedError(
                    f"agent '{agent}' may not use tool '{name}'",
                    details={"allowed": sorted(allowed)},
                )
            agent_tool = self.get(name)
            spec = agent_tool.spec

            if spec.required_permissions:
                require(principal, *spec.required_permissions, resource=f"tool:{name}")
            if spec.approval is ApprovalMode.BLOCKED:
                raise ToolNotAllowedError(f"tool '{name}' is disabled by policy")
            if (
                spec.approval is ApprovalMode.REQUIRES_APPROVAL
                and name not in context.approved_tools
            ):
                raise ToolNotAllowedError(
                    f"tool '{name}' requires explicit human approval before an agent may run it",
                    details={"approval": spec.approval.value},
                )

            payload = agent_tool.handler(context, **_clean(arguments))
            content, evidence = _unpack(payload)
            duration_ms = (time.perf_counter() - started) * 1000

            self._audit(
                context, agent, name, arguments, "success", {"duration_ms": round(duration_ms, 1)}
            )
            METRICS.increment("agent_tool_calls_total", tool=name, result="success")
            METRICS.observe("agent_tool_ms", duration_ms, tool=name)
            log.info("agent_tool_called", agent=agent, tool=name, duration_ms=round(duration_ms, 1))

            return ToolResult(
                tool=name,
                call_id=call_id,
                ok=True,
                content=content,
                duration_ms=duration_ms,
                evidence=evidence,
            )

        except GdapError as exc:
            duration_ms = (time.perf_counter() - started) * 1000
            self._audit(
                context,
                agent,
                name,
                arguments,
                "denied" if isinstance(exc, ToolNotAllowedError) else "error",
                {"error": exc.message},
            )
            METRICS.increment("agent_tool_calls_total", tool=name, result="error")
            log.warning(
                "agent_tool_failed", agent=agent, tool=name, code=exc.code, error=exc.message
            )
            return ToolResult(
                tool=name, call_id=call_id, ok=False, error=exc.message, duration_ms=duration_ms
            )
        except Exception as exc:
            duration_ms = (time.perf_counter() - started) * 1000
            self._audit(context, agent, name, arguments, "error", {"error": str(exc)})
            METRICS.increment("agent_tool_calls_total", tool=name, result="crash")
            log.exception("agent_tool_crashed", agent=agent, tool=name, error=str(exc))
            return ToolResult(
                tool=name,
                call_id=call_id,
                ok=False,
                error=f"{type(exc).__name__}: {exc}",
                duration_ms=duration_ms,
            )

    def _audit(
        self,
        context: ToolContext,
        agent: str,
        name: str,
        arguments: dict[str, Any],
        result: str,
        details: dict[str, Any],
    ) -> None:
        context.services.audit.record(
            context.services.principal,
            "agent.tool_call",
            "tool",
            name,
            result=result,  # type: ignore[arg-type]
            details={"agent": agent, "arguments": _truncate_args(arguments), **details},
        )


def tool(
    name: str,
    description: str,
    *,
    parameters: dict[str, Any] | None = None,
    permissions: list[Permission] | None = None,
    approval: ApprovalMode = ApprovalMode.AUTO,
    read_only: bool = True,
    category: str = "data",
    registry: ToolRegistry | None = None,
) -> Callable[[ToolHandler], ToolHandler]:
    """Register a function as an agent tool."""

    def decorator(handler: ToolHandler) -> ToolHandler:
        spec = ToolSpec(
            name=name,
            description=description,
            parameters=parameters or {"type": "object", "properties": {}},
            required_permissions=permissions or [],
            approval=approval,
            read_only=read_only,
        )
        (registry or _REGISTRY).register(
            AgentTool(spec=spec, handler=handler, category=category), replace=True
        )
        return handler

    return decorator


_REGISTRY = ToolRegistry()


def get_tool_registry() -> ToolRegistry:
    if not _REGISTRY.names():
        from gdap.ai.tools import data_tools  # noqa: F401  (import registers the built-ins)
    return _REGISTRY


# ─────────────────────────────────────────── helpers ───────────────────────────────────────


def _clean(arguments: dict[str, Any]) -> dict[str, Any]:
    """Drop nulls so tool defaults apply, and reject non-string keys."""
    return {str(key): value for key, value in (arguments or {}).items() if value is not None}


def _unpack(payload: Any) -> tuple[Any, Evidence | None]:
    if (
        isinstance(payload, tuple)
        and len(payload) == 2
        and isinstance(payload[1], Evidence | type(None))
    ):
        return payload[0], payload[1]
    return payload, None


def _truncate_args(arguments: dict[str, Any]) -> dict[str, Any]:
    rendered = {}
    for key, value in (arguments or {}).items():
        text = str(value)
        rendered[key] = text if len(text) <= 300 else text[:300] + "…"
    return rendered


def render_for_model(content: Any) -> str:
    """Serialise a tool result for the model, bounded in size and never lying about truncation."""
    if isinstance(content, str):
        text = content
    else:
        try:
            text = json.dumps(content, indent=2, default=str, ensure_ascii=False)
        except (TypeError, ValueError):
            text = str(content)
    if len(text) <= MAX_CONTENT_CHARS:
        return text
    return (
        text[:MAX_CONTENT_CHARS]
        + f"\n… [truncated: {len(text) - MAX_CONTENT_CHARS} more characters]"
    )
