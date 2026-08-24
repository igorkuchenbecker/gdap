"""Natural language → pipeline (§41).

    "Take the sales data, remove duplicates, compute revenue per region,
     compare with last month and produce a report."

becomes a **reviewable** :class:`PipelineSpec`. Two planners, one contract:

``LLM planner``
    Asks the model for a spec, then validates it against the real step registry and the safe
    expression grammar. An invalid plan is retried once with the validation error attached; a plan
    that still fails validation is discarded — never partially executed.

``Heuristic planner``
    Intent matching over the same step catalogue. It is the fallback when no model is configured,
    and the safety net when the model produces something invalid.

Generated plans are never auto-executed: ``requires_review`` is True by default, because a
pipeline writes data (§36.3, §38).
"""

from __future__ import annotations

import json
import re
from typing import Any

from gdap.core.contracts import PipelinePlan, PipelineSpec, StepSpec
from gdap.core.errors import AIError, PipelineSpecError
from gdap.observability.logging import get_logger
from gdap.pipelines.spec import parse_spec
from gdap.pipelines.steps import step_catalog

log = get_logger(__name__)

PLANNER_SYSTEM = """You translate a data request into a GDAP pipeline specification.

Return ONLY a JSON object with this shape — no prose, no markdown fence:
{
  "name": "snake_case_name",
  "description": "one line",
  "steps": [{"id": "...", "uses": "<step key>", "with": {...}, "input": "<frame>", "output": "<frame>"}],
  "assumptions": ["..."],
  "rationale": "why these steps, in one or two sentences"
}

Hard rules:
- "uses" MUST be one of the step keys listed below. Never invent a step.
- Expressions (transform.calculate, transform.filter) use the safe grammar: column names,
  literals, + - * / comparisons, and functions like round(), upper(), trim(), coalesce(),
  if(cond, a, b), date_part(col, 'month'). No Python, no attribute access, no imports.
- The first step must read data (read.dataset, read.source or read.query).
- Only reference columns that exist in the dataset schema provided.
- If the request is ambiguous, choose the conservative interpretation and record it in
  "assumptions" rather than inventing business rules.
"""


def plan_pipeline(
    request: str,
    *,
    provider: Any | None = None,
    datasets: list[dict[str, Any]] | None = None,
    default_dataset: str | None = None,
) -> PipelinePlan:
    """Produce a reviewable pipeline plan from a natural-language request."""
    if provider is not None and getattr(provider, "name", "heuristic") != "heuristic":
        try:
            return _plan_with_model(request, provider, datasets or [], default_dataset)
        except (AIError, PipelineSpecError, ValueError) as exc:
            log.warning("llm_planner_failed", error=str(exc), action="falling back to heuristics")
    return _plan_heuristically(request, default_dataset, datasets or [])


# ─────────────────────────────────────────── LLM path ──────────────────────────────────────


def _plan_with_model(
    request: str,
    provider: Any,
    datasets: list[dict[str, Any]],
    default_dataset: str | None,
) -> PipelinePlan:
    catalogue = "\n".join(
        f"- {step['key']}: {step['description']} options={list(step['options'])[:6]}"
        for step in step_catalog()
    )
    context_block = json.dumps(datasets[:8], indent=2, default=str) if datasets else "(no datasets)"
    prompt = (
        f"Request: {request}\n\n"
        f"Default dataset: {default_dataset or 'unspecified'}\n\n"
        f"Available datasets and schemas:\n{context_block}\n\n"
        f"Available steps:\n{catalogue}"
    )

    messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
    last_error: str | None = None

    for attempt in (1, 2):
        response = provider.complete(system=PLANNER_SYSTEM, messages=messages, tools=None)
        text = (response.get("text") or "").strip()
        try:
            payload = _extract_json(text)
            spec_payload = {
                "name": payload.get("name") or _slug(request),
                "description": payload.get("description") or request[:200],
                "steps": payload.get("steps") or [],
            }
            spec = parse_spec(spec_payload)
            return PipelinePlan(
                request=request,
                spec=spec,
                rationale=str(payload.get("rationale") or "generated from the request"),
                assumptions=[str(item) for item in payload.get("assumptions", [])][:10],
                requires_review=True,
                provider=getattr(provider, "name", "llm"),
                confidence=0.7,
            )
        except (ValueError, PipelineSpecError) as exc:
            last_error = str(exc)
            log.info("llm_plan_invalid", attempt=attempt, error=last_error[:300])
            if attempt == 1:
                messages.extend(
                    [
                        {"role": "assistant", "content": text or "(empty)"},
                        {
                            "role": "user",
                            "content": (
                                f"That plan failed validation: {last_error}\n"
                                "Return corrected JSON only."
                            ),
                        },
                    ]
                )
    raise AIError(f"the planner could not produce a valid pipeline: {last_error}")


