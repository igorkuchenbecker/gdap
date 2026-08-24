"""AI steps.

The AI layer is optional by construction: when no provider is configured the platform still
produces evidence-backed commentary from the deterministic analyses (§36, ADR-006). The step
therefore never fails because AI is unavailable — it degrades to the heuristic narrator.
"""

from __future__ import annotations

from gdap.core.contracts import Insight, StepSpec
from gdap.core.enums import InsightKind
from gdap.observability.logging import get_logger
from gdap.pipelines.steps.registry import StepContext, StepOutcome, register_step

log = get_logger(__name__)


@register_step(
    "ai.insights",
    description="Summarise the run's analyses into an evidence-backed narrative.",
    category="ai",
    options={
        "question": "optional focus question for the analyst",
        "max_insights": "how many insights to keep (default 8)",
    },
)
def ai_insights(context: StepContext, step: StepSpec) -> StepOutcome:
    question = context.option(step, "question")
    limit = int(context.option(step, "max_insights", 8))

    try:
        answer = context.services.agents.summarise_run(
            analyses=context.analyses,
            insights=context.insights,
            metrics=context.metrics,
            question=str(question) if question else None,
            pipeline=context.pipeline,
        )
        produced = answer.insights[:limit]
        provider = answer.provider
        narrative = answer.answer
    except Exception as exc:  # AI must never break a data pipeline
        log.warning("ai_insights_degraded", error=str(exc))
        produced = _heuristic_summary(context)[:limit]
        provider = "heuristic-fallback"
        narrative = "; ".join(insight.title for insight in produced) or "no insights available"

    context.insights.extend(produced)
    return StepOutcome(
        message=f"{len(produced)} insight(s) from {provider}: {narrative[:160]}",
        metrics={"ai_provider": provider, "ai_insights": len(produced)},
        insights=produced,
    )


def _heuristic_summary(context: StepContext) -> list[Insight]:
    """Rank what the deterministic analyses already found — no model, no invention."""
    ordered = sorted(
        context.insights,
        key=lambda insight: (
            {"critical": 0, "warning": 1, "info": 2}[insight.severity.value],
            -insight.confidence,
        ),
    )
    if ordered:
        return ordered
    return [
        Insight(
            kind=InsightKind.INFERENCE,
            title="No notable findings in this run",
            detail=(
                "The analyses completed without producing findings above the reporting threshold."
            ),
            confidence=0.5,
        )
    ]
