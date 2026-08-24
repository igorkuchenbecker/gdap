"""Agent runtime.

Implements the loop required by §14::

    Agent → Planner → Tool Selection → Execution → Observation → Validation → Final Answer

Design commitments:

* **The loop lives here, not in the SDK.** Approval gates, the tool allow-list, the audit trail
  and evidence capture are platform concerns; a vendor helper cannot enforce them.
* **No claim without evidence.** Every tool result contributes an :class:`Evidence` record, and an
  answer produced without a single successful tool call is reported as such instead of narrated.
* **Bounded.** Iterations, tool calls and result sizes are all capped, so a confused model costs a
  bounded amount of money and time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from gdap.ai.tools.registry import ToolContext, ToolRegistry, render_for_model
from gdap.core.contracts import (
    AgentAnswer,
    ChartSpec,
    Evidence,
    Insight,
    ToolCall,
    ToolResult,
)
from gdap.core.enums import InsightKind, Severity
from gdap.observability.logging import get_logger, log_context
from gdap.observability.metrics import METRICS

log = get_logger(__name__)

BASE_SYSTEM_PROMPT = """You are part of GDAP, a data automation platform.

Rules you must follow without exception:
1. Never state a number, trend or conclusion that did not come from a tool result. If you do not
   have the data, say so and name the tool or dataset that would provide it.
2. Distinguish clearly between fact (measured), inference (derived), hypothesis (unverified) and
   recommendation (an action you propose).
3. State uncertainty explicitly, including when a sample, a partial period or a data quality
   problem limits the conclusion.
