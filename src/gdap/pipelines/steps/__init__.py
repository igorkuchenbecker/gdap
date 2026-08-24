"""Step registry. Importing this package registers every built-in step."""

from gdap.pipelines.steps.registry import (
    StepContext,
    StepDefinition,
    StepOutcome,
    get_step,
    known_steps,
    register_step,
    step_catalog,
)


def _load_builtins() -> None:
    from gdap.pipelines.steps import (  # noqa: F401
        ai_steps,
        alerting,
        analyze,
        io,
        quality,
        transform,
    )


_load_builtins()

__all__ = [
    "StepContext",
    "StepDefinition",
    "StepOutcome",
    "get_step",
    "known_steps",
    "register_step",
    "step_catalog",
]
