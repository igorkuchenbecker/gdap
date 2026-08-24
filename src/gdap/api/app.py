"""FastAPI application factory.

The API is the platform's only public surface: the CLI and the Web UI are clients of it (§32).
Composition order matters — tracing wraps everything so a rate-limited or failed request still
carries a trace id.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from gdap import __version__
from gdap.api.errors import install_error_handlers
from gdap.api.middleware import RateLimitMiddleware, TracingMiddleware
from gdap.api.routers import (
    agents,
    alerts,
    analyses,
    datasets,
    governance,
    jobs,
    pipelines,
    reports,
    sources,
    system,
)
from gdap.core.config import Settings
from gdap.core.container import Platform, get_platform
from gdap.observability.logging import get_logger

log = get_logger(__name__)

WEB_DIR = Path(__file__).resolve().parents[3] / "web"

DESCRIPTION = """
**GDAP — Global Data Automation Platform**

Connect to data anywhere, discover its structure, validate and clean it, transform, analyse and
explain it, and automate the whole loop under governance.

* Every response error uses the envelope `{"error": {"code", "message", "details", "trace_id"}}`.
* Authentication is an API key in `X-API-Key` (or `Authorization: Bearer …`).
* Every state-changing call is recorded in the audit trail and, where data moves, in lineage.
"""


def create_app(settings: Settings | None = None, platform: Platform | None = None) -> FastAPI:
    active = platform or get_platform(settings)
    config = active.settings

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        active.db.create_all()
        log.info(
            "api_started",
            version=__version__,
            environment=config.environment,
            auth_enabled=config.security.auth_enabled,
        )
        yield
        log.info("api_stopping")

    app = FastAPI(
        title=f"{config.app_name} API",
        description=DESCRIPTION,
        version=__version__,
        docs_url="/docs" if config.api.docs_enabled else None,
        redoc_url="/redoc" if config.api.docs_enabled else None,
        openapi_url="/openapi.json" if config.api.docs_enabled else None,
        root_path=config.api.root_path,
        lifespan=lifespan,
    )
    app.state.platform = active

    app.add_middleware(
        RateLimitMiddleware,
        limit_per_minute=config.api.rate_limit_per_minute,
        header=config.security.api_key_header,
    )
    if config.api.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=config.api.cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
            expose_headers=["X-Request-ID", "X-Response-Time-ms"],
        )
    app.add_middleware(TracingMiddleware, header=config.observability.trace_header)

    install_error_handlers(app)

    for router in (
        system.router,
        sources.router,
        datasets.router,
        pipelines.router,
        jobs.router,
        analyses.router,
        reports.router,
        agents.router,
        alerts.router,
        governance.router,
    ):
        app.include_router(router)

    _mount_web_ui(app, config)
    return app


def _mount_web_ui(app: FastAPI, config: Settings) -> None:
    """Serve the bundled single-page UI, when present and enabled."""
    index = WEB_DIR / "index.html"
    if not (config.api.serve_web_ui and index.is_file()):

        @app.get("/", include_in_schema=False)
        def _root() -> JSONResponse:
            return JSONResponse(
                {
                    "name": config.app_name,
                    "version": __version__,
                    "docs": "/docs" if config.api.docs_enabled else None,
                    "health": "/health",
                }
            )

        return

    assets = WEB_DIR / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/", include_in_schema=False)
    def _index() -> FileResponse:
        return FileResponse(index)


def build_openapi(app: FastAPI) -> dict[str, Any]:
    """Exported so the CLI can dump the schema for client generation."""
    return app.openapi()
