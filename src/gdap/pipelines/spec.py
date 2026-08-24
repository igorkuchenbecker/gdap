"""Pipeline specification loading, parameter interpolation and validation.

A spec is data: YAML or JSON in, :class:`PipelineSpec` out. Validation happens before anything
runs — unknown steps, bad expressions and missing parameters are caught at *create* time, not in
the middle of a nightly job (§35).
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from gdap.core.contracts import PipelineSpec
from gdap.core.errors import PipelineSpecError
from gdap.observability.logging import get_logger

log = get_logger(__name__)

_PLACEHOLDER = re.compile(r"\$\{\s*([a-zA-Z_][a-zA-Z0-9_.]*)\s*\}")


def parse_spec(payload: str | dict[str, Any]) -> PipelineSpec:
    """Parse and validate a spec from YAML/JSON text or an already-decoded mapping."""
    if isinstance(payload, str):
        try:
            decoded = yaml.safe_load(payload)
        except yaml.YAMLError as exc:
            raise PipelineSpecError(f"invalid YAML: {exc}") from exc
    else:
        decoded = payload

    if not isinstance(decoded, dict):
        raise PipelineSpecError("pipeline spec must be a mapping")

    # allow both a bare spec and one nested under a 'pipeline:' key
    if "pipeline" in decoded and isinstance(decoded["pipeline"], dict):
        decoded = decoded["pipeline"]

    try:
        spec = PipelineSpec.model_validate(decoded)
    except ValidationError as exc:
        raise PipelineSpecError(
            "pipeline spec failed validation",
            details={"errors": _readable_errors(exc)},
        ) from exc

    validate_steps(spec)
    return spec


def load_spec(path: str | Path) -> PipelineSpec:
    file_path = Path(path).expanduser()
    if not file_path.is_file():
        raise PipelineSpecError(f"pipeline file not found: {file_path}")
    return parse_spec(file_path.read_text(encoding="utf-8"))


def dump_spec(spec: PipelineSpec) -> str:
    return yaml.safe_dump(
        spec.model_dump(mode="json", by_alias=True, exclude_none=True),
        sort_keys=False,
        allow_unicode=True,
    )


def validate_steps(spec: PipelineSpec) -> None:
    """Every step must resolve to a registered handler, and ids must be unique."""
    from gdap.pipelines.steps import get_step, known_steps

    seen: set[str] = set()
    problems: list[dict[str, Any]] = []
    for index, step in enumerate(spec.steps):
        step_id = step.id or f"{step.uses}#{index + 1}"
        if step_id in seen:
            problems.append({"step": step_id, "error": "duplicate step id"})
        seen.add(step_id)
        try:
            get_step(step.uses)
        except Exception:
            problems.append(
                {
                    "step": step_id,
                    "error": f"unknown step '{step.uses}'",
                    "available": known_steps()[:40],
                }
            )
    if problems:
        raise PipelineSpecError(
            "pipeline spec references invalid steps", details={"problems": problems}
        )


def interpolate(value: Any, params: dict[str, Any]) -> Any:
    """Resolve ``${name}`` / ``${params.name}`` placeholders against the run parameters.

    A placeholder that is the *entire* string keeps the parameter's native type (so
    ``limit: "${params.batch}"`` stays an int); embedded placeholders render as text.
    """
    if isinstance(value, str):
        match = _PLACEHOLDER.fullmatch(value.strip())
        if match:
            return _lookup(match.group(1), params)
        return _PLACEHOLDER.sub(lambda m: str(_lookup(m.group(1), params)), value)
    if isinstance(value, dict):
        return {key: interpolate(item, params) for key, item in value.items()}
    if isinstance(value, list):
        return [interpolate(item, params) for item in value]
    return value


def runtime_params(spec: PipelineSpec, overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    """Pipeline defaults + run overrides + built-ins (``run_date``, ``now``, ``pipeline``)."""
    now = datetime.now(UTC)
    params: dict[str, Any] = {
        "pipeline": spec.name,
        "pipeline_version": spec.version,
        "run_date": now.date().isoformat(),
        "run_timestamp": now.isoformat(),
        "year": now.year,
        "month": now.month,
        "day": now.day,
    }
    params.update(spec.params or {})
    params.update(overrides or {})
    return params


def _lookup(reference: str, params: dict[str, Any]) -> Any:
    key = reference.removeprefix("params.")
    if key not in params:
        raise PipelineSpecError(
            f"undefined parameter '${{{reference}}}'",
            details={"available": sorted(params)},
        )
    value = params[key]
    return value.isoformat() if isinstance(value, date | datetime) else value


def _readable_errors(exc: ValidationError) -> list[dict[str, Any]]:
    return [
        {
            "field": ".".join(str(part) for part in error["loc"]),
            "problem": error["msg"],
            "type": error["type"],
        }
        for error in exc.errors()[:20]
    ]
