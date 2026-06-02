"""Configuration for the VoiceConnector."""

from __future__ import annotations

from dataclasses import dataclass

from ...core.config import ConnectorConfig


@dataclass
class VoiceConfig:
    """Configuration for the HTTP voice gateway connector.

    Attributes:
        port:    TCP port to bind the HTTP server on.
        host:    Bind address (default 0.0.0.0 for LAN access from iPhone).
        secret:  Bearer token required in Authorization header.
                 Empty string = no auth (dev/localhost only).
        timeout: Seconds to wait for the agent to reply before responding with
                 a polite timeout message.
    """

    port: int = 8765
    host: str = "0.0.0.0"
    secret: str = ""
    timeout: int = 45

    @classmethod
    def from_connector_config(cls, cc: ConnectorConfig) -> "VoiceConfig":
        raw = cc.raw
        return cls(
            port=int(raw.get("port", 8765)),
            host=str(raw.get("host", "0.0.0.0")),
            secret=str(raw.get("secret", "")),
            timeout=int(raw.get("timeout", 45)),
        )
