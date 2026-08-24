"""Notification channels (§22).

An alert is *raised* by the platform and *delivered* by channels. Channels are pluggable
(``gdap.notification_channels`` entry point group); the built-ins cover the two cases that need no
external account: structured log and outbound webhook. Every channel failure is contained — a
broken webhook must never fail the pipeline that raised the alert.
"""

from __future__ import annotations

from importlib.metadata import entry_points
from typing import Any

from gdap.core.contracts import AlertSpec
from gdap.observability.logging import get_logger

log = get_logger(__name__)

ENTRY_POINT_GROUP = "gdap.notification_channels"


class LogChannel:
    """Always available. Structured, greppable, and picked up by any log pipeline."""

    name = "log"

    def send(self, alert: AlertSpec) -> bool:
        logger = log.bind() if hasattr(log, "bind") else log
        payload: dict[str, Any] = {
            "rule": alert.rule,
            "severity": alert.severity.value,
            "title": alert.title,
            "message": alert.message,
            **{f"payload_{k}": v for k, v in list(alert.payload.items())[:10]},
        }
        if alert.severity.value == "critical":
            logger.error("alert", **payload)
        elif alert.severity.value == "warning":
            logger.warning("alert", **payload)
        else:
            logger.info("alert", **payload)
        return True


class StoreChannel:
    """No-op delivery: the alert row in the metadata store *is* the delivery."""

    name = "store"

    def send(self, alert: AlertSpec) -> bool:
        return True


class WebhookChannel:
    """POSTs the alert as JSON. URL and headers come from configuration, never from the alert."""

    name = "webhook"

    def __init__(
        self, url: str, *, headers: dict[str, str] | None = None, timeout: float = 10.0
    ) -> None:
        self.url = url
        self.headers = headers or {}
        self.timeout = timeout

    def send(self, alert: AlertSpec) -> bool:
        import httpx

        try:
            response = httpx.post(
                self.url,
                json={
                    "rule": alert.rule,
                    "severity": alert.severity.value,
                    "title": alert.title,
                    "message": alert.message,
                    "payload": alert.payload,
                },
                headers=self.headers,
                timeout=self.timeout,
            )
            response.raise_for_status()
            return True
        except Exception as exc:
            log.error("webhook_delivery_failed", url=self.url, error=str(exc))
            return False


class ChannelRegistry:
    def __init__(self) -> None:
        self._channels: dict[str, Any] = {c.name: c for c in (LogChannel(), StoreChannel())}
        self._loaded = False

    def register(self, channel: Any) -> None:
        self._channels[channel.name] = channel

    def configure_webhook(self, url: str, headers: dict[str, str] | None = None) -> None:
        self.register(WebhookChannel(url, headers=headers))

    def get(self, name: str) -> Any | None:
        self._load_external()
        return self._channels.get(name)

    def names(self) -> list[str]:
        self._load_external()
        return sorted(self._channels)

    def _load_external(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            discovered = entry_points(group=ENTRY_POINT_GROUP)
        except Exception:  # pragma: no cover
            return
        for entry in discovered:
            try:
                self.register(entry.load()())
                log.info("notification_channel_loaded", name=entry.name)
            except Exception as exc:
                log.error("notification_channel_failed", name=entry.name, error=str(exc))


CHANNELS = ChannelRegistry()
