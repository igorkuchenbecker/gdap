"""The agent roster and the orchestrator (§13).

Rather than one omniscient agent, GDAP runs a small set of specialists with **different tool
grants**. That is a security property as much as a design preference: the Reporting agent cannot
run SQL, the Analysis agent cannot publish reports, and nothing but the Orchestrator decides which
of them handles a request.

    ORCHESTRATOR ── routes by intent ──▶ Data · Quality · Analysis · Reporting
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from gdap.ai.agents.base import Agent, AgentRunResult
from gdap.ai.tools.registry import ToolContext, ToolRegistry
from gdap.observability.logging import get_logger

log = get_logger(__name__)


DATA_AGENT = Agent(
    "data",
    description="knows what data exists, where it came from and what it means",
    instructions=(
        "Answer questions about datasets, schemas, volumes, freshness and lineage. "
        "Use describe_dataset and profile_dataset before speculating about a column's meaning."
    ),
    tools={"list_datasets", "describe_dataset", "profile_dataset", "get_lineage", "run_sql"},
)

QUALITY_AGENT = Agent(
    "quality",
    description="judges whether the data can be trusted, and says why not",
    instructions=(
        "Report the quality score, the dimensions that pull it down and the concrete findings. "
        "Always quantify: how many rows, what share of the dataset, which columns."
    ),
    tools={"quality_report", "profile_dataset", "describe_dataset", "list_datasets"},
)

ANALYSIS_AGENT = Agent(
    "analysis",
    description="measures what happened, what changed and what is likely next",
    instructions=(
        "Prefer the purpose-built analysis tools over ad-hoc SQL. When asked *why* something "
        "moved, combine compare_periods with find_drivers, and label causal statements as "
        "inference or hypothesis — never as fact."
    ),
    tools={
        "analyze_trend",
        "compare_periods",
        "segment_metric",
        "find_drivers",
        "detect_anomaly",
        "forecast_metric",
        "correlate_columns",
        "calculate_metric",
        "run_sql",
        "describe_dataset",
        "generate_chart",
    },
    max_iterations=8,
)

REPORTING_AGENT = Agent(
    "reporting",
    description="turns findings into artifacts people can read and share",
    instructions=(
        "Summarise for an executive audience: what happened, why it matters, what to do. "
        "Use create_report to produce the artifact and reference it in your answer."
    ),
    tools={"create_report", "generate_chart", "describe_dataset", "quality_report", "send_alert"},
    max_iterations=4,
)

GOVERNANCE_AGENT = Agent(
    "governance",
    description="answers where a number came from and who touched it",
    instructions=(
        "Trace lineage and classification. Never disclose the contents of restricted columns; "
        "describe them instead."
    ),
    tools={"get_lineage", "describe_dataset", "list_datasets"},
    max_iterations=4,
)


def build_roster() -> dict[str, Agent]:
    return {
        agent.name: agent
        for agent in (DATA_AGENT, QUALITY_AGENT, ANALYSIS_AGENT, REPORTING_AGENT, GOVERNANCE_AGENT)
    }


@dataclass(frozen=True, slots=True)
class Route:
    agent: str
    reason: str


#: Deterministic routing rules, evaluated in order. Routing is *not* delegated to the model: a
#: mis-routed request would silently grant a different tool set, which is a security boundary.
ROUTING_RULES: list[tuple[re.Pattern[str], Route]] = [
    (
        re.compile(r"(?i)\b(report|relat[óo]rio|pdf|xlsx|export|summar\w*|resumo|executive)"),
        Route("reporting", "the request asks for an artifact or an executive summary"),
    ),
    (
        re.compile(
            r"(?i)\b(qualit\w*|qualidade|missing|null\w*|duplicat\w*|trust\w*|confi[áa]vel|clean\w*)"
        ),
        Route("quality", "the request is about trustworthiness of the data"),
    ),
    (
        re.compile(r"(?i)\b(lineage|linhagem|where did|origin|proveni\w*|who changed|audit\w*)"),
        Route("governance", "the request is about provenance or accountability"),
    ),
    (
        re.compile(
            r"(?i)\b(trend\w*|anomal\w*|forecast\w*|predict\w*|compar\w*|why|porqu\w*|por que|driver\w*|"
            r"segment\w*|correlat\w*|growth|revenue|vendas|faturamento|sum\b|total|average|m[ée]dia)"
        ),
        Route("analysis", "the request asks for a measurement or an explanation"),
    ),
    (
        re.compile(r"(?i)\b(schema|column\w*|dataset\w*|table\w*|what data|quais dados|estrutura)"),
        Route("data", "the request is about what data exists"),
    ),
]


class Orchestrator:
    """Routes a question to the right specialist, and can fan out when a question spans roles."""

    def __init__(self, roster: dict[str, Agent] | None = None) -> None:
        self.roster = roster or build_roster()

    def route(self, question: str) -> Route:
        for pattern, route in ROUTING_RULES:
            if pattern.search(question):
                return route
        return Route("analysis", "default: treat an unclassified question as an analysis request")

    def agent_for(self, question: str) -> tuple[Agent, Route]:
        route = self.route(question)
        agent = self.roster.get(route.agent, self.roster["analysis"])
        log.info("agent_routed", agent=agent.name, reason=route.reason)
        return agent, route

    def run(
        self,
        question: str,
        *,
        provider: object,
        registry: ToolRegistry,
        context: ToolContext,
        agent_name: str | None = None,
    ) -> tuple[AgentRunResult, Route]:
        if agent_name:
            agent = self.roster.get(agent_name)
            if agent is None:
                from gdap.core.errors import NotFoundError

                raise NotFoundError(
                    f"unknown agent '{agent_name}'", details={"available": sorted(self.roster)}
                )
            route = Route(agent_name, "explicitly requested by the caller")
        else:
            agent, route = self.agent_for(question)

        result = agent.run(question, provider=provider, registry=registry, context=context)
        return result, route

    def describe(self) -> list[dict[str, object]]:
        return [
            {
                "name": agent.name,
                "description": agent.description,
                "tools": sorted(agent.tools),
                "max_iterations": agent.max_iterations,
            }
            for agent in self.roster.values()
        ]


ORCHESTRATOR = Orchestrator()