4. Never reveal credentials, connection strings or internal file paths.
5. Prefer one well-chosen tool call over many. Stop calling tools as soon as you can answer.
6. Answer in the language the user asked in, and keep it concise and specific.
"""


@dataclass(slots=True)
class AgentRunResult:
    answer: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_results: list[ToolResult] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    insights: list[Insight] = field(default_factory=list)
    charts: list[ChartSpec] = field(default_factory=list)
    iterations: int = 0
    provider: str = "heuristic"
    usage: dict[str, Any] = field(default_factory=dict)
    limitations: list[str] = field(default_factory=list)

    def to_contract(self, question: str) -> AgentAnswer:
        successful = [result for result in self.tool_results if result.ok]
        confidence = 0.0
        if successful:
            confidence = min(0.35 + 0.15 * len(successful), 0.9)
        if not self.evidence:
            confidence = min(confidence, 0.3)
        return AgentAnswer(
            question=question,
            answer=self.answer,
            insights=self.insights,
            evidence=self.evidence,
            charts=self.charts,
            tool_calls=self.tool_calls,
            confidence=round(confidence, 2),
            provider=self.provider,
            limitations=self.limitations,
        )


class Agent:
    """One specialised worker: a role, a system prompt and a fixed set of tools it may use."""

    def __init__(
        self,
        name: str,
        *,
        description: str,
        instructions: str,
        tools: set[str],
        max_iterations: int = 6,
        max_tool_calls: int = 12,
    ) -> None:
        self.name = name
        self.description = description
        self.instructions = instructions
        self.tools = tools
        self.max_iterations = max_iterations
        self.max_tool_calls = max_tool_calls

    # ------------------------------------------------------------------ prompt
    def system_prompt(self, context: ToolContext) -> str:
        dataset_hint = (
            f"\nDefault dataset for this conversation: '{context.dataset}'."
            if context.dataset
            else ""
        )
        return (
            f"{BASE_SYSTEM_PROMPT}\n"
            f"Your role: {self.name} — {self.description}\n"
            f"{self.instructions}{dataset_hint}"
        )

    # ------------------------------------------------------------------ run
    def run(
        self,
        question: str,
        *,
        provider: Any,
        registry: ToolRegistry,
        context: ToolContext,
        extra_context: str | None = None,
    ) -> AgentRunResult:
        specs = registry.specs(self.tools)
        messages: list[dict[str, Any]] = [
            {
                "role": "user",
                "content": question
                if not extra_context
                else f"{question}\n\nContext:\n{extra_context}",
            }
        ]
        result = AgentRunResult(answer="", provider=getattr(provider, "name", "unknown"))

        with log_context(agent=self.name):
            for iteration in range(1, self.max_iterations + 1):
                result.iterations = iteration
                response = provider.complete(
                    system=self.system_prompt(context),
                    messages=messages,
                    tools=specs,
                )
                _accumulate_usage(result.usage, response.get("usage") or {})
                tool_calls = response.get("tool_calls") or []
                text = (response.get("text") or "").strip()

                if not tool_calls:
                    result.answer = text or self._fallback_answer(result)
                    break

                if len(result.tool_calls) + len(tool_calls) > self.max_tool_calls:
                    result.limitations.append(
                        f"stopped after {self.max_tool_calls} tool calls to bound cost"
                    )
                    result.answer = text or self._fallback_answer(result)
                    break

                messages.append(_assistant_message(response, tool_calls))

                blocks: list[dict[str, Any]] = []
                for call in tool_calls:
                    invocation = ToolCall(
                        tool=str(call.get("name")),
                        arguments=dict(call.get("arguments") or {}),
                        call_id=call.get("id"),
                    )
                    result.tool_calls.append(invocation)
                    tool_result = registry.execute(
                        invocation.tool,
                        invocation.arguments,
                        context,
                        allowed=self.tools,
                        call_id=invocation.call_id,
                        agent=self.name,
                    )
                    result.tool_results.append(tool_result)
                    if tool_result.ok:
                        if tool_result.evidence:
                            result.evidence.append(tool_result.evidence)
                        _collect_findings(tool_result, result)
                    else:
                        result.limitations.append(f"{invocation.tool}: {tool_result.error}")

                    blocks.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": invocation.call_id or invocation.tool,
                            "content": render_for_model(
                                tool_result.content
                                if tool_result.ok
                                else f"ERROR: {tool_result.error}"
                            ),
                            **({"is_error": True} if not tool_result.ok else {}),
                        }
                    )
                messages.append({"role": "user", "content": blocks})
            else:
                result.limitations.append(
                    f"reached the {self.max_iterations}-iteration limit before concluding"
                )
                result.answer = self._fallback_answer(result)

        METRICS.increment("agent_runs_total", agent=self.name, provider=result.provider)
        log.info(
            "agent_run_completed",
            agent=self.name,
            iterations=result.iterations,
            tool_calls=len(result.tool_calls),
            evidence=len(result.evidence),
        )
        return result

    def _fallback_answer(self, result: AgentRunResult) -> str:
        successful = [item for item in result.tool_results if item.ok]
        if not successful:
            return "I could not answer this from the available data. " + (
                " ".join(result.limitations[:2])
                if result.limitations
                else "No tool returned usable results — check that the dataset exists and has data."
            )
        return "\n".join(render_for_model(item.content)[:800] for item in successful[-2:])


# ─────────────────────────────────────────── helpers ───────────────────────────────────────


def _assistant_message(
    response: dict[str, Any], tool_calls: list[dict[str, Any]]
) -> dict[str, Any]:
    """Echo the assistant turn back verbatim when the provider gives us native blocks.

    Native blocks matter: on Claude, thinking and tool_use blocks must be replayed unchanged for
    the next turn to be valid. For providers without native blocks we synthesise an equivalent.
    """
    raw = response.get("raw_content")
    if raw is not None:
        return {"role": "assistant", "content": raw}
    blocks: list[dict[str, Any]] = []
    if response.get("text"):
        blocks.append({"type": "text", "text": response["text"]})
    for call in tool_calls:
        blocks.append(
            {
                "type": "tool_use",
                "id": call.get("id") or call.get("name"),
                "name": call.get("name"),
                "input": call.get("arguments") or {},
            }
        )
    return {"role": "assistant", "content": blocks}


def _collect_findings(tool_result: ToolResult, result: AgentRunResult) -> None:
    """Lift structured insights and charts out of a tool result into the agent's answer."""
    content = tool_result.content
    if not isinstance(content, dict):
        return

    for raw in content.get("insights", []) or []:
        if not isinstance(raw, dict):
            continue
        try:
            result.insights.append(
                Insight(
                    kind=InsightKind(raw.get("kind", "inference")),
                    title=str(raw.get("title", "")),
                    detail=str(raw.get("detail", "")),
                    severity=Severity(raw.get("severity", "info")),
                    confidence=float(raw.get("confidence", 0.5)),
                    evidence=[tool_result.evidence] if tool_result.evidence else [],
                )
            )
        except Exception as exc:  # a malformed insight must not break the answer
            log.debug("insight_discarded", tool=tool_result.tool, error=str(exc))
            continue

    chart = content.get("chart")
    if isinstance(chart, dict):
        try:
            result.charts.append(ChartSpec.model_validate(chart))
        except Exception as exc:
            log.debug("chart_discarded", tool=tool_result.tool, error=str(exc))


def _accumulate_usage(total: dict[str, Any], usage: dict[str, Any]) -> None:
    for key, value in usage.items():
        if isinstance(value, int | float):
            total[key] = total.get(key, 0) + value
