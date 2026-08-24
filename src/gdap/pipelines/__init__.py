"""Pipeline engine: declarative specs, a step registry and a resumable executor (§10, §20)."""

from gdap.pipelines.executor import PipelineExecutor
from gdap.pipelines.spec import load_spec, parse_spec

__all__ = ["PipelineExecutor", "load_spec", "parse_spec"]
