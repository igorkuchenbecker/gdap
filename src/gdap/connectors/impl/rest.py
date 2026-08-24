"""REST/HTTP connector with pagination, auth and retry/backoff.

Covers the "authenticated JSON API" case that most SaaS/ERP/CRM integrations reduce to. Vendor
specifics stay in configuration — the core never learns about a particular vendor (§5).
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from typing import Any

import httpx
import polars as pl

from gdap.connectors.base import BaseConnector, ConnectorPlugin
from gdap.core.contracts import DiscoveredObject, ReadOptions, SourceSpec
from gdap.core.enums import SourceType
from gdap.core.errors import ConnectorError
from gdap.observability.logging import get_logger

log = get_logger(__name__)

_RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


class RestConnector(BaseConnector):
    """Config: ``url``; optional ``method``, ``record_path``, ``pagination``, ``auth``."""

    key = "rest"
    source_type = SourceType.REST

    def __init__(self, spec: SourceSpec, secrets: dict[str, str] | None = None) -> None:
        super().__init__(spec, secrets)
        self.url = str(self.require("url"))
        self.method = str(self.config.get("method", "GET")).upper()
        self.timeout = float(self.config.get("timeout", 30))
        self.max_retries = int(self.config.get("max_retries", 3))
        self.pagination: dict[str, Any] = self.config.get("pagination", {}) or {}
        self._client: httpx.Client | None = None

    # ------------------------------------------------------------------ transport
    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json", **(self.config.get("headers") or {})}
        auth = self.config.get("auth") or {}
        kind = str(auth.get("type", "none")).lower()
        if kind == "bearer":
            headers["Authorization"] = f"Bearer {self.secret('token')}"
        elif kind == "header":
            headers[str(auth.get("header_name", "X-API-Key"))] = self.secret("api_key")
        elif kind == "basic":
            import base64

            raw = f"{auth.get('username', '')}:{self.secret('password')}".encode()
            headers["Authorization"] = "Basic " + base64.b64encode(raw).decode()
        return {k: str(v) for k, v in headers.items()}

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                timeout=self.timeout,
                headers=self._headers(),
                verify=bool(self.config.get("verify_ssl", True)),
                follow_redirects=True,
            )
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def _request(self, url: str, params: dict[str, Any]) -> httpx.Response:
        backoff = float(self.config.get("retry_backoff", 1.0))
        last_error: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                response = self.client.request(
                    self.method,
                    url,
                    params=params or None,
                    json=self.config.get("body")
                    if self.method in {"POST", "PUT", "PATCH"}
                    else None,
                )
                if response.status_code in _RETRYABLE_STATUS:
                    raise httpx.HTTPStatusError(
                        f"retryable status {response.status_code}",
                        request=response.request,
                        response=response,
                    )
                response.raise_for_status()
                return response
            except (httpx.HTTPError, httpx.StreamError) as exc:
                last_error = exc
                if attempt == self.max_retries:
                    break
                sleep_for = backoff * (2 ** (attempt - 1))
                log.warning(
                    "rest_retry", source=self.name, attempt=attempt, sleep=sleep_for, error=str(exc)
                )
                time.sleep(sleep_for)
        raise ConnectorError(
            f"request failed after {self.max_retries} attempts: {last_error}",
            details={"url": url},
            cause=last_error,
        )

    # ------------------------------------------------------------------ parsing
    def _extract(self, payload: Any) -> list[dict[str, Any]]:
        record_path = self.config.get("record_path")
        data = payload
        if record_path:
            for part in str(record_path).split("."):
                if part == "":
                    continue
                if isinstance(data, dict):
                    if part not in data:
                        raise ConnectorError(
                            f"record_path segment '{part}' not found",
                            details={"available": sorted(data)[:20]},
                        )
                    data = data[part]
                else:
                    raise ConnectorError(f"record_path '{record_path}' does not match the payload")
        if isinstance(data, dict):
            data = [data]
        if not isinstance(data, list):
            raise ConnectorError("response did not resolve to a list of records")
        return [record if isinstance(record, dict) else {"value": record} for record in data]

    # ------------------------------------------------------------------ interface
    def discover(self) -> list[DiscoveredObject]:
        response = self._request(self.url, dict(self.config.get("params") or {}))
        records = self._extract(response.json())
        return [
            DiscoveredObject(
                name=self.config.get("name", self.url.rsplit("/", 1)[-1] or "endpoint"),
                kind="endpoint",
                location=self.url,
                estimated_rows=len(records),
                extra={"status_code": response.status_code},
            )
        ]

    def read(self, options: ReadOptions) -> Iterator[pl.DataFrame]:
        params = dict(self.config.get("params") or {})
        if options.incremental_column and options.since is not None:
            since_param = self.pagination.get("since_param", "since")
            params[since_param] = str(options.since)

        mode = str(self.pagination.get("type", "none")).lower()
        page_param = self.pagination.get("page_param", "page")
        size_param = self.pagination.get("size_param", "per_page")
        page_size = int(self.pagination.get("page_size", 0) or 0)
        max_pages = int(self.pagination.get("max_pages", 100))
        if page_size:
            params[size_param] = page_size

        url: str | None = self.url
        page = int(self.pagination.get("start_page", 1))
        emitted = 0

        for iteration in range(max_pages):
            if url is None:
                return
            if mode == "page":
                params[page_param] = page + iteration
            elif mode == "offset":
                params[self.pagination.get("offset_param", "offset")] = emitted

            response = self._request(url, params)
            payload = response.json()
            records = self._extract(payload)
            if not records:
                return

            frame = pl.DataFrame(records, infer_schema_length=None, strict=False)
            if options.columns:
                frame = frame.select([c for c in options.columns if c in frame.columns])
            if options.limit is not None and emitted + frame.height > options.limit:
                frame = frame.head(options.limit - emitted)
            emitted += frame.height
            yield frame

            if options.limit is not None and emitted >= options.limit:
                return
            if mode == "none":
                return
            if mode == "cursor":
                cursor = _dig(payload, self.pagination.get("cursor_path", "next_cursor"))
                if not cursor:
                    return
                params[self.pagination.get("cursor_param", "cursor")] = cursor
            elif mode == "link":
                url = _dig(payload, self.pagination.get("next_link_path", "next")) or _next_link(
                    response
                )
                params = {}
            elif page_size and frame.height < page_size:
                return


def _dig(payload: Any, path: str) -> Any:
    current = payload
    for part in str(path).split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return None
    return current


def _next_link(response: httpx.Response) -> str | None:
    link = response.headers.get("Link", "")
    for section in link.split(","):
        if 'rel="next"' in section:
            return section.split(";")[0].strip().strip("<>")
    return None


class RestPlugin(ConnectorPlugin):
    key = "rest"
    source_type = SourceType.REST
    title = "REST / HTTP JSON API"
    description = "Authenticated JSON endpoints with pagination, retries and incremental reads."

    def config_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "required": ["url"],
            "properties": {
                "url": {"type": "string"},
                "method": {"type": "string", "enum": ["GET", "POST"], "default": "GET"},
                "params": {"type": "object"},
                "headers": {"type": "object"},
                "body": {"type": "object"},
                "record_path": {"type": "string", "description": "dotted path to the array"},
                "timeout": {"type": "number", "default": 30},
                "max_retries": {"type": "integer", "default": 3},
                "verify_ssl": {"type": "boolean", "default": True},
                "auth": {
                    "type": "object",
                    "properties": {
                        "type": {"enum": ["none", "bearer", "basic", "header"]},
                        "header_name": {"type": "string"},
                        "username": {"type": "string"},
                    },
                },
                "pagination": {
                    "type": "object",
                    "properties": {
                        "type": {"enum": ["none", "page", "offset", "cursor", "link"]},
                        "page_param": {"type": "string", "default": "page"},
                        "size_param": {"type": "string", "default": "per_page"},
                        "page_size": {"type": "integer"},
                        "max_pages": {"type": "integer", "default": 100},
                        "cursor_path": {"type": "string"},
                        "next_link_path": {"type": "string"},
                    },
                },
            },
            "secret_refs": {"token": "env:API_TOKEN", "api_key": "env:API_KEY"},
        }

    def create(self, spec: SourceSpec, secrets: dict[str, str]) -> BaseConnector:
        return RestConnector(spec, secrets)
