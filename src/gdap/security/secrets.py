"""Secret resolution.

Secrets are *referenced*, never stored: a source keeps ``{"password": "env:PG_PASSWORD"}`` and the
value is fetched at the last possible moment, held only for the lifetime of the connector, and
never logged, serialised or returned by the API.

Supported schemes: ``env:NAME``, ``file:/path`` (also ``file:relative`` under a configured root)
and ``literal:value`` — which is refused outside development on purpose.
"""

from __future__ import annotations

import os
from pathlib import Path

from gdap.core.config import Settings
from gdap.core.errors import ConfigurationError
from gdap.observability.logging import get_logger

log = get_logger(__name__)

REDACTED = "***"


class SecretsResolver:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._root = settings.security.secret_file_root

    def resolve(self, ref: str) -> str:
        if not isinstance(ref, str) or ":" not in ref:
            raise ConfigurationError(
                "secret reference must look like 'env:NAME' or 'file:/path'",
                details={"received_type": type(ref).__name__},
            )
        scheme, _, target = ref.partition(":")
        scheme = scheme.strip().lower()
        target = target.strip()

        if scheme == "env":
            value = os.environ.get(target)
            if value is None:
                raise ConfigurationError(
                    f"environment variable '{target}' is not set",
                    details={"hint": "export it or point the reference somewhere else"},
                )
            return value

        if scheme == "file":
            path = Path(target).expanduser()
            if not path.is_absolute() and self._root:
                path = Path(self._root).expanduser() / target
            if not path.is_file():
                raise ConfigurationError(f"secret file not found: {path}")
            return path.read_text(encoding="utf-8").strip()

        if scheme == "literal":
            if self._settings.is_production:
                raise ConfigurationError(
                    "literal secrets are forbidden in production — use env: or file:"
                )
            log.warning("literal_secret_used", environment=self._settings.environment)
            return target

        raise ConfigurationError(
            f"unsupported secret scheme '{scheme}'",
            details={"supported": ["env", "file", "literal (non-production)"]},
        )

    def resolve_all(self, refs: dict[str, str]) -> dict[str, str]:
        return {name: self.resolve(ref) for name, ref in (refs or {}).items()}

    @staticmethod
    def redact(payload: dict[str, object]) -> dict[str, object]:
        """Best-effort redaction for logs and API responses."""
        suspicious = ("password", "secret", "token", "key", "credential", "authorization")
        out: dict[str, object] = {}
        for key, value in payload.items():
            if any(word in key.lower() for word in suspicious):
                out[key] = REDACTED
            elif isinstance(value, dict):
                out[key] = SecretsResolver.redact(value)  # type: ignore[arg-type]
            else:
                out[key] = value
        return out
