"""AI endpoints: ask the analyst, plan a pipeline, inspect the tool and agent catalogues."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from gdap.api.deps import ContextDep
from gdap.api.schemas import AskBody, PlanBody
from gdap.core.contracts import AgentAnswer
from gdap.core.services.pipeline_service import PipelineService

router = APIRouter(prefix="/api/v1/agents", tags=["ai"])


@router.get("", summary="Available agents and their tool grants")
def list_agents(context: ContextDep) -> dict[str, Any]:
    return {"items": context.agents.agents(), "status": context.agents.status()}


@router.get("/tools", summary="Tool catalogue with permissions and approval modes")
def list_tools(context: ContextDep) -> dict[str, Any]:
    tools = context.agents.tools()
    return {"items": tools, "count": len(tools)}


@router.post("/ask", summary="Ask the AI Data Analyst a question about the data")
def ask(body: AskBody, context: ContextDep) -> AgentAnswer:
    return context.agents.ask(
        body.question,
        dataset=body.dataset,
        agent=body.agent,
        approved_tools=set(body.approved_tools),
    )


@router.post("/plan", summary="Turn a natural-language request into a reviewable pipeline")
def plan(body: PlanBody, context: ContextDep) -> dict[str, Any]:
    generated = context.agents.plan(body.request, dataset=body.dataset)
    # by_alias keeps the spec in its authoring form ("with:", not "with_"), so a client can
    # round-trip the plan straight back into POST /pipelines.
    payload: dict[str, Any] = {"plan": generated.model_dump(mode="json", by_alias=True)}
    if body.create:
        # The plan is stored as a *disabled-by-default* pipeline: a human still has to run it.
        row = context.pipelines.create(generated.spec)
        payload["pipeline"] = PipelineService.to_dict(row)
    return payload
