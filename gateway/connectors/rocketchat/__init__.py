"""Rocket.Chat connector: REST + DDP/WebSocket platform integration."""

from .connector import RocketChatConnector
from .config import RocketChatConfig

__all__ = ["RocketChatConnector", "RocketChatConfig"]
