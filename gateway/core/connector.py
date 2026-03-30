"""Connector abstraction layer: abstract base and normalized message models.

This module defines the platform-agnostic interface that all messaging platform
integrations must implement.  The core library (SessionManager, MessageProcessor)
only ever deals with the types defined here — it never imports anything
platform-specific.

Design influences:
  - OpenClaw (github.com/openclaw/openclaw): decomposed adapter pattern,
    send_text/send_media split, delivery_mode, text_chunk_limit.
  - matterbridge: minimal 4-method Bridger interface.
  - OpenClaw security note: "trusted sender id from inbound context —
    server-injected, must never be sourced from tool/model-controlled params."
    This is the principle behind format_prompt_prefix().
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Literal

from ..agents.response import AgentResponse

# ---------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------

class UserRole(str, Enum):
    """Access level assigned to a sender by the Connector (never by the core)."""

    OWNER = "owner"
    GUEST = "guest"
    ANONYMOUS = "anonymous"


# ---------------------------------------------------------------------------
# Normalized data models
# ---------------------------------------------------------------------------

@dataclass
class Attachment:
    """A platform file attachment already resolved to a local path on disk.

    The Connector is responsible for downloading the file before handing the
    IncomingMessage to the core.  The core passes local_path directly to
    AgentBackend.send() — it never fetches from the platform itself.
    """

    original_name: str
    local_path: str      # Absolute path, ready for AgentBackend.send(attachments=[...])
    mime_type: str = ""
    size_bytes: int = 0


@dataclass
class Room:
    """Platform-agnostic channel / conversation descriptor.

    id   — opaque platform identifier used when sending replies (RC room _id,
            Slack channel ID, Discord channel snowflake, etc.)
    name — human-readable label (#channel, @username, "script", …)
    type — "channel" | "group" | "dm" | "thread" | "script"
    """

    id: str
    name: str
    type: str = "channel"


@dataclass
class User:
    """Platform-agnostic sender descriptor."""

    id: str
    username: str
    display_name: str = ""


@dataclass
class IncomingMessage:
    """Normalized inbound message — the only form the core library ever sees.

    All platform-specific parsing (DDP field extraction, @mention stripping,
    attachment downloads, deduplication) happens INSIDE the Connector before
    this object is created.  The Connector also resolves sender identity to a
    UserRole so the core never touches raw platform user data.

    The raw field preserves the original platform payload for debugging or
    platform-specific downstream handling (e.g. Slack blocks, Discord embeds).
    """

    id: str                                        # Platform message ID (dedup key)
    timestamp: str                                 # ISO 8601 or sortable string
    room: Room
    sender: User
    role: UserRole                                 # Resolved by Connector — NOT by core
    text: str                                      # Cleaned body (mention prefix stripped)
    attachments: list[Attachment] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # Human-readable warnings from the Connector (e.g. attachment download failures).
    # The core injects these into the agent prompt so the agent can inform the user.
    thread_id: str | None = None                   # Platform thread ID (RC tmid, etc.); None = top-level
    extra_context: dict[str, Any] = field(default_factory=dict)
    # Connector-computed behavioral hints (e.g. RC's "permission_thread_id").
    # Distinct from raw: raw holds the unmodified platform payload for debugging;
    # extra_context holds derived values the core/broker layer may act on.
    raw: dict[str, Any] = field(default_factory=dict)  # Original platform payload


# Handler type: the callback the core registers to receive inbound messages.
# Returns True if the message was accepted for processing, False if dropped
# (e.g. queue full).  Connectors use the return value to gate watermark advancement
# so that a dropped message is not silently marked as processed.
MessageHandler = Callable[[IncomingMessage], Awaitable[bool]]

# Capacity check: quick preflight to determine whether the core pipeline has
# room to accept a message for a given room_id.  Connectors call this BEFORE
# expensive work (normalize, attachment download) to short-circuit when the
# queue is already full.  Returns True if at least one processor has capacity.
CapacityCheck = Callable[[str], bool]  # (room_id: str) -> bool


# ---------------------------------------------------------------------------
# Connector ABC
# ---------------------------------------------------------------------------

class Connector(ABC):
    """Abstract base for all messaging platform integrations.

    A Connector is responsible for:
      1. Authenticating and establishing the platform connection.
      2. Receiving inbound messages, normalizing them to IncomingMessage, and
         firing the registered handler.
      3. Delivering outbound text and media back to the platform.
      4. Resolving sender identity to a UserRole (RBAC lives here, not in core).
      5. Optionally downloading platform file attachments to local disk.

    Transport model
    ---------------
    Pull-based (WebSocket / polling):
        Connector drives the event loop internally and fires handler() when a
        message arrives.  Callers use connect() then the Connector self-runs.

    Push-based (webhook):
        Connector exposes handle_webhook(); an external HTTP server calls it for
        each inbound POST.  Both models share the same register_handler() API.

    Design notes (from OpenClaw study)
    ------------------------------------
    * send_text / send_media are separate methods — platforms differ
      significantly in how they deliver text vs files (e.g. Slack text API vs
      file upload API are completely different endpoints).
    * delivery_mode makes the transport model explicit for SessionManager.
    * text_chunk_limit carries the per-platform message size constraint so the
      core can split long responses without knowing the platform.
    * format_prompt_prefix() injects a server-controlled trusted header into the
      agent prompt.  Per OpenClaw: "server-injected, must never be sourced from
      tool/model-controlled params."  This is the RBAC security boundary.
    * Optional capabilities (attachments, media, webhooks) use non-abstract
      methods with sensible defaults rather than forcing every connector to stub
      out methods it cannot support.
    """

    # ── Lifecycle ────────────────────────────────────────────────────────────

    @abstractmethod
    async def connect(self) -> None:
        """Authenticate and establish the platform connection.

        WebSocket platforms : login via REST/auth API, open WebSocket.
        Webhook platforms   : start HTTP server, or no-op if server is external.
        Script connector    : no-op.

        Must be called once before the Connector can receive or send messages.
        """
        ...

    @abstractmethod
    async def disconnect(self) -> None:
        """Graceful shutdown: close connections, cancel tasks, stop HTTP servers."""
        ...

    # ── Inbound — Observer pattern ───────────────────────────────────────────

    @abstractmethod
    def register_handler(self, handler: MessageHandler) -> None:
        """Register the callback the core uses to receive normalized messages.

        The Connector stores this and fires it for each valid inbound message,
        AFTER platform-level filtering (bot-self-filter, allowlist, @mention
        check, timestamp deduplication).

        Args:
            handler: Async callable that accepts an IncomingMessage.
        """
        ...

    def register_capacity_check(self, check: "CapacityCheck") -> None:
        """Register a preflight capacity check for two-phase inbound acceptance.

        Connectors that perform expensive work before dispatch (normalize,
        attachment download) should call this before the heavy phase to avoid
        wasting resources when the queue is already full.

        The default implementation is a no-op — connectors that don't perform
        expensive pre-dispatch work (e.g. ScriptConnector) need not override.
        """

    # ── Outbound ─────────────────────────────────────────────────────────────
    # Inspired by OpenClaw's ChannelOutboundAdapter split of sendText / sendMedia.

    @abstractmethod
    async def send_text(
        self,
        room_id: str,
        response: AgentResponse,
        thread_id: str | None = None,
    ) -> None:
        """Deliver an agent response to the platform room.

        The ``response.text`` field carries the primary reply.  Implementations
        may also inspect other fields (``response.is_error``, ``response.usage``,
        etc.) to adjust formatting, add metadata footers, or post error notices.

        ``thread_id`` — when set, the reply is posted inside the given thread
        (e.g. RC's ``tmid`` field).  Connectors that do not support threading
        should accept and silently ignore this parameter.

        Implementations should respect ``text_chunk_limit`` and split long text
        if needed.  The ``room_id`` is the opaque platform ID from ``Room.id``.
        """
        ...

    async def send_to_room(
        self,
        room: str,
        text: str,
        attachment_path: str | None = None,
    ) -> None:
        """Send a message (and optional file attachment) to a room by name or ID.

        This is a high-level convenience method used by the CLI ``send`` command
        via the control socket.  It resolves room names, sends text via
        ``send_text``, and uploads attachments via ``send_media``.

        Subclasses may override for platform-specific optimizations; the default
        implementation delegates to resolve_room / send_text / send_media.

        Args:
            room           : Room name or opaque platform room ID.
            text           : Message body to send (may be empty when only an
                             attachment is provided).
            attachment_path: Optional absolute path to a local file to upload.
        """
        from ..agents.response import AgentResponse

        resolved = await self.resolve_room(room)
        room_id = resolved.id

        if attachment_path:
            await self.send_media(room_id, attachment_path, caption=text)
        elif text:
            response = AgentResponse(text=text, session_id="")
            await self.send_text(room_id, response)

    async def send_media(
        self,
        room_id: str,
        file_path: str,
        caption: str = "",
    ) -> None:
        """Upload a local file to the platform room with an optional caption.

        Default: raises NotImplementedError.
        Override in connectors that support file upload (RC, Slack, Discord, …).

        Args:
            room_id  : Opaque platform room ID.
            file_path: Absolute local path to the file to upload.
            caption  : Optional description / caption for the uploaded file.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not support file upload"
        )

    # ── Room resolution ───────────────────────────────────────────────────────

    @abstractmethod
    async def resolve_room(self, room_name: str) -> Room:
        """Resolve a human-readable name to a platform Room object.

        RC     : channels.info / groups.info / im.create
        Slack  : conversations.info
        Script : in-memory Room(id=name, name=name, type="script")

        Args:
            room_name: Human-readable identifier e.g. "general", "@alice", "#dev".

        Returns:
            Populated Room dataclass with platform id, name, and type.
        """
        ...

    # ── Per-room subscription (pull-based platforms) ─────────────────────────
    # Rocket.Chat DDP requires explicit per-room WebSocket subscriptions.
    # Slack / Discord / WhatsApp / webhook connectors: default no-op.

    async def subscribe_room(self, room: Room, **kwargs: object) -> None:
        """Subscribe to inbound messages for this room.

        RC: opens a DDP stream-room-messages subscription for room.id.
        Other platforms: no-op (their transport already covers all rooms).

        Extra keyword arguments (e.g. watcher_id, working_directory) are
        accepted and ignored by the default implementation.  RC's override
        uses them to set up per-room watcher state.
        """
        pass

    async def unsubscribe_room(self, room_id: str, watcher_id: str = "") -> None:
        """Unsubscribe from this room's message stream.

        RC: cancels the DDP subscription when the last watcher leaves the room.
        Other platforms: no-op.

        Args:
            room_id   : Opaque platform room ID.
            watcher_id: ID of the watcher that is unsubscribing.  Used by
                        connectors that track per-watcher state (e.g. RC) to
                        remove the correct watcher context while keeping the
                        DDP subscription alive for any remaining watchers.
        """
        pass

    def get_last_processed_ts(self, room_id: str) -> str | None:
        """Return the last processed message timestamp for a room, or None.

        Override in connectors that track per-room deduplication timestamps.
        Default: no-op (returns None).
        """
        return None

    def update_last_processed_ts(self, room_id: str, ts: str) -> None:
        """Update the deduplication timestamp for a room after processing.

        Override in connectors that track per-room deduplication timestamps.
        Default: no-op.
        """
        pass

    # ── Attachment support ────────────────────────────────────────────────────

    def supports_attachments(self) -> bool:
        """Return True if this connector can download platform file attachments.

        The Connector must download attachments to local disk before calling the
        handler, so the agent receives file paths it can read directly.
        """
        return False

    async def download_attachment(self, ref: dict[str, Any], dest_path: str) -> None:
        """Download a platform file attachment to a local absolute path.

        Args:
            ref      : Platform-specific reference dict (e.g. RC's files[] entry
                       with title_link, or Slack's file object with url_private).
            dest_path: Absolute local path to write the downloaded file.

        Default: raises NotImplementedError.
        Override in connectors that carry file attachments.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not support attachment download"
        )

    def attachment_cache_dir(self, room_id: str) -> str | None:
        """Return the absolute path to the attachment cache directory for a room.

        Used by SessionManager to create per-watcher symlinks inside the agent's
        working directory, so the agent can read attachments without triggering
        out-of-project permission prompts.

        Default: None (no attachment caching).
        Override in connectors that download attachments to a global cache.
        """
        return None

    # ── Webhook entry point (push-based platforms) ────────────────────────────
    # The HTTP server lives OUTSIDE the connector (FastAPI, aiohttp, Flask, …).
    # The connector provides this single entry point; the server calls it.
    #
    # Platform signature algorithms:
    #   Slack     : HMAC-SHA256 of "v0:{timestamp}:{body}"  → X-Slack-Signature
    #   WhatsApp  : HMAC-SHA256 of raw body                 → X-Hub-Signature-256
    #   Discord   : Ed25519 of timestamp + body             → X-Signature-Ed25519
    #   Telegram  : token in URL path (no header signature)

    async def handle_webhook(
        self,
        headers: dict[str, str],
        body: bytes,
    ) -> dict[str, Any]:
        """Entry point for inbound webhook POST requests.

        The Connector must:
          1. Verify the platform's HMAC / Ed25519 signature.
          2. Handle one-time challenge handshakes (Slack URL verify,
             WhatsApp hub.verify_token, Discord PING → {"type": 1}).
          3. Parse the payload and emit normalized IncomingMessage(s) to handler.
          4. Return {"status": 200, "body": "OK"} or platform-required response.

        Raises:
            ValueError : If the signature verification fails.

        Default: raises NotImplementedError — WebSocket connectors don't use this.
        """
        raise NotImplementedError(
            f"{type(self).__name__} is not a webhook-based connector"
        )

    # ── Platform capability hints ─────────────────────────────────────────────
    # Inspired by OpenClaw's ChannelOutboundAdapter properties.

    @property
    def delivery_mode(self) -> Literal["direct", "gateway"]:
        """How outbound messages reach the platform.

        "direct"  — Connector sends directly via REST / API call.
        "gateway" — Connector proxies through an intermediary broker
                    (e.g. RC's DDP WebSocket gateway).

        Used by SessionManager to select timeout and retry strategies.
        """
        return "direct"

    @property
    def text_chunk_limit(self) -> int | None:
        """Maximum characters per outbound message, or None for no limit.

        Inspired by OpenClaw's ChannelOutboundAdapter.textChunkLimit.
        send_text() implementations should split responses that exceed this.

        Platform defaults:
            Rocket.Chat : ~40 000   Discord  : 2 000
            Slack       :  4 000   Telegram : 4 096
        """
        return None

    # ── Security: server-injected prompt prefix ───────────────────────────────

    def format_prompt_prefix(self, msg: IncomingMessage) -> str:
        """Return a trusted platform header to prepend to the agent prompt.

        This is the security boundary for RBAC enforcement.  The header is
        injected by the Connector (server-controlled) and parsed by CLAUDE.md
        instructions.  It must NEVER be derived from user-controlled content.

        Per OpenClaw: "trusted sender id from inbound context — server-injected,
        must never be sourced from tool/model-controlled params."

        RC returns : "[Rocket.Chat #general | from: alice | role: owner]"
        Others     : return "" (no prefix) or define their own convention.
                     Any new connector that uses RBAC MUST document its prefix
                     format in CLAUDE.md.
        """
        return ""

    # ── Optional status notifications ─────────────────────────────────────────

    async def notify_online(self, room_id: str, text: str) -> None:
        """Post a status message when the agent comes online in a room.

        Args:
            room_id: Opaque platform room ID.
            text   : Message text to post (watcher-configured, may include emoji/markdown).

        Default: no-op.  Override for platforms that support status messages.
        """
        pass

    async def notify_offline(self, room_id: str, text: str) -> None:
        """Post a status message when the agent goes offline in a room.

        Args:
            room_id: Opaque platform room ID.
            text   : Message text to post (watcher-configured, may include emoji/markdown).

        Default: no-op.  Override for platforms that support status messages.
        """
        pass

    async def notify_typing(self, room_id: str, is_typing: bool) -> None:
        """Signal that the agent is (or has stopped) typing in a room.

        Called by MessageProcessor immediately before and after agent.send().
        Platforms that auto-clear the indicator (e.g. Telegram, 5 s TTL) can
        ignore the is_typing=False call; platforms that require an explicit
        clear (e.g. Rocket.Chat) should send it.

        Connectors that need to keep a long-running indicator alive (Telegram)
        should start/cancel an internal refresh loop here rather than expecting
        the caller to repeat the call.

        Default: no-op.  Override in connectors that support typing indicators.
        """
        pass
