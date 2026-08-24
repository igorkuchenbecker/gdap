"""HTTP API (FastAPI). The Web UI and the CLI are clients of exactly this surface (§32)."""

from gdap.api.app import create_app

__all__ = ["create_app"]
