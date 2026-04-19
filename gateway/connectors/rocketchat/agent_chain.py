"""Agent-chain turn tracking for controlled agent-to-agent communication.

When two ACG bots share a room, uncontrolled cross-agent messaging creates
infinite loops.  This module provides:

  - AGENT_CHAIN_TERMINATION_TOKEN: the sentinel the LLM outputs to self-terminate
  - TurnStore: per-sender turn budget tracker with reset-on-drop and TTL GC
  - build_agent_chain_context: re-exported from core for convenience
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from ...core.agent_chain import (  # noqa: F401 — re-export for connector consumers
    AGENT_CHAIN_TERMINATION_TOKEN,
    build_agent_chain_context,
)

logger = logging.getLogger("agent-chat-gateway.connectors.rocketchat.agent_chain")


@dataclass
class _TurnContext:
    turns: int = 0
    last_updated: float = field(default_factory=time.monotonic)


class TurnStore:
    """Thread-safe (asyncio single-threaded) per-sender turn budget tracker.

    Key: (room_id, thread_id, sender_username)
    - Each sender has an independent counter against the current bot.
    - On any drop (force or LLM termination): immediately reset that sender's counter.
    - On human message: reset all counters for (room_id, thread_id).
    - TTL GC: entries older than ttl_seconds are purged on each check.
    """

    def __init__(self, ttl_seconds: float = 3600.0) -> None:
        self._ttl = ttl_seconds
        self._store: dict[tuple[str, str | None, str], _TurnContext] = {}

    # Key helpers
    @staticmethod
    def _key(room_id: str, thread_id: str | None, sender: str) -> tuple[str, str | None, str]:
        return (room_id, thread_id, sender)

    def check_and_increment(
        self,
        room_id: str,
        thread_id: str | None,
        sender: str,
        max_turns: int,
    ) -> tuple[bool, int]:
        """Check turn budget and increment if allowed.

        Returns:
            (allowed, current_turn_after_increment)
            allowed=False means the message should be dropped.
        """
        self._gc()
        key = self._key(room_id, thread_id, sender)
        ctx = self._store.get(key)
        if ctx is None:
            ctx = _TurnContext()
            self._store[key] = ctx

        if ctx.turns >= max_turns:
            return False, ctx.turns

        ctx.turns += 1
        ctx.last_updated = time.monotonic()
        return True, ctx.turns

    def current_turns(self, room_id: str, thread_id: str | None, sender: str) -> int:
        """Return current turn count for a sender (0 if not tracked)."""
        key = self._key(room_id, thread_id, sender)
        ctx = self._store.get(key)
        return ctx.turns if ctx else 0

    def reset_sender(self, room_id: str, thread_id: str | None, sender: str) -> None:
        """Reset turn counter for a specific sender (call on any drop)."""
        key = self._key(room_id, thread_id, sender)
        self._store.pop(key, None)
        logger.debug("Agent chain counter reset for sender=%s thread=%s", sender, thread_id)

    def reset_all(self, room_id: str, thread_id: str | None) -> None:
        """Reset all agent counters for a room/thread context (call on human message)."""
        keys_to_remove = [
            k for k in self._store if k[0] == room_id and k[1] == thread_id
        ]
        for k in keys_to_remove:
            del self._store[k]
        if keys_to_remove:
            logger.debug(
                "Agent chain counters reset for room=%s thread=%s (%d entries)",
                room_id, thread_id, len(keys_to_remove),
            )

    def _gc(self) -> None:
        """Remove entries older than TTL."""
        now = time.monotonic()
        expired = [k for k, v in self._store.items() if now - v.last_updated > self._ttl]
        for k in expired:
            del self._store[k]
