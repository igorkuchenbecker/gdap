"""LLM providers.

The platform talks to models through one narrow port (:class:`gdap.core.ports.LLMProvider`) and
two adapters:

``HeuristicProvider``
    No network, no credentials, no model. It turns a question plus the tool catalogue into a
    deterministic plan, and turns tool results into plain-language findings. This is what keeps
    every AI feature usable — and testable — with zero configuration (ADR-006).

``AnthropicProvider``
    The real thing, using the official SDK's Messages API with tool use. The agent loop stays in
    :mod:`gdap.ai.agents.base` on purpose: approval gates, the tool allow-list and the audit trail
    are platform concerns, not SDK concerns, so we drive the loop ourselves rather than delegating
    it to the SDK's tool runner.

Vendor exceptions never escape this module: everything becomes :class:`gdap.core.errors.AIError`.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from gdap.core.config import AISettings, Settings
from gdap.core.contracts import ToolSpec
from gdap.core.errors import AIError, ConfigurationError
from gdap.observability.logging import get_logger
from gdap.observability.metrics import METRICS
from gdap.security.secrets import SecretsResolver

log = get_logger(__name__)

#: Models that still accept sampling parameters. Current Claude models reject them with a 400.
LEGACY_SAMPLING_MODELS = re.compile(r"claude-(haiku-4-5|3-|2)|gpt|llama|mistral")


class HeuristicProvider:
    """Deterministic stand-in for a model.

    It does not write prose about data it has not seen: the "plan" is keyword-driven tool
    selection, and the "answer" is a template filled exclusively from tool results.
    """

    name = "heuristic"
    supports_tools = True

    #: question keywords → tool name, in priority order
    INTENTS: list[tuple[re.Pattern[str], str]] = [
        (
            re.compile(r"(?i)\b(anomal\w*|outlier\w*|unusual|spike\w*|weird|estranh\w*)"),
            "detect_anomaly",
        ),
        (
            re.compile(r"(?i)\b(trend\w*|growth|over time|evolu\w*|tend[êe]nci\w*|crescimento)"),
            "analyze_trend",
        ),
        (
            re.compile(r"(?i)\b(why|caus\w*|driver\w*|explain\w*|reason|por que|porqu\w*|motivo)"),
            "find_drivers",
        ),
        (
            re.compile(
                r"(?i)\b(compar\w*|versus|vs\b|change[ds]?\b|previous|last month|per[íi]od\w*)"
            ),
            "compare_periods",
        ),
        (
            re.compile(
                r"(?i)\b(region\w*|segment\w*|categor\w*|channel\w*|break ?down|by \w+|per \w+|por \w+)"
            ),
            "segment_metric",
        ),
        (
            re.compile(
                r"(?i)\b(forecast\w*|predict\w*|next (month|week|quarter)|proje\w*|previs\w*)"
            ),
            "forecast_metric",
        ),
        (
            re.compile(
                r"(?i)\b(qualit\w*|trust\w*|confi[áa]ve\w*|reliab\w*|missing|null\w*|duplicat\w*|qualidade|nulo\w*)"
            ),
            "quality_report",
        ),
        (
            re.compile(
                r"(?i)\b(schema|column\w*|structure|profil\w*|perfil|coluna\w*|type[sd]?\b)"
            ),
            "profile_dataset",
        ),
        (
            re.compile(r"(?i)\b(how many|count\w*|total|sum\b|average|quant\w*|m[ée]dia|soma)"),
            "calculate_metric",
        ),
        (
            re.compile(
                r"(?i)(what|which|list|quais|liste)\b[^?]{0,30}\b(datasets?|tables?|dados dispon)"
            ),
            "list_datasets",
        ),
    ]

    def complete(
        self,
        *,
        system: str,
        messages: Sequence[dict[str, Any]],
        tools: Sequence[ToolSpec] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> dict[str, Any]:
        available = {tool.name for tool in tools or []}
        question = _last_user_text(messages)
        already_called = _tools_already_called(messages)

        if tools and question:
            for pattern, tool_name in self.INTENTS:
                if (
                    tool_name in available
                    and tool_name not in already_called
                    and pattern.search(question)
                ):
                    return {
                        "text": "",
                        "tool_calls": [
                            {"name": tool_name, "arguments": {}, "id": f"heuristic-{tool_name}"}
                        ],
                        "stop_reason": "tool_use",
                        "usage": {},
                    }
            if "describe_dataset" in available and "describe_dataset" not in already_called:
                return {
                    "text": "",
                    "tool_calls": [
                        {"name": "describe_dataset", "arguments": {}, "id": "heuristic-describe"}
                    ],
                    "stop_reason": "tool_use",
                    "usage": {},
                }

        return {
            "text": _summarise_observations(messages),
            "tool_calls": [],
            "stop_reason": "end_turn",
            "usage": {},
        }


class AnthropicProvider:
    """Claude adapter (Messages API with tool use)."""

    name = "anthropic"
    supports_tools = True

    def __init__(self, settings: AISettings, api_key: str) -> None:
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - optional extra
            raise ConfigurationError(
                "the Anthropic provider requires the 'ai' extra: pip install 'gdap[ai]'",
                details={"fallback": "set ai.provider=heuristic to run without a model"},
            ) from exc

        self._anthropic = anthropic
        self.settings = settings
        self.model = settings.model
        self._client = anthropic.Anthropic(api_key=api_key, timeout=float(settings.timeout_s))

    def complete(
        self,
        *,
        system: str,
        messages: Sequence[dict[str, Any]],
        tools: Sequence[ToolSpec] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> dict[str, Any]:
        request: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens or self.settings.max_tokens,
            "system": system,
            "messages": list(messages),
        }
        if tools:
            request["tools"] = [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": tool.parameters,
                }
                for tool in tools
            ]
        if self.settings.thinking == "adaptive":
            request["thinking"] = {"type": "adaptive"}
        if self.settings.effort:
            request["output_config"] = {"effort": self.settings.effort}
        # Current Claude models reject sampling parameters; only legacy models get them.
        if (
            self.settings.legacy_sampling
            and temperature is not None
            and LEGACY_SAMPLING_MODELS.search(self.model)
        ):
            request["temperature"] = temperature

        try:
            with METRICS.timer("llm_request_ms", provider=self.name, model=self.model):
                response = self._client.messages.create(**request)
        except self._anthropic.RateLimitError as exc:
            raise AIError(
                "the model provider is rate limiting this workspace — retry shortly",
                details={"provider": self.name},
                cause=exc,
            ) from exc
        except self._anthropic.APIConnectionError as exc:
            raise AIError("could not reach the model provider", cause=exc) from exc
        except self._anthropic.APIStatusError as exc:
            raise AIError(
                f"model provider error ({exc.status_code})",
                details={"provider": self.name, "model": self.model},
                cause=exc,
            ) from exc
        except Exception as exc:  # defensive: never leak a vendor exception upwards
            raise AIError(f"unexpected model failure: {type(exc).__name__}", cause=exc) from exc

        return self._parse(response)

    def _parse(self, response: Any) -> dict[str, Any]:
        text_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_calls.append(
                    {"name": block.name, "arguments": dict(block.input), "id": block.id}
                )

        stop_reason = getattr(response, "stop_reason", "end_turn")
        if stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            log.warning(
                "model_refused",
                category=getattr(details, "category", None),
                explanation=getattr(details, "explanation", None),
            )

        usage = getattr(response, "usage", None)
        return {
            "text": "\n".join(part for part in text_parts if part).strip(),
            "tool_calls": tool_calls,
            "stop_reason": stop_reason,
            "raw_content": response.content,
            "usage": {
                "input_tokens": getattr(usage, "input_tokens", 0),
                "output_tokens": getattr(usage, "output_tokens", 0),
                "cache_read_input_tokens": getattr(usage, "cache_read_input_tokens", 0),
            }
            if usage
            else {},
        }

    def count_tokens(self, *, system: str, messages: Sequence[dict[str, Any]]) -> int:
        """Exact token accounting for budgeting — never estimate with a third-party tokenizer."""
        try:
            result = self._client.messages.count_tokens(
                model=self.model, system=system, messages=list(messages)
            )
            return int(result.input_tokens)
        except Exception as exc:  # pragma: no cover - best effort
            log.debug("token_count_failed", error=str(exc))
            return 0


def build_provider(settings: Settings, secrets: SecretsResolver | None = None) -> Any:
    """Select and construct the configured provider, degrading to heuristic with a clear log."""
    ai = settings.ai
    if not ai.enabled or ai.provider == "heuristic":
        return HeuristicProvider()

    resolver = secrets or SecretsResolver(settings)
    try:
        api_key = resolver.resolve(ai.api_key_ref)
    except ConfigurationError as exc:
        log.warning(
            "llm_credentials_missing",
            provider=ai.provider,
            reference=ai.api_key_ref,
            reason=exc.message,
            action="falling back to the deterministic heuristic provider",
        )
        return HeuristicProvider()

    if ai.provider == "anthropic":
        log.info("llm_provider_ready", provider="anthropic", model=ai.model)
        return AnthropicProvider(ai, api_key)

    raise ConfigurationError(
        f"unknown AI provider '{ai.provider}'", details={"supported": ["heuristic", "anthropic"]}
    )


# ─────────────────────────────────────────── helpers ───────────────────────────────────────


def _last_user_text(messages: Sequence[dict[str, Any]]) -> str:
    for message in reversed(list(messages)):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            texts = [
                block.get("text", "")
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            ]
            if texts:
                return " ".join(texts)
    return ""


def _tools_already_called(messages: Sequence[dict[str, Any]]) -> set[str]:
    called: set[str] = set()
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                called.add(str(block.get("name")))
            elif hasattr(block, "type") and getattr(block, "type", None) == "tool_use":
                called.add(str(getattr(block, "name", "")))
    return called


def _summarise_observations(messages: Sequence[dict[str, Any]]) -> str:
    """Build the final answer strictly from what the tools returned.

    Tool payloads are JSON; a raw dump is unreadable, so this lifts the fields that carry meaning
    — the analysis summary, insight titles, quality status — and leaves everything else out. It
    never adds a number that is not present in a tool result.
    """
    import json as _json

    lines: list[str] = []
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not (isinstance(block, dict) and block.get("type") == "tool_result"):
                continue
            payload = block.get("content")
            if not isinstance(payload, str) or not payload.strip():
                continue
            if block.get("is_error"):
                lines.append(payload.strip().splitlines()[0])
                continue
            try:
                parsed = _json.loads(payload)
            except (ValueError, TypeError):
                # a truncated payload is still readable: pull the fields that matter
                lines.extend(_salvage(payload))
                continue
            lines.extend(_readable(parsed))

    if not lines:
        return (
            "No tool produced a result for this question, so there is nothing to report. "
            "Name a dataset and a metric explicitly, or check that the dataset has data."
        )
    seen: set[str] = set()
    unique: list[str] = []
    for line in lines:
        if line not in seen:
            seen.add(line)
            unique.append(line)
    return "\n".join(f"• {line}" for line in unique[:8])


def _salvage(payload: str) -> list[str]:
    """Recover meaning from a payload too large to have survived intact."""
    lines: list[str] = []
    summary = re.search(r'"summary"\s*:\s*"([^"]{3,400})"', payload)
    if summary:
        lines.append(summary.group(1))
    for title in re.findall(r'"title"\s*:\s*"([^"]{3,200})"', payload)[:4]:
        lines.append(title)
    if not lines:
        lines.append(payload.strip()[:300] + " …")
    return lines


def _readable(payload: Any) -> list[str]:
    """Extract the human-meaningful parts of a tool payload."""
    lines: list[str] = []
    if isinstance(payload, list):
        if payload and isinstance(payload[0], dict) and "dataset" in payload[0]:
            names = ", ".join(str(item.get("dataset")) for item in payload[:10])
            lines.append(f"Datasets available: {names}")
        return lines

    if not isinstance(payload, dict):
        return lines

    if payload.get("summary"):
        lines.append(str(payload["summary"]))
    for insight in payload.get("insights", [])[:4]:
        if isinstance(insight, dict) and insight.get("title"):
            marker = {
                "fact": "",
                "inference": "(inference) ",
                "hypothesis": "(hypothesis) ",
                "recommendation": "(recommendation) ",
            }.get(str(insight.get("kind")), "")
            lines.append(f"{marker}{insight['title']}")
    if "score" in payload and "status" in payload:
        lines.append(f"Data quality is {payload['status']} at {payload['score']}/100")
        for finding in payload.get("findings", [])[:3]:
            if isinstance(finding, dict):
                lines.append(f"{finding.get('severity', 'info')}: {finding.get('message', '')}")
    if "columns" in payload and "dataset" in payload:
        lines.append(
            f"'{payload['dataset']}' has {payload.get('rows', '?')} rows and "
            f"{len(payload['columns'])} columns (version {payload.get('version', '?')})"
        )
    if "recommendations" in payload:
        lines.extend(str(item) for item in payload["recommendations"][:3])
    if "records" in payload and isinstance(payload["records"], list) and payload["records"]:
        lines.append(
            f"Query returned {payload.get('rows', len(payload['records']))} row(s); first: {payload['records'][0]}"
        )
    for key in ("sum", "mean", "median", "count", "min", "max"):
        if key in payload:
            lines.append(f"{key} = {payload[key]}")
    if payload.get("inferred_arguments"):
        lines.append(f"(columns chosen automatically: {', '.join(payload['inferred_arguments'])})")
    if payload.get("reports"):
        formats = ", ".join(str(item.get("format")) for item in payload["reports"])
        lines.append(f"Report generated ({formats}).")
    return lines
