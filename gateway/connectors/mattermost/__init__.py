"""Mattermost connector package."""

from .config import MattermostConfig
from .connector import MattermostConnector

__all__ = ["MattermostConfig", "MattermostConnector"]