def _extract_json(text: str) -> dict[str, Any]:
    """Tolerate a fenced block, but never execute anything — this is strict JSON parsing."""
    candidate = text.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", candidate, re.DOTALL)
    if fence:
        candidate = fence.group(1).strip()
    start, end = candidate.find("{"), candidate.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("the planner returned no JSON object")
    payload = json.loads(candidate[start : end + 1])
    if not isinstance(payload, dict):
        raise ValueError("the planner returned JSON that is not an object")
    return payload


# ──────────────────────────────────────── heuristic path ───────────────────────────────────


INTENTS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?i)\b(dedup\w*|duplicat\w*|duplicidad\w*|remove duplicates)"), "deduplicate"),
    (re.compile(r"(?i)\b(clean\w*|limp\w*|normali[sz]\w*|padroni[sz]\w*|fix\w*)"), "clean"),
    (re.compile(r"(?i)\b(valid\w*|quality|qualidade|check\w*|verifi\w*)"), "validate"),
    (re.compile(r"(?i)\b(filter\w*|filtr\w*|only|apenas|somente|where)"), "filter"),
    (
        re.compile(r"(?i)\b(revenue|receita|faturamento|calculat\w*|calcul\w*|comput\w*)"),
        "calculate",
    ),
    (
        re.compile(r"(?i)\b(by|per|por|group\w*|agrup\w*|breakdown|region|categor\w*|segment\w*)"),
        "aggregate",
    ),
    (
        re.compile(r"(?i)\b(compar\w*|previous|anterior|last (month|week|year)|m[êe]s passado|vs)"),
        "compare",
    ),
    (re.compile(r"(?i)\b(trend\w*|tend[êe]nc\w*|over time|evolu\w*|growth|crescimento)"), "trend"),
    (re.compile(r"(?i)\b(anomal\w*|outlier\w*|unusual|estranh\w*|spike\w*)"), "anomaly"),
    (
        re.compile(
            r"(?i)\b(forecast\w*|previs\w*|predict\w*|proje\w*|next month|pr[óo]ximo m[êe]s)"
        ),
        "forecast",
    ),
    (re.compile(r"(?i)\b(report\w*|relat[óo]rio|dashboard|summar\w*|resumo)"), "report"),
    (re.compile(r"(?i)\b(alert\w*|notif\w*|avis\w*|warn\w*)"), "alert"),
    (re.compile(r"(?i)\b(save|persist|write|grav\w*|salv\w*|publish\w*|table|tabela)"), "write"),
]

_GROUP_HINT = re.compile(r"(?i)\b(?:by|per|por)\s+([a-z_][a-z0-9_]*)")
_METRIC_HINT = re.compile(
    r"(?i)\b(revenue|receita|faturamento|amount|valor|total|price|quantity|qty)\b"
)


