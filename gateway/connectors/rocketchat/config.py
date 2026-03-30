"""Rocket.Chat-specific configuration dataclass."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ...config import AttachmentConfig, ConnectorConfig

logger = logging.getLogger(__name__)


@dataclass
class RocketChatConfig:
    """All Rocket.Chat platform configuration in one place.

    Separates RC-specific concerns (server URL, credentials, user allow-lists)
    from the generic gateway config (agent type, timeout, etc.).

    Construct via from_connector_config() to derive from a ConnectorConfig,
    or build directly for testing.
    """

    server_url: str
    username: str
    password: str
    name: str = ""  # connector name — used to namespace the global attachment cache dir
    owners: list[str] = field(default_factory=list)
    guests: list[str] = field(default_factory=list)
    attachments: AttachmentConfig = field(default_factory=AttachmentConfig)
    reply_in_thread: bool = False
    # When True, top-level (non-threaded) messages trigger a proactive thread:
    # the agent reply is posted as tmid=triggering_message_id, starting a new thread.
    permission_reply_in_thread: bool = True
    # When True, 🔐 permission notifications are posted in a thread anchored to the
    # triggering message (keeps the main channel clean). Independent of reply_in_thread.

    @property
    def allow_senders(self) -> list[str]:
        """All users permitted to interact — owners + guests."""
        return self.owners + self.guests

    def role_of(self, username: str) -> str:
        """Return 'owner' or 'guest' for a given username.

        If the username is not in ``owners``, 'guest' is returned as a fallback —
        even if the username is not in ``guests`` either.  Unknown users are
        treated as guests rather than raising an error.  The caller is expected
        to have already filtered messages via ``filter_rc_message`` / ``allow_senders``
        before calling this method.
        """
        if username in self.owners:
            return "owner"
        if username not in self.guests:
            logger.debug("role_of: unknown user %r — defaulting to 'guest'", username)
        return "guest"

    @classmethod
    def from_connector_config(cls, cc: ConnectorConfig) -> "RocketChatConfig":
        """Build a RocketChatConfig from a ConnectorConfig.

        The ConnectorConfig.raw dict is expected to contain:
            server:        {url, username, password}
            allowed_users: {owners: [...], guests: [...]}
            attachments:   {max_file_size_mb, download_timeout, cache_dir}  (all optional)
        """
        raw = cc.raw
        server = raw.get("server", {})
        allowed_users = raw.get("allowed_users", {})
        attach_raw = raw.get("attachments", {})

        attach_cfg = AttachmentConfig(
            max_file_size_mb=attach_raw.get("max_file_size_mb", 10.0),
            download_timeout=attach_raw.get("download_timeout", 30),
            cache_dir=attach_raw.get("cache_dir", "agent-chat.cache"),
            cache_dir_global=attach_raw.get(
                "cache_dir_global", "~/.agent-chat-gateway/attachments"
            ),
        )

        return cls(
            server_url=server.get("url", "").rstrip("/"),
            username=server.get("username", ""),
            password=server.get("password", ""),
            name=cc.name,
            owners=allowed_users.get("owners", []),
            guests=allowed_users.get("guests", []),
            attachments=attach_cfg,
            reply_in_thread=raw.get("reply_in_thread", False),
            permission_reply_in_thread=raw.get("permission_reply_in_thread", True),
        )
