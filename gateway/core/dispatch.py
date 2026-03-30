"""MessageDispatcher: routes inbound messages to processors by room ID.

Owns the room→processor dispatch index and permission command interception.
Extracted from SessionManager to keep dispatch logic focused and testable.
"""

from __future__ import annotations

import logging
import re

from ..agents.response import AgentResponse
from .connector import Connector, IncomingMessage, UserRole
from .message_processor import MessageProcessor
from .permission import PermissionRegistry

logger = logging.getLogger("agent-chat-gateway.core.dispatch")

_PERMISSION_CMD_RE = re.compile(
    r"^(approve|deny)\s+([a-z0-9]+)$", re.IGNORECASE
)


class MessageDispatcher:
    """Routes IncomingMessages to MessageProcessors by room ID.

    Permission commands (approve/deny) from owners are intercepted before
    fan-out so they are handled exactly once per room, even when multiple
    processors subscribe to the same room.

    Usage::

        dispatcher = MessageDispatcher(connector, permission_registry)
        connector.register_handler(dispatcher.dispatch)

        # When watchers start/stop:
        dispatcher.add_processor("room-123", processor)
        dispatcher.remove_processor("room-123", processor)
    """

    def __init__(
        self,
        connector: Connector,
        permission_registry: PermissionRegistry | None = None,
    ) -> None:
        self._connector = connector
        self._permission_registry = permission_registry
        self._room_processors: dict[str, list[MessageProcessor]] = {}

    # ── Dispatch ──────────────────────────────────────────────────────────────

    async def dispatch(self, msg: IncomingMessage) -> bool:
        """Route an IncomingMessage to all matching processors by room ID.

        Permission commands (approve/deny) from owners are intercepted here —
        BEFORE fan-out — so they are handled exactly once per room, even when
        multiple processors subscribe to the same room.

        Returns:
            True if all processors accepted the message (or it was a permission
            command handled inline).  False if any processor dropped the message
            due to a full queue — the connector must NOT advance the dedup
            watermark so the message can be re-delivered on reconnect.
        """
        # --- Intercept permission commands at room level (before fan-out) ---
        if self._permission_registry and msg.role == UserRole.OWNER:
            m = _PERMISSION_CMD_RE.match(msg.text.strip())
            if m:
                await self._handle_permission_command(msg, m)
                return True  # permission commands are always accepted

        processors = self._room_processors.get(msg.room.id, [])
        if processors:
            results = [await processor.enqueue(msg) for processor in processors]
            # Advance the room-level dedup watermark if ANY watcher accepted the
            # message.  Using any() instead of all() is correct because:
            #   - The room watermark is a DDP transport cursor, not a per-watcher
            #     processing guarantee.
            #   - A watcher that dropped the message (queue full) has already
            #     notified the user and the drop is an explicit overload decision.
            #   - Using all() would penalize healthy watchers: one slow watcher's
            #     full queue would prevent the watermark from advancing, causing
            #     duplicate delivery to ALL watchers (including those that already
            #     processed the message) on the next reconnect.
            return any(results)
        else:
            logger.warning("No processor found for room_id=%s", msg.room.id)
            return False

    def has_capacity(self, room_id: str) -> bool:
        """Check whether any processor for this room can accept a new message.

        Returns True if at least one processor for the room is in ``running``
        state and has space in its queue.  Returns False if no processors exist,
        all are draining/stopped, or all queues are full.

        Used by connectors to short-circuit expensive normalization/download
        work before the core pipeline commits to accept the message.
        """
        processors = self._room_processors.get(room_id, [])
        return any(p.is_accepting for p in processors)

    # ── Index management ──────────────────────────────────────────────────────

    def add_processor(self, room_id: str, processor: MessageProcessor) -> None:
        """Register a processor in the room dispatch index."""
        self._room_processors.setdefault(room_id, []).append(processor)

    def remove_processor(self, room_id: str, processor: MessageProcessor) -> None:
        """Remove a processor from the room dispatch index."""
        if room_id in self._room_processors:
            self._room_processors[room_id] = [
                p for p in self._room_processors[room_id] if p is not processor
            ]
            if not self._room_processors[room_id]:
                del self._room_processors[room_id]

    # ── Permission command handling ───────────────────────────────────────────

    async def _handle_permission_command(
        self, msg: IncomingMessage, match: re.Match
    ) -> None:
        """Resolve an approve/deny permission command (room-level, pre-fan-out)."""
        action = match.group(1).lower()
        req_id = match.group(2).lower()

        if len(req_id) != 4:
            reply = (
                f"⚠️ Invalid ID `{req_id}` — "
                f"expected 4 characters (e.g. `{action} a3k9`)."
            )
        else:
            approved = action == "approve"
            resolved = self._permission_registry.resolve(req_id, approved)  # type: ignore[union-attr]
            if resolved:
                icon = "✅" if approved else "❌"
                verb = "approved" if approved else "denied"
                reply = f"{icon} Permission `{req_id}` {verb}."
            else:
                reply = f"⚠️ No pending permission request with ID `{req_id}`."

        await self._connector.send_text(
            msg.room.id, AgentResponse(text=reply), thread_id=msg.thread_id
        )