def _plan_heuristically(
    request: str, default_dataset: str | None, datasets: list[dict[str, Any]]
) -> PipelinePlan:
    """Intent matching over the step catalogue — deterministic, explainable, always available."""
    intents = [name for pattern, name in INTENTS if pattern.search(request)]
    assumptions: list[str] = []

    dataset = default_dataset or _guess_dataset(request, datasets)
    if not default_dataset and dataset:
        named = _guess_dataset(request, datasets)
        dataset = named or dataset
    if dataset is None:
        raise PipelineSpecError(
            "could not tell which dataset this request is about",
            details={
                "hint": "name the dataset, or pass dataset=…",
                "available": [d.get("dataset") for d in datasets],
            },
        )
    if not default_dataset:
        assumptions.append(f"assumed the request refers to the dataset '{dataset}'")

    columns = _columns_for(dataset, datasets)
    metric = _guess_metric(request, columns)
    dimension = _guess_dimension(request, columns)
    time_column = _guess_time_column(columns)

    steps: list[StepSpec] = [
        StepSpec.of(
            "read.dataset",
            id="read",
            options={"dataset": dataset},
        )
    ]
    # analysis steps must read the row-level frame, not whatever an aggregate step published
    row_level = dataset

    if "clean" in intents:
        steps.append(
            StepSpec.of(
                "clean.auto",
                id="clean",
                options={"apply": "validated", "dataset": dataset},
            )
        )
    if "deduplicate" in intents and "clean" not in intents:
        steps.append(StepSpec.of("transform.deduplicate", id="deduplicate"))
    if "validate" in intents:
        steps.append(
            StepSpec.of(
                "validate.expectations",
                id="validate",
                options={"auto": True, "dataset": dataset},
            )
        )
    if "aggregate" in intents and metric and dimension:
        steps.append(
            StepSpec.of(
                "aggregate",
                id="aggregate",
                output="aggregated",
                options={
                    "group_by": [dimension],
                    "metrics": {metric: f"sum({metric})", "rows": "count"},
                },
            )
        )
        assumptions.append(f"aggregated sum({metric}) grouped by '{dimension}'")
    if "compare" in intents and metric and time_column:
        steps.append(
            StepSpec.of(
                "analyze.comparison",
                id="compare",
                input=row_level,
                options={
                    "metric": metric,
                    "time_column": time_column,
                    "granularity": "month",
                    **({"dimension": dimension} if dimension else {}),
                },
            )
        )
    if "trend" in intents and metric and time_column:
        steps.append(
            StepSpec.of(
                "analyze.trend",
                id="trend",
                input=row_level,
                options={"metric": metric, "time_column": time_column, "granularity": "month"},
            )
        )
    if "anomaly" in intents and metric:
        steps.append(
            StepSpec.of(
                "analyze.anomaly",
                id="anomalies",
                input=row_level,
                options={
                    "metric": metric,
                    "method": "timeseries" if time_column else "iqr",
                    **({"time_column": time_column} if time_column else {}),
                    "granularity": "week",
                },
            )
        )
    if "forecast" in intents and metric and time_column:
        steps.append(
            StepSpec.of(
                "analyze.forecast",
                id="forecast",
                input=row_level,
                options={"metric": metric, "time_column": time_column, "horizon": 3},
            )
        )
    if "write" in intents:
        steps.append(
            StepSpec.of(
                "write.dataset",
                id="publish",
                options={"dataset": f"{dataset}_processed"},
            )
        )
    if "report" in intents or len(steps) == 1:
        steps.append(
            StepSpec.of(
                "report.generate",
                id="report",
                options={
                    "title": _title(request),
                    "formats": ["html"],
                    "dataset": dataset,
                },
            )
        )
    if "alert" in intents:
        steps.append(
            StepSpec.of(
                "alert.threshold",
                id="alert",
                options={
                    "metric": "quality_score",
                    "operator": "lt",
                    "threshold": 85,
                    "severity": "warning",
                    "message": "Raised by a pipeline generated from a natural-language request.",
                },
            )
        )

    spec = PipelineSpec(
        name=_slug(request),
        description=request[:200],
        steps=steps,
        tags=["generated", "nl2pipeline"],
    )
    rationale = (
        "Matched intents "
        + ", ".join(intents or ["read"])
        + f" against the step catalogue; {len(steps)} step(s) planned."
    )
    if not metric:
        assumptions.append("no metric column was identified — analysis steps were left out")
    return PipelinePlan(
        request=request,
        spec=spec,
        rationale=rationale,
        assumptions=assumptions,
        requires_review=True,
        provider="heuristic",
        confidence=0.55 if intents else 0.35,
    )


def _title(request: str) -> str:
    """A readable report title: first clause of the request, capped."""
    clause = re.split(r"[.,;]| e |and ", request.strip())[0].strip()
    title = (clause or request)[:80].strip()
    return title[:1].upper() + title[1:]


def _slug(text: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return (cleaned[:48] or "generated_pipeline").rstrip("_")


def _guess_dataset(request: str, datasets: list[dict[str, Any]]) -> str | None:
    lowered = request.lower()
    for entry in datasets:
        name = str(entry.get("dataset", ""))
        if name and name.lower() in lowered:
            return name
    return str(datasets[0]["dataset"]) if datasets else None


def _columns_for(dataset: str, datasets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for entry in datasets:
        if str(entry.get("dataset")) == dataset:
            return list(entry.get("columns", []))
    return []


def _guess_metric(request: str, columns: list[dict[str, Any]]) -> str | None:
    numeric = [
        column
        for column in columns
        if str(column.get("meaning")) in {"currency", "numeric", "quantity", "percentage"}
        or str(column.get("type", "")).lower().startswith(("float", "int"))
    ]
    lowered = request.lower()
    for column in numeric:
        if str(column.get("name", "")).lower() in lowered:
            return str(column["name"])
    hint = _METRIC_HINT.search(request)
    if hint:
        for column in numeric:
            if hint.group(1).lower() in str(column.get("name", "")).lower():
                return str(column["name"])
    currency = [c for c in numeric if str(c.get("meaning")) == "currency"]
    if currency:
        return str(currency[0]["name"])
    return str(numeric[0]["name"]) if numeric else None


def _guess_dimension(request: str, columns: list[dict[str, Any]]) -> str | None:
    categorical = [
        str(column.get("name"))
        for column in columns
        if str(column.get("meaning")) in {"categorical", "ordinal", "country"}
    ]
    hint = _GROUP_HINT.search(request)
    if hint:
        candidate = hint.group(1).lower()
        for name in categorical:
            if candidate in name.lower():
                return name
    lowered = request.lower()
    for name in categorical:
        if name.lower() in lowered:
            return name
    return categorical[0] if categorical else None


def _guess_time_column(columns: list[dict[str, Any]]) -> str | None:
    for column in columns:
        if str(column.get("meaning")) in {"date", "datetime", "timestamp"}:
            return str(column.get("name"))
    return None
