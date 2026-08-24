"""The AI layer without a model: deterministic routing, planning and safety."""

from __future__ import annotations

import pytest

from gdap.ai.agents.roster import ORCHESTRATOR
from gdap.ai.nl2pipeline import plan_pipeline
from gdap.ai.providers import HeuristicProvider, build_provider
from gdap.ai.tools import get_tool_registry
from gdap.core.config import Settings
from gdap.core.contracts import ToolSpec
from gdap.core.enums import ApprovalMode
from gdap.core.errors import PipelineSpecError

DATASETS = [
    {
        "dataset": "transactions",
        "columns": [
            {"name": "order_date", "type": "Date", "meaning": "date"},
            {"name": "region", "type": "String", "meaning": "categorical"},
            {"name": "revenue", "type": "Float64", "meaning": "currency"},
            {"name": "quantity", "type": "Int64", "meaning": "quantity"},
        ],
    }
]


def test_platform_works_without_credentials() -> None:
    provider = build_provider(Settings())
    assert provider.name == "heuristic"
    assert provider.supports_tools


def test_missing_credentials_fall_back_instead_of_failing() -> None:
    settings = Settings()
    settings.ai.provider = "anthropic"
    settings.ai.api_key_ref = "env:DEFINITELY_NOT_SET_XYZ"
    assert build_provider(settings).name == "heuristic"


@pytest.mark.parametrize(
    ("question", "expected_tool"),
    [
        ("Where are the anomalies in revenue?", "detect_anomaly"),
        ("What is the revenue trend?", "analyze_trend"),
        ("Why did revenue fall?", "find_drivers"),
        ("Break down revenue by region", "segment_metric"),
        ("How is the data quality?", "quality_report"),
        ("Encontre anomalias nas vendas", "detect_anomaly"),
        ("Descubra por que o faturamento caiu", "find_drivers"),
    ],
)
def test_heuristic_intent_routing(question: str, expected_tool: str) -> None:
    tools = [
        ToolSpec(name=name, description="", parameters={})
        for name in (
            "detect_anomaly",
            "analyze_trend",
            "find_drivers",
            "segment_metric",
            "quality_report",
            "describe_dataset",
        )
    ]
    response = HeuristicProvider().complete(
        system="", messages=[{"role": "user", "content": question}], tools=tools
    )
    assert response["tool_calls"][0]["name"] == expected_tool


def test_orchestrator_routes_to_specialists() -> None:
    routes = {
        "Generate an executive report": "reporting",
        "Is this data trustworthy?": "quality",
        "Where did this number come from?": "governance",
        "What columns exist?": "data",
        "Why did revenue fall?": "analysis",
    }
    for question, expected in routes.items():
        agent, _route = ORCHESTRATOR.agent_for(question)
        assert agent.name == expected, question


def test_agents_have_disjoint_privileges() -> None:
    roster = ORCHESTRATOR.roster
    assert "run_sql" not in roster["reporting"].tools
    assert "create_report" not in roster["analysis"].tools
    assert "send_alert" not in roster["data"].tools


def test_outward_facing_tools_require_approval() -> None:
    registry = get_tool_registry()
    for name in ("send_alert", "schedule_pipeline"):
        assert registry.get(name).spec.approval is ApprovalMode.REQUIRES_APPROVAL
        assert not registry.get(name).spec.read_only


def test_read_tools_are_marked_read_only() -> None:
    registry = get_tool_registry()
    for name in ("list_datasets", "describe_dataset", "run_sql", "analyze_trend"):
        assert registry.get(name).spec.read_only


def test_nl_to_pipeline_produces_the_documented_shape() -> None:
    plan = plan_pipeline(
        "Pegue os dados de transactions, remova duplicidades, calcule receita por region, "
        "compare com o mês anterior e gere um relatório.",
        datasets=DATASETS,
    )
    used = [step.uses for step in plan.spec.steps]
    assert used[0].startswith("read.")
    assert "transform.deduplicate" in used
    assert "aggregate" in used
    assert "analyze.comparison" in used
    assert "report.generate" in used
    assert plan.requires_review is True


def test_generated_plans_are_valid_pipelines() -> None:
    from gdap.pipelines.spec import validate_steps

    plan = plan_pipeline("find anomalies in revenue and alert me", datasets=DATASETS)
    validate_steps(plan.spec)  # must not raise


def test_planner_reports_when_it_cannot_identify_a_dataset() -> None:
    with pytest.raises(PipelineSpecError, match="which dataset"):
        plan_pipeline("do something clever", datasets=[])


def test_analysis_steps_read_row_level_data_after_an_aggregate() -> None:
    plan = plan_pipeline(
        "aggregate revenue per region and compare with last month", datasets=DATASETS
    )
    comparison = next(step for step in plan.spec.steps if step.uses == "analyze.comparison")
    assert comparison.input == "transactions"
