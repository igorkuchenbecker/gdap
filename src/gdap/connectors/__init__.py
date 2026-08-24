"""Connector plugin system: one interface, many systems (§5)."""

from gdap.connectors.registry import ConnectorRegistry, get_registry, register_connector

__all__ = ["ConnectorRegistry", "get_registry", "register_connector"]
