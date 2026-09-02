"""Typed, machine-readable error hierarchy.

Every error carries a stable ``code`` (``GDAP-XXXX``) so that API clients, the CLI and the
AI layer can branch on failures without string matching. ``http_status`` lets the API layer
translate domain errors without knowing about them individually.
"""

from __future__ import annotations

from typing import Any


class GdapError(Exception):
    """Base class for every error raised on purpose by the platform."""

    code: str = "GDAP-1000"
    http_status: int = 500
    message: str = "Unexpected platform error"

    def __init__(
        self,
        message: str | None = None,
        *,
        details: dict[str, Any] | None = None,
        cause: Exception | None = None,
    ) -> None:
        self.message = message or self.__class__.message
        self.details: dict[str, Any] = details or {}
        self.cause = cause
        super().__init__(self.message)

    def to_dict(self, trace_id: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "details": self.details,
        }
        if trace_id:
            payload["trace_id"] = trace_id
        return {"error": payload}

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return f"{type(self).__name__}(code={self.code!r}, message={self.message!r})"


# --- configuration & wiring -------------------------------------------------------------
class ConfigurationError(GdapError):
    code = "GDAP-1001"
    http_status = 500
    message = "Invalid or missing configuration"


class PluginError(GdapError):
    code = "GDAP-1002"
    http_status = 500
    message = "Plugin could not be loaded"


# --- request / resource -----------------------------------------------------------------
class NotFoundError(GdapError):
    code = "GDAP-2000"
    http_status = 404
    message = "Resource not found"


class ConflictError(GdapError):
    code = "GDAP-2001"
    http_status = 409
    message = "Resource conflict"


class ValidationFailedError(GdapError):
    code = "GDAP-2002"
    http_status = 422
    message = "Validation failed"


class UnsupportedOperationError(GdapError):
    code = "GDAP-2003"
    http_status = 400
    message = "Operation not supported"


class PayloadTooLargeError(GdapError):
    code = "GDAP-2004"
    http_status = 413
    message = "Payload exceeds the configured size limit"


# --- security ----------------------------------------------------------------------------
class AuthenticationError(GdapError):
    code = "GDAP-3000"
    http_status = 401
    message = "Authentication required"


class AuthorizationError(GdapError):
    code = "GDAP-3001"
    http_status = 403
    message = "Not allowed"


class PolicyViolationError(GdapError):
    code = "GDAP-3002"
    http_status = 403
    message = "Blocked by policy"


class SqlSafetyError(PolicyViolationError):
    code = "GDAP-3003"
    message = "SQL statement blocked by the safety layer"


class RateLimitedError(GdapError):
    code = "GDAP-3004"
    http_status = 429
    message = "Too many requests"


class ApprovalRequiredError(GdapError):
    code = "GDAP-3005"
    http_status = 409
    message = "Operation requires human approval"


# --- data plane ---------------------------------------------------------------------------
class ConnectorError(GdapError):
    code = "GDAP-4000"
    http_status = 502
    message = "Connector failure"


class ConnectionTestError(ConnectorError):
    code = "GDAP-4001"
    message = "Could not connect to the source"


class IngestionError(GdapError):
    code = "GDAP-4100"
    http_status = 500
    message = "Ingestion failure"


class SchemaDriftError(IngestionError):
    code = "GDAP-4101"
    # 409, not the 500 it inherits from IngestionError. The data the caller supplied conflicts
    # with the dataset it is being loaded into; nothing on this side failed. A 500 pages someone
    # and tells every client to retry a request that will never succeed unchanged.
    http_status = 409
    message = "Incompatible schema change detected"


class StorageError(GdapError):
    code = "GDAP-4200"
    http_status = 500
    message = "Storage failure"


class QualityGateError(GdapError):
    code = "GDAP-4300"
    http_status = 422
    message = "Data quality gate failed"


# --- orchestration -------------------------------------------------------------------------
class PipelineError(GdapError):
    code = "GDAP-5000"
    http_status = 400
    message = "Pipeline failure"


class PipelineSpecError(PipelineError):
    code = "GDAP-5001"
    http_status = 422
    message = "Invalid pipeline specification"


class StepExecutionError(PipelineError):
    code = "GDAP-5002"
    http_status = 500
    message = "Pipeline step failed"


class JobCancelledError(PipelineError):
    code = "GDAP-5003"
    http_status = 409
    message = "Job cancelled"


# --- ai --------------------------------------------------------------------------------------
class AIError(GdapError):
    code = "GDAP-6000"
    http_status = 502
    message = "AI layer failure"


class ToolNotAllowedError(PolicyViolationError):
    code = "GDAP-6001"
    message = "Agent is not allowed to use this tool"


class ModelError(GdapError):
    code = "GDAP-7000"
    http_status = 500
    message = "Model failure"
