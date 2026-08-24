"""Analysis endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from gdap.api.deps import ContextDep, PaginationDep
from gdap.api.schemas import AnalysisBody
from gdap.core.contracts import AnalysisResult
from gdap.core.services.analysis_service import AUTO_KINDS, AnalysisService

router = APIRouter(prefix="/api/v1/analyses", tags=["analyses"])


@router.get("", summary="List stored analyses")
def list_analyses(
    context: ContextDep,
    page: PaginationDep,
    dataset: str | None = Query(default=None),
    kind: str | None = Query(default=None),
) -> dict[str, Any]:
    rows = context.analyses.list(dataset=dataset, kind=kind, limit=page.limit)
    return {"items": [AnalysisService.to_dict(row) for row in rows], "count": len(rows)}


@router.get("/kinds", summary="Analyses the platform can run")
def kinds() -> dict[str, Any]:
    return {
        "items": [kind.value for kind in AUTO_KINDS],
        "auto": [kind.value for kind in AUTO_KINDS],
    }


@router.post("", summary="Run an analysis")
def run_analysis(body: AnalysisBody, context: ContextDep) -> AnalysisResult:
    return context.analyses.run(
        body.dataset,
        body.kind,
        params=body.params,
        version=body.version,
        persist=body.persist,
    )


@router.post("/auto", summary="Run every analysis that applies to a dataset")
def run_auto(
    context: ContextDep,
    dataset: str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    results = context.analyses.auto(dataset, params=params or {})
    return {
        "items": [result.model_dump(mode="json") for result in results],
        "count": len(results),
    }


@router.get("/{analysis_id}", summary="Get a stored analysis")
def get_analysis(analysis_id: str, context: ContextDep) -> AnalysisResult:
    return context.analyses.get(analysis_id)


@router.get("/insights/{dataset}", summary="Recent insights for a dataset")
def insights(dataset: str, context: ContextDep, limit: int = 20) -> dict[str, Any]:
    items = context.analyses.insights(dataset, limit=limit)
    return {"items": [insight.model_dump(mode="json") for insight in items], "count": len(items)}
