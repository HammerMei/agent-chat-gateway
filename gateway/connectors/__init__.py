"""Platform connector implementations."""

from ..config import ConnectorConfig
from ..core.connector import Connector


def connector_factory(cc: ConnectorConfig) -> Connector:
    """Instantiate the correct Connector implementation from a ConnectorConfig.

    Supported types:
      - "rocketchat": Full Rocket.Chat DDP/REST connector
      - "script":     In-memory connector for testing and scripting
      - "voice":      HTTP voice gateway for Siri / iOS Shortcuts
    """
    if cc.type == "rocketchat":
        from .rocketchat import RocketChatConnector
        from .rocketchat.config import RocketChatConfig
        return RocketChatConnector(RocketChatConfig.from_connector_config(cc))
    if cc.type == "script":
        from .script import ScriptConnector
        return ScriptConnector(name=cc.name)
    if cc.type == "voice":
        from .voice import VoiceConnector
        from .voice.config import VoiceConfig
        return VoiceConnector(VoiceConfig.from_connector_config(cc))
    raise ValueError(
        f"Unknown connector type: {cc.type!r} (supported: 'rocketchat', 'script', 'voice')"
    )
