"""RocketChatConnector: full Connector implementation for Rocket.Chat.

Encapsulates ALL Rocket.Chat-specific knowledge:
  - DDP WebSocket subscription per room (subscribe_room / unsubscribe_room)
  - REST API calls for posting text and uploading files
  - Inbound message filtering (bot-self, allow-list, @mention, timestamp dedup)
  - Inbound message normalization (field extraction, attachment download)
  - Role resolution from allow-list config (RBAC lives here, not in core)
  - Per-room state tracking (room type, last processed timestamp, cache path)

The core library (SessionManager, MessageProcessor) interacts with this
connector only through the Connector ABC defined in gateway.core.connector.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ...agents.response import AgentEvent, AgentResponse
from ...core.connector import (
    Connector,
    IncomingMessage,
    MessageHandler,
    Room,
)
from .config import RocketChatConfig
from .normalize import FilterResult, filter_rc_message, normalize_rc_message
from .outbound import send_media as _send_media
from .outbound import send_text as _send_text
from .policy import apply_thread_policy
from .rest import RocketChatREST, RoomNotFoundError
from .websocket import RCWebSocketClient

logger = logging.getLogger("agent-chat-gateway.connectors.rocketchat")


# ---------------------------------------------------------------------------
# Per-room runtime state (internal to the connector)
# ---------------------------------------------------------------------------


@dataclass
class _RoomSubscription:
    """Connector-level room state: platform subscription + shared dedup watermark.

    Owned by the connector, not by any individual watcher.
    """

    room: Room
    last_processed_ts: str | None = None


@dataclass
class _WatcherRoomContext:
    """Per-watcher subscription membership for a shared room.

    The connector tracks watcher IDs for refcounting — when the last watcher
    for a room is removed, the DDP subscription is torn down.  All per-watcher
    filesystem concerns (working directory, attachment workspace) live in the
    core layer (``WatcherLifecycle`` / ``AttachmentWorkspace``), not here.
    """

    watcher_id: str


# ---------------------------------------------------------------------------
# RocketChatConnector
# ---------------------------------------------------------------------------


class RocketChatConnector(Connector):
    """Connector for Rocket.Chat (REST + DDP/WebSocket).

    Usage::

        config = RocketChatConfig.from_gateway_config(gateway_cfg)
        connector = RocketChatConnector(config)

        connector.register_handler(my_handler)
        await connector.connect()

        room = await connector.resolve_room("general")
        await connector.subscribe_room(
            room,
            watcher_id="abc123",
            working_directory="/path/to/cwd",
        )

        # ... messages arrive, handler is called ...

        await connector.disconnect()
    """

    @property
    def delivery_mode(self):
        """Delivery goes through the RC DDP gateway broker."""
        return "gateway"

    _TEXT_CHUNK_LIMIT = 40_000

    def __init__(self, config: RocketChatConfig) -> None:
        self._config = config
        self._rest = RocketChatREST(config.server_url)
        self._ws = RCWebSocketClient(
            config.server_url, config.username, config.password
        )
        self._handler: MessageHandler | None = None
        self._capacity_check: Callable[[str], bool] | None = None
        self._rooms: dict[str, _RoomSubscription] = {}  # room_id -> subscription
        self._watcher_contexts: dict[
            str, list[_WatcherRoomContext]
        ] = {}  # room_id -> [watcher...]
        self._room_refcount: dict[str, int] = {}  # room_id -> subscriber count
        # Global attachment cache base: {cache_dir_global}/{connector_name}/{room_id}/
        # Namespaced by connector name to avoid collisions across multi-connector deployments.
        self._attachments_cache_base = (
            Path(config.attachments.cache_dir_global).expanduser() / config.name
        )

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def connect(self) -> None:
        """Login via REST and establish the DDP WebSocket connection."""
        await self._rest.login(self._config.username, self._config.password)
        await self._ws.connect()
        await self._ws.start()
        logger.info(
            "RocketChatConnector connected to %s as %s",
            self._config.server_url,
            self._config.username,
        )

    async def disconnect(self) -> None:
        """Close the WebSocket and release HTTP client resources."""
        await self._ws.stop()
        await self._rest.close()
        logger.info("RocketChatConnector disconnected")

    # ── Inbound ──────────────────────────────────────────────────────────────

    def register_handler(self, handler: MessageHandler) -> None:
        self._handler = handler

    def register_capacity_check(self, check) -> None:
        self._capacity_check = check

    # ── Outbound ─────────────────────────────────────────────────────────────

    async def send_text(
        self,
        room_id: str,
        response: AgentResponse,
        thread_id: str | None = None,
    ) -> None:
        """Post an agent response to the room.

        Uses ``response.text`` as the message body.  When ``response.is_error``
        is True the text is delivered as-is (already contains an error prefix).
        ``thread_id`` is forwarded as RC's ``tmid`` so the reply lands in the
        correct thread.
        """
        await _send_text(
            self._rest,
            room_id,
            response.text,
            chunk_limit=self.text_chunk_limit,
            tmid=thread_id,
        )

    async def notify_agent_event(
        self,
        room_id: str,
        event: AgentEvent,
        thread_id: str | None = None,
    ) -> None:
        """Refresh the typing indicator on each intermediate agent event.

        RC's typing indicator auto-expires after ~10 seconds.  For long-running
        turns (tool calls, permission approvals, extended thinking) this means
        the indicator vanishes mid-turn, leaving the user with no feedback.

        Re-triggering it on every non-final AgentEvent keeps it alive for the
        full duration without posting any messages (no delete permissions needed,
        no placeholder race conditions).

        All errors are silently swallowed — a failed typing refresh must never
        abort an agent turn.
        """
        if event.kind == "final":
            return
        try:
            await self.notify_typing(room_id, True)
        except Exception as exc:
            logger.debug(
                "Failed to refresh typing indicator for room %s: %s", room_id, exc
            )

    async def send_media(self, room_id: str, file_path: str, caption: str = "") -> None:
        """Upload a local file to the room."""
        await _send_media(self._rest, room_id, file_path, caption)

    async def send_to_room(
        self,
        room: str,
        text: str,
        attachment_path: str | None = None,
    ) -> None:
        """Send a message (and optional attachment) to a room by name or ID.

        Overrides the base Connector implementation to use the RC REST client
        directly for efficient room resolution and delivery.
        """
        # Resolve room name to ID.
        # Only fall back to treating the input as a raw room ID when the room
        # was genuinely not found.  Broader failures (auth, network, API errors)
        # are re-raised so callers receive an accurate error.
        try:
            room_info = await self._rest.resolve_room(room)
            room_id = room_info["_id"]
        except RoomNotFoundError:
            # Input is likely already a room ID — use it directly.
            room_id = room

        if attachment_path:
            await self._rest.upload_file(room_id, attachment_path, caption=text)
        elif text:
            await self._rest.post_message(room_id, text)

    def supports_attachments(self) -> bool:
        return True

    async def download_attachment(self, ref: dict, dest_path: str) -> None:
        """Download a RC file attachment (identified by title_link) to dest_path."""
        title_link = ref.get("title_link", "")
        await self._rest.download_file(title_link, dest_path)

    # ── Room resolution ───────────────────────────────────────────────────────

    async def resolve_room(self, room_name: str) -> Room:
        """Resolve a human-readable room name to a Room object via REST."""
        info = await self._rest.resolve_room(room_name)
        return Room(
            id=info["_id"],
            name=info.get("name", room_name),
            type=info.get("type", "channel"),
        )

    # ── Per-room subscription ─────────────────────────────────────────────────

    async def subscribe_room(
        self,
        room: Room,
        watcher_id: str = "",
        working_directory: str = "",
    ) -> None:
        """Subscribe to DDP stream-room-messages for this room.

        Each call registers a new per-watcher context even if the DDP
        subscription already exists.  The DDP subscription is opened only once
        (on the first subscriber); subsequent callers increment the refcount and
        append their watcher context.

        Args:
            room              : Resolved Room to subscribe to.
            watcher_id        : Unique ID for the watcher; used as the
                                attachment cache subdirectory name.
            working_directory : Base path for attachment cache storage.
        """
        ctx = _WatcherRoomContext(
            watcher_id=watcher_id or room.id,
        )

        if room.id in self._rooms:
            self._room_refcount[room.id] += 1
            self._watcher_contexts.setdefault(room.id, []).append(ctx)
            logger.debug(
                "Room '%s' (id=%s) already subscribed — added watcher '%s', refcount=%d",
                room.name,
                room.id,
                ctx.watcher_id,
                self._room_refcount[room.id],
            )
            return

        self._rooms[room.id] = _RoomSubscription(room=room)
        self._watcher_contexts[room.id] = [ctx]
        self._room_refcount[room.id] = 1

        try:
            await self._ws.subscribe_room(room.id, self._make_ddp_callback(room.id))
        except Exception:
            # DDP subscription failed — roll back the connector-level state so
            # there is no dangling entry with a refcount of 1 and no live subscription.
            self._rooms.pop(room.id, None)
            self._watcher_contexts.pop(room.id, None)
            self._room_refcount.pop(room.id, None)
            raise

        logger.info(
            "Subscribed to room '%s' (id=%s, type=%s)",
            room.name,
            room.id,
            room.type,
        )

    async def unsubscribe_room(self, room_id: str, watcher_id: str = "") -> None:
        """Remove a watcher from a room; cancel the DDP subscription when the last watcher leaves.

        Args:
            room_id   : Platform room ID.
            watcher_id: ID of the departing watcher.  Its ``_WatcherRoomContext``
                        is removed regardless of the refcount; the DDP subscription
                        is cancelled only when the refcount reaches zero.
        """
        # Remove the specific watcher context and track whether it was found.
        # The refcount must only be decremented when an actual watcher is removed;
        # calling unsubscribe_room with a stale/unknown watcher_id must be a no-op.
        removed = False
        if room_id in self._watcher_contexts and watcher_id:
            before = self._watcher_contexts[room_id]
            after = [ctx for ctx in before if ctx.watcher_id != watcher_id]
            removed = len(after) < len(before)
            self._watcher_contexts[room_id] = after

        if room_id in self._room_refcount:
            if removed:
                self._room_refcount[room_id] -= 1
            if self._room_refcount[room_id] > 0:
                logger.debug(
                    "Room %s still has %d active watcher(s) — skipping DDP unsubscribe",
                    room_id,
                    self._room_refcount[room_id],
                )
                return
            del self._room_refcount[room_id]

        self._rooms.pop(room_id, None)
        self._watcher_contexts.pop(room_id, None)
        await self._ws.unsubscribe_room(room_id)
        logger.info("Unsubscribed from room %s", room_id)

    def update_last_processed_ts(self, room_id: str, ts: str) -> None:
        """Update the deduplication timestamp for a room after processing."""
        if room_id in self._rooms:
            self._rooms[room_id].last_processed_ts = ts

    def get_last_processed_ts(self, room_id: str) -> str | None:
        """Return the last processed message timestamp for a room."""
        sub = self._rooms.get(room_id)
        return sub.last_processed_ts if sub else None

    # ── Attachment cache ────────────────────────────────────────────────────────

    def attachment_cache_dir(self, room_id: str) -> str | None:
        """Return the global cache directory for a room's attachments."""
        return str(self._attachments_cache_base / room_id)

    @property
    def text_chunk_limit(self) -> int | None:
        """Maximum outbound text size before RC responses are split into chunks."""
        return self._TEXT_CHUNK_LIMIT

    # ── Security: server-injected prompt prefix ───────────────────────────────

    # Characters that are illegal in this protocol's delimiter grammar.
    # The prefix format uses '|' as a field separator and ']' as the closing
    # bracket.  A room name or username containing these could be crafted by a
    # malicious RC admin to inject fake role fields (e.g. "| role: owner") and
    # bypass RBAC enforcement in CLAUDE.md.  Stripping them here closes the gap.
    _PREFIX_UNSAFE_RE = re.compile(r"[\|\[\]\r\n]")

    def format_prompt_prefix(self, msg: IncomingMessage) -> str:
        """Return the trusted RC identity header for the agent prompt.

        This is server-controlled and parsed by CLAUDE.md as the security
        boundary for RBAC enforcement.  It must never be derived from
        user-controlled content.

        room.name and sender.username are sanitized to remove characters that
        could be used to inject fake delimiter fields (``|``, ``[``, ``]``,
        newlines).  role.value is an enum — not user-controlled.
        """
        safe_room = self._PREFIX_UNSAFE_RE.sub("_", msg.room.name)
        safe_user = self._PREFIX_UNSAFE_RE.sub("_", msg.sender.username)
        return (
            f"[Rocket.Chat #{safe_room} | "
            f"from: {safe_user} | "
            f"role: {msg.role.value}]"
        )

    # ── Status notifications ──────────────────────────────────────────────────

    async def notify_typing(self, room_id: str, is_typing: bool) -> None:
        """Send a typing indicator via DDP WebSocket.

        RC 7.x replaced the old stream-notify-room/typing event with
        stream-notify-room/user-activity.  The event args are:
          typing=True:  [username, ["user-typing"], {}]
          typing=False: [username, [],              {}]
        """
        activity = ["user-typing"] if is_typing else []
        await self._ws.call_method(
            "stream-notify-room",
            [f"{room_id}/user-activity", self._config.username, activity],
        )

    async def notify_online(self, room_id: str, text: str) -> None:
        try:
            await self._rest.post_message(room_id, text)
        except Exception as e:
            logger.warning("Failed to post online notification: %s", e)

    async def notify_offline(self, room_id: str, text: str) -> None:
        try:
            await self._rest.post_message(room_id, text)
        except Exception as e:
            logger.warning("Failed to post offline notification: %s", e)

    # ── Internal: DDP callback factory ───────────────────────────────────────

    def _make_ddp_callback(self, room_id: str):
        """Return the async callback that the WebSocket client calls for each DDP message."""

        async def on_raw_ddp_message(doc: dict) -> None:
            await self._enqueue_room_doc(room_id, doc)

        return on_raw_ddp_message

    async def _enqueue_room_doc(self, room_id: str, doc: dict) -> None:
        """Forward one raw DDP doc into connector normalization/dispatch.

        Per-room buffering and ordering already live in ``RCWebSocketClient``.
        Keeping a second connector-owned room queue here duplicated backpressure
        and blurred the transport-vs-connector boundary.  The callback now
        relies on the transport layer's queue and proceeds directly to the
        connector-specific normalize/filter step.
        """
        await self._on_raw_ddp_message(room_id, doc)

    async def _on_raw_ddp_message(self, room_id: str, doc: dict) -> None:
        """Parse a raw RC DDP message doc, filter it, normalize it, fire handler.

        Filtering and deduplication are room-level (done once).
        Normalization and dispatch are per-watcher so each watcher gets its own
        attachment cache path and its own IncomingMessage instance.

        This is the boundary where all RC-specific field names disappear.
        After this method, only IncomingMessage objects exist in the codebase.
        """
        if not self._handler:
            return

        sub = self._rooms.get(room_id)
        if not sub:
            logger.warning("Received message for unknown room_id=%s", room_id)
            return

        # --- Filter (room-level, evaluated once) ---
        result: FilterResult = filter_rc_message(
            doc=doc,
            config=self._config,
            room_type=sub.room.type,
            last_processed_ts=sub.last_processed_ts,
        )
        if not result.accepted:
            logger.debug(
                "Message filtered: %s (sender=%s)", result.reason, result.sender
            )
            return

        logger.info(
            "Filter passed for message from %s in room '%s' — dispatching: %s",
            result.sender,
            sub.room.name,
            doc.get("msg", "")[:80],
        )

        # --- Preflight capacity check (two-phase inbound acceptance) ---
        # Short-circuit BEFORE expensive normalization + attachment download
        # when the core pipeline cannot accept the message anyway.  This avoids
        # wasted network, disk, and CPU under overload.
        #
        # Note: there is a TOCTOU gap — capacity may change between this check
        # and the later enqueue().  This is handled correctly: enqueue() returns
        # False and the watermark is not advanced.  The preflight is a best-effort
        # optimization, not a hard guarantee.
        if self._capacity_check and not self._capacity_check(room_id):
            logger.warning(
                "Preflight rejected for message from %s in room '%s' — "
                "all processor queues full, skipping normalize + download",
                result.sender,
                sub.room.name,
            )
            # Best-effort notification so the user knows their message was dropped.
            try:
                await self._handler_send_busy(room_id, doc)
            except Exception as exc:
                logger.debug(
                    "Best-effort busy notification failed for room '%s': %s",
                    room_id, exc
                )
            return  # watermark NOT advanced — message can be re-delivered

        # --- Normalize (once per message) ---
        # Attachment files are downloaded to a connector-global cache directory
        # namespaced by connector name and room ID.  All processors that subscribe
        # to this room reference the same local file paths — no per-watcher copies.
        # Fan-out to multiple processors is the SessionManager's responsibility;
        # the connector always calls the handler exactly once per accepted message.
        # Sanitize room_id before using it as a path component — room IDs are
        # server-controlled values and may contain path-traversal characters.
        # The downstream path-traversal check in normalize.py provides a second
        # layer of defense, but early sanitization is cleaner.
        safe_room_id = re.sub(r"[^\w.\-]", "_", room_id)
        cache_dir = self._attachments_cache_base / safe_room_id
        try:
            msg: IncomingMessage = await normalize_rc_message(
                doc=doc,
                room=sub.room,
                sender_username=result.sender,
                msg_ts=result.msg_ts,
                config=self._config,
                rest=self._rest,
                cache_dir=cache_dir,
            )
        except Exception as e:
            logger.error("Failed to normalize message: %s", e)
            return

        # --- Apply thread + permission-thread policy (extracted to policy.py) ---
        apply_thread_policy(msg, self._config)

        # --- Hand off to core (SessionManager._dispatch fans out to all processors) ---
        try:
            accepted = await self._handler(msg)
        except Exception as e:
            logger.error("Handler error for message from %s: %s", result.sender, e)
            return

        if not accepted:
            logger.warning(
                "Message from %s was dropped (queue full)",
                result.sender,
            )
            return

        # --- Advance dedup watermark AFTER confirmed acceptance ---
        # Update the watermark only once the handler has confirmed the message
        # was accepted (enqueued).  Advancing it before the handler call would
        # silently lose messages that are dropped due to queue-full conditions:
        # the RC replay mechanism skips messages whose ts <= last_processed_ts,
        # so a message dropped before it reaches a processor would never be
        # re-delivered on reconnect.
        #
        # Reconnect-duplicate risk: the window between handler returning True
        # and this assignment is a single Python statement — effectively zero.
        # This is a much smaller race than waiting for the entire handler
        # duration, so the previous "advance before handler" behaviour did not
        # meaningfully reduce reconnect duplication in practice.
        sub.last_processed_ts = result.msg_ts

    async def _handler_send_busy(self, room_id: str, doc: dict) -> None:
        """Best-effort 'server busy' notification to the user when preflight rejects."""
        thread_id = doc.get("tmid") or None
        await self._rest.post_message(
            room_id,
            "⚠️ Server busy — your message was dropped. Please retry.",
            tmid=thread_id,
        )
