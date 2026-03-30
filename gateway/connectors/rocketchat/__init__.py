"""Rocket.Chat connector: REST + DDP/WebSocket platform integration."""

from .config import RocketChatConfig
from .connector import RocketChatConnector

__all__ = ["RocketChatConnector", "RocketChatConfig"]
