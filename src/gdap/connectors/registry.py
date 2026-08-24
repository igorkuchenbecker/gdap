"""Connector registry with third-party plugin discovery (§33).

Built-in connectors register themselves on import; external packages advertise theirs through the
``gdap.connectors`` entry-point group, so adding a connector never means editing the core.
"""

from __future__ import annotations

import builtins
from importlib.metadata import entry_points
from typing import Any

from gdap.connectors.base import BaseConnector, ConnectorPlugin
from gdap.core.contracts import SourceSpec
from gdap.core.errors import NotFoundError, PluginError, ValidationFailedError
from gdap.observability.logging import get_logger

log = get_logger(__name__)

ENTRY_POINT_GROUP = "gdap.connectors"


class ConnectorRegistry:
    def __init__(self) -> None:
        self._plugins: dict[str, ConnectorPlugin] = {}
        self._external_loaded = False

    def register(self, plugin: ConnectorPlugin, *, replace: bool = False) -> None:
        if plugin.key in self._plugins and not replace:
            raise PluginError(f"connector '{plugin.key}' is already registered")
        self._plugins[plugin.key] = plugin
        log.debug("connector_registered", key=plugin.key)

    def get(self, key: str) -> ConnectorPlugin:
        self.load_external()
        if key not in self._plugins:
            raise NotFoundError(
                f"unknown connector '{key}'",
                details={"available": sorted(self._plugins)},
            )
        return self._plugins[key]

    def create(self, spec: SourceSpec, secrets: dict[str, str] | None = None) -> BaseConnector:
        plugin = self.get(spec.connector)
        self.validate_config(spec)
        return plugin.create(spec, secrets or {})

    def validate_config(self, spec: SourceSpec) -> None:
        """Enforce the plugin's declared required keys before anything is instantiated."""
        schema = self.get(spec.connector).config_schema()
        required = schema.get("required", [])
        missing = [key for key in required if key not in spec.config]
        if missing:
            raise ValidationFailedError(
                f"connector '{spec.connector}' is missing required config: {', '.join(missing)}",
                details={"missing": missing, "schema": schema},
            )

    def list(self) -> builtins.list[dict[str, Any]]:
        self.load_external()
        return [plugin.describe() for plugin in sorted(self._plugins.values(), key=lambda p: p.key)]

    def keys(self) -> builtins.list[str]:
        self.load_external()
        return sorted(self._plugins)

    def load_external(self) -> None:
        if self._external_loaded:
            return
        self._external_loaded = True
        try:
            discovered = entry_points(group=ENTRY_POINT_GROUP)
        except Exception:  # pragma: no cover - importlib differences
            return
        for entry in discovered:
            try:
                plugin_cls = entry.load()
                self.register(plugin_cls(), replace=True)
                log.info("external_connector_loaded", key=entry.name)
            except Exception as exc:  # a broken plugin must not kill the platform
                log.error("external_connector_failed", key=entry.name, error=str(exc))


_REGISTRY = ConnectorRegistry()


def get_registry() -> ConnectorRegistry:
    """Registry singleton, with built-ins imported lazily to avoid import cycles."""
    if not _REGISTRY._plugins:
        from gdap.connectors import impl  # noqa: F401  (import registers built-ins)
    return _REGISTRY


def register_connector(plugin: ConnectorPlugin, *, replace: bool = False) -> None:
    _REGISTRY.register(plugin, replace=replace)
