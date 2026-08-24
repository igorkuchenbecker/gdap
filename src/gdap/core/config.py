"""Typed configuration.

Precedence (highest first): explicit init kwargs → environment variables → ``.env`` →
``config/<environment>.yaml`` → ``config/default.yaml`` → field defaults.

Nothing operational is hardcoded and no secret value is ever stored in this object: secrets are
referenced (``env:VAR`` / ``file:/path``) and resolved on demand by
:mod:`gdap.security.secrets`.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

Environment = Literal["development", "testing", "staging", "production"]

DEFAULT_HOME = Path(os.environ.get("GDAP_HOME") or (Path.home() / ".gdap"))
CONFIG_DIR = Path(os.environ.get("GDAP_CONFIG_DIR") or (Path.cwd() / "config"))


class PathsSettings(BaseModel):
    """Filesystem layout. Everything the platform writes lives under ``home``."""

    home: Path = DEFAULT_HOME
    warehouse: Path | None = None  # versioned dataset files (parquet)
    artifacts: Path | None = None  # reports, charts, exports
    models: Path | None = None  # serialized ML models
    staging: Path | None = None  # in-flight ingestion buffers

    def resolved(self) -> PathsSettings:
        home = self.home.expanduser()
        return PathsSettings(
            home=home,
            warehouse=(self.warehouse or home / "warehouse").expanduser(),
            artifacts=(self.artifacts or home / "artifacts").expanduser(),
            models=(self.models or home / "models").expanduser(),
            staging=(self.staging or home / "staging").expanduser(),
        )


class DatabaseSettings(BaseModel):
    """Operational metadata store (SQLite for dev/single node, PostgreSQL for production)."""

    url: str | None = None
    echo: bool = False
    pool_size: int = 5
    max_overflow: int = 10
    pool_pre_ping: bool = True


class ApiSettings(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8000
    root_path: str = ""
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:5173"])
    docs_enabled: bool = True
    max_upload_mb: int = 512
    rate_limit_per_minute: int = 240
    serve_web_ui: bool = True


class SecuritySettings(BaseModel):
    """Secure defaults: auth on, destructive SQL off, masking on for restricted data."""

    auth_enabled: bool = True
    allow_anonymous_reads: bool = False
    api_key_header: str = "X-API-Key"
    sql_write_enabled: bool = False
    sql_destructive_enabled: bool = False
    sql_statement_timeout_s: int = 30
    sql_max_rows: int = 100_000
    mask_restricted_columns: bool = True
    secret_backend: Literal["env", "file"] = "env"  # noqa: S105 - a backend name, not a secret
    secret_file_root: Path | None = None
    bootstrap_api_key: str | None = None  # dev convenience; must be a secret ref in prod


class IngestionSettings(BaseModel):
    chunk_rows: int = 250_000
    max_retries: int = 3
    retry_backoff_s: float = 2.0
    retry_backoff_max_s: float = 60.0
    infer_schema_rows: int = 10_000
    allow_schema_evolution: bool = True
    default_preview_rows: int = 100


class QualitySettings(BaseModel):
    """Weights per quality dimension; must be re-normalised if edited."""

    fail_below_score: float = 60.0
    warn_below_score: float = 85.0
    weights: dict[str, float] = Field(
        default_factory=lambda: {
            "completeness": 0.25,
            "validity": 0.20,
            "uniqueness": 0.15,
            "consistency": 0.15,
            "accuracy": 0.10,
            "timeliness": 0.10,
            "integrity": 0.05,
        }
    )


class WorkerSettings(BaseModel):
    concurrency: int = 2
    poll_interval_s: float = 1.0
    lease_seconds: int = 300
    heartbeat_seconds: int = 30
    scheduler_enabled: bool = True
    max_job_runtime_s: int = 3600


class AISettings(BaseModel):
    """Provider-agnostic. ``heuristic`` keeps the platform fully functional with no credentials."""

    provider: Literal["heuristic", "anthropic"] = "heuristic"
    model: str = "claude-opus-5"
    api_key_ref: str = "env:ANTHROPIC_API_KEY"
    max_tokens: int = 8192
    #: Sampling parameters are rejected by current Claude models; kept for legacy/self-hosted
    #: providers only, and never sent unless ``legacy_sampling`` is enabled.
    temperature: float | None = None
    legacy_sampling: bool = False
    thinking: Literal["adaptive", "off"] = "adaptive"
    effort: Literal["low", "medium", "high", "xhigh", "max"] = "high"
    max_tool_iterations: int = 8
    timeout_s: int = 120
    enabled: bool = True


class ObservabilitySettings(BaseModel):
    log_level: str = "INFO"
    log_format: Literal["console", "json"] = "console"
    log_file: Path | None = None
    metrics_enabled: bool = True
    trace_header: str = "X-Request-ID"
    slow_step_ms: int = 5_000


class LocaleSettings(BaseModel):
    """No implicit USD/en-US/UTC-3 assumptions anywhere in the platform."""

    default_locale: str = "en_US"
    default_timezone: str = "UTC"
    default_currency: str = "USD"
    date_format: str = "%Y-%m-%d"
    datetime_format: str = "%Y-%m-%d %H:%M:%S%z"
    decimal_separator: str = "."
    thousands_separator: str = ","


class Settings(BaseSettings):
    """Root settings object. Inject it; never read environment variables elsewhere."""

    model_config = SettingsConfigDict(
        env_prefix="GDAP_",
        env_nested_delimiter="__",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        validate_default=True,
    )

    environment: Environment = "development"
    app_name: str = "GDAP"
    debug: bool = False
    default_org_slug: str = "default"

    paths: PathsSettings = Field(default_factory=PathsSettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    api: ApiSettings = Field(default_factory=ApiSettings)
    security: SecuritySettings = Field(default_factory=SecuritySettings)
    ingestion: IngestionSettings = Field(default_factory=IngestionSettings)
    quality: QualitySettings = Field(default_factory=QualitySettings)
    worker: WorkerSettings = Field(default_factory=WorkerSettings)
    ai: AISettings = Field(default_factory=AISettings)
    observability: ObservabilitySettings = Field(default_factory=ObservabilitySettings)
    locale: LocaleSettings = Field(default_factory=LocaleSettings)

    @field_validator("paths")
    @classmethod
    def _resolve_paths(cls, value: PathsSettings) -> PathsSettings:
        return value.resolved()

    @property
    def database_url(self) -> str:
        if self.database.url:
            return self.database.url
        return f"sqlite:///{self.paths.home / 'gdap.db'}"

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    def ensure_directories(self) -> None:
        """Create the writable layout. Called once at startup, idempotent."""
        p = self.paths
        for directory in (p.home, p.warehouse, p.artifacts, p.models, p.staging):
            if directory is not None:
                directory.mkdir(parents=True, exist_ok=True)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            _YamlSource(settings_cls),
            file_secret_settings,
        )


class _YamlSource(PydanticBaseSettingsSource):
    """Reads ``config/default.yaml`` then overlays ``config/<environment>.yaml``."""

    def __init__(self, settings_cls: type[BaseSettings]) -> None:
        super().__init__(settings_cls)
        self._data = self._load()

    def _load(self) -> dict[str, Any]:
        env = os.environ.get("GDAP_ENVIRONMENT", "development")
        merged: dict[str, Any] = {}
        for name in ("default", env):
            path = CONFIG_DIR / f"{name}.yaml"
            if path.is_file():
                loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
                if not isinstance(loaded, dict):
                    raise ValueError(f"{path} must contain a YAML mapping")
                merged = _deep_merge(merged, loaded)
        return merged

    def get_field_value(self, field: Any, field_name: str) -> tuple[Any, str, bool]:
        return self._data.get(field_name), field_name, False

    def __call__(self) -> dict[str, Any]:
        return self._data


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton (cache-cleared by tests through :func:`reset_settings`)."""
    return Settings()


def reset_settings() -> None:
    get_settings.cache_clear()
