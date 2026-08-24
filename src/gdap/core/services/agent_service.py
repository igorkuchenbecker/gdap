"""Agent service: the AI Data Analyst entry point (§12, §40, §41).

Everything the AI layer does for a caller goes through here, which means it inherits the caller's
identity, permissions, tenant and audit trail. There is no "AI user" with special powers.
"""

from __future__ import annotations

from typing import Any

from gdap.ai.agents.roster import ORCHESTRATOR, Orchestrator
from gdap.ai.nl2pipeline import plan_pipeline
from gdap.ai.providers import build_provider
from gdap.ai.tools.registry import ToolContext, get_tool_registry
from gdap.core.contracts import (
    AgentAnswer,
    AnalysisResult,
    Insight,
    PipelinePlan,
)
from gdap.core.enums import InsightKind, Permission, Severity
from gdap.core.services.context import ServiceContext
from gdap.observability.logging import get_logger
from gdap.security.rbac import require

log = get_logger(__name__)


class AgentService:
    def __init__(self, context: ServiceContext, orchestrator: Orchestrator | None = None) -> None:
        self.context = context
        self.orchestrator = orchestrator or ORCHESTRATOR
        self.registry = get_tool_registry()
        self._provider: Any | None = None

    @property
    def provider(self) -> Any:
        if self._provider is None:
            self._provider = build_provider(self.context.settings, self.context.platform.secrets)
        return self._provider

    # ------------------------------------------------------------------ ask
    def ask(
        self,
        question: str,
        *,
        dataset: str | None = None,
        agent: str | None = None,
        approved_tools: set[str] | None = None,
        job_id: str | None = None,
    ) -> AgentAnswer:
        """Answer a question about the data, with evidence for every claim."""
        require(self.context.principal, Permission.AGENT_USE, resource="agent")
        if not question or not question.strip():
            from gdap.core.errors import ValidationFailedError

            raise ValidationFailedError("the question is empty")

        tool_context = ToolContext(
            services=self.context,
            dataset=dataset or self._default_dataset(),
            job_id=job_id,
            approved_tools=set(approved_tools or set()),
            question=question,
        )
        self.context.audit.record(
            self.context.principal,
            "agent.ask",
            "agent",
            agent or "auto",
            details={"question": question[:500], "dataset": tool_context.dataset},
        )

        result, route = self.orchestrator.run(
            question,
            provider=self.provider,
            registry=self.registry,
            context=tool_context,
            agent_name=agent,
        )
        answer = result.to_contract(question)
        answer.limitations.append(f"answered by the '{route.agent}' agent — {route.reason}")
        if self.provider.name == "heuristic":
            answer.limitations.append(
                "no language model is configured: the answer is assembled deterministically from "
                "tool results (set ai.provider=anthropic to enable natural-language reasoning)"
            )
        return answer

    # ------------------------------------------------------------------ plan
    def plan(self, request: str, *, dataset: str | None = None) -> PipelinePlan:
        """Turn a natural-language request into a reviewable pipeline spec."""
        require(self.context.principal, Permission.AGENT_USE, resource="agent")
        plan = plan_pipeline(
            request,
            provider=self.provider,
            datasets=self._dataset_context(),
            # only an explicit caller choice overrides what the request itself names
            default_dataset=dataset,
        )
        self.context.audit.record(
            self.context.principal,
            "agent.plan",
            "pipeline",
            plan.spec.name,
            details={
                "request": request[:500],
                "steps": [step.uses for step in plan.spec.steps],
                "provider": plan.provider,
            },
        )
        log.info(
            "pipeline_planned",
            name=plan.spec.name,
            steps=len(plan.spec.steps),
            provider=plan.provider,
        )
        return plan

    # ------------------------------------------------------------------ narration
    def summarise_run(
        self,
        *,
        analyses: list[AnalysisResult],
        insights: list[Insight],
        metrics: dict[str, Any],
        question: str | None = None,
        pipeline: str | None = None,
    ) -> AgentAnswer:
        """Narrate a pipeline run from what the deterministic analyses already produced.

        Called by the ``ai.insights`` step. The narrative is *derived*, never invented: with no
        model configured this ranks and phrases existing findings; with a model it asks for a
        summary of those same findings and nothing else.
        """
        ranked = sorted(
            insights,
            key=lambda insight: (
                {"critical": 0, "warning": 1, "info": 2}[insight.severity.value],
                -insight.confidence,
            ),
        )
        headline = "; ".join(insight.title for insight in ranked[:3])

        if self.provider.name == "heuristic" or not ranked:
            return AgentAnswer(
                question=question or f"summary of pipeline '{pipeline}'",
                answer=headline or "The run completed with no notable findings.",
                insights=ranked[:8],
                evidence=[evidence for insight in ranked[:8] for evidence in insight.evidence][:12],
                confidence=0.5 if ranked else 0.3,
                provider="heuristic",
                limitations=["narrative assembled from deterministic analyses, no model involved"],
            )

        findings = "\n".join(
            f"- [{insight.kind.value}/{insight.severity.value}] {insight.title}: {insight.detail}"
            for insight in ranked[:15]
        )
        summaries = "\n".join(
            f"- {result.kind.value}: {result.summary}" for result in analyses[:10]
        )
        prompt = (
            f"Pipeline: {pipeline}\n"
            f"Question: {question or 'What happened in this run and what deserves attention?'}\n\n"
            f"Measured findings:\n{findings}\n\nAnalysis summaries:\n{summaries}\n\n"
            f"Run metrics: { {k: v for k, v in metrics.items() if isinstance(v, int | float | str)} }\n\n"
            "Write 3-5 sentences for a business reader. Use only the findings above; do not invent "
            "numbers. End with the single most useful next action."
        )
        response = self.provider.complete(
            system=(
                "You summarise data pipeline runs. Never state a number that is not in the "
                "findings you were given. Separate fact from inference explicitly."
            ),
            messages=[{"role": "user", "content": prompt}],
            tools=None,
        )
        narrative = (response.get("text") or "").strip() or headline
        summary_insight = Insight(
            kind=InsightKind.INFERENCE,
            title="Run summary",
            detail=narrative,
            severity=ranked[0].severity if ranked else Severity.INFO,
            confidence=0.6,
            evidence=[evidence for insight in ranked[:5] for evidence in insight.evidence][:6],
        )
        return AgentAnswer(
            question=question or f"summary of pipeline '{pipeline}'",
            answer=narrative,
            insights=[summary_insight, *ranked[:7]],
            evidence=summary_insight.evidence,
            confidence=0.6,
            provider=self.provider.name,
        )

    # ------------------------------------------------------------------ catalogs
    def tools(self) -> list[dict[str, Any]]:
        return self.registry.catalog()

    def agents(self) -> list[dict[str, Any]]:
        return [{**entry, "provider": self.provider.name} for entry in self.orchestrator.describe()]

    def status(self) -> dict[str, Any]:
        settings = self.context.settings.ai
        return {
            "enabled": settings.enabled,
            "provider": self.provider.name,
            "model": settings.model if self.provider.name != "heuristic" else None,
            "tools": len(self.registry.names()),
            "agents": len(self.orchestrator.roster),
            "mode": "llm" if self.provider.name != "heuristic" else "deterministic",
        }

    # ------------------------------------------------------------------ helpers
    def _default_dataset(self) -> str | None:
        rows = self.context.datasets.repo.list(limit=1, order_by="updated_at")
        return rows[0].name if rows else None

    def _dataset_context(self, limit: int = 8) -> list[dict[str, Any]]:
        """Compact schema context for the planner — names and meanings, never data."""
        entries: list[dict[str, Any]] = []
        for row in self.context.datasets.repo.list(limit=limit):
            version = self.context.datasets.repo.latest_version(row.id)
            if version is None:
                continue
            schema = (version.schema_json or {}).get("columns", [])
            entries.append(
                {
                    "dataset": row.name,
                    "rows": row.row_count,
                    "columns": [
                        {
                            "name": column.get("name"),
                            "type": column.get("dtype"),
                            "meaning": column.get("semantic_type"),
                        }
                        for column in schema
                    ],
                }
            )
        return entries
