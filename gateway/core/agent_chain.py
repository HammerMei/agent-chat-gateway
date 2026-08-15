"""Agent-chain primitives shared between the core and connector layers.

Both ``gateway.core.agent_turn_runner`` and connector packages (e.g.
``gateway.connectors.rocketchat``, ``gateway.connectors.mattermost``) import
from here so that the token, prompt-suffix builder, and turn-budget tracker
are defined exactly once — no connector needs to import from another
connector to get platform-agnostic loop-protection logic.

``TurnStore`` was originally implemented inside the Rocket.Chat connector but
is keyed purely on ``(room_id, thread_id, sender)`` strings with no RC-specific
behavior, so it lives here now.  ``gateway.connectors.rocketchat.agent_chain``
re-exports it for backward compatibility.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

logger = logging.getLogger("agent-chat-gateway.core.agent_chain")

# Sentinel the LLM outputs to self-terminate an agent chain turn.
# ACG detects this via exact match (response.text.strip() == TOKEN).
AGENT_CHAIN_TERMINATION_TOKEN = "<end-of-agent-chain>"


@dataclass
class AgentChainConfig:
    """Configuration for controlled agent-to-agent communication.

    Platform-agnostic — shared by every connector config that supports
    agent-chain (e.g. RocketChatConfig, MattermostConfig).
    """
    agent_usernames: list[str] = field(default_factory=list)
    max_turns: int = 5
    ttl_seconds: float = 3600.0


def build_agent_chain_context(turn: int, max_turns: int) -> str:
    """Build the toll-call prompt suffix injected when processing an agent-chain message.

    turn:      1-based current turn number (already incremented).
    max_turns: configured budget ceiling.
    """
    lines = [
        f"\n---\n[Agent chain: turn {turn}/{max_turns}]"
    ]
    if turn == max_turns - 1:
        lines.append(
            "\u26a0\ufe0f  Your next response will be your last turn in this agent chain."
        )
    elif turn >= max_turns:
        lines.append(
            "\u26a0\ufe0f  This is your final turn in this agent chain. "
            "Please wrap up gracefully.\n"
            "If the task is not yet complete, you may use the scheduler tool "
            "to schedule a follow-up message and continue with a fresh turn budget."
        )
    if turn < max_turns:
        lines.append(
            f"If this conversation is repeating without making progress (a loop), "
            f"or if you have nothing meaningful to add, respond with ONLY: "
            f"{AGENT_CHAIN_TERMINATION_TOKEN}"
        )
    return "\n".join(lines)


@dataclass
class _TurnContext:
    turns: int = 0
    last_updated: float = field(default_factory=time.monotonic)
    # Incremented by every reset. A turn number cannot identify the increment that
    # produced it, because a reset starts the next count at one again — so a delivery
    # still in flight would match a turn that belongs to someone else. See `release_turn`.
    generation: int = 0


class TurnStore:
    """Thread-safe (asyncio single-threaded) per-sender turn budget tracker.

    Key: (room_id, thread_id, sender_username)
    - Each sender has an independent counter against the current bot.
    - On force-drop (budget exhausted): counter stays at max, sender locked until
      human message or TTL expiry.
    - On self-termination (LLM gracefully exits): counter stays, chain dies
      naturally (no reply posted → no trigger for the other agent to reply).
    - On human message: reset_all clears all counters for the room/thread.
    - TTL GC: entries older than ttl_seconds are purged lazily on each check,
      giving any sender a fresh full budget after a long idle period.

    Future consideration — two-TTL design:
        Force-drop and self-termination may warrant different TTLs.  A force-drop
        is like a dropped call: the other agent likely wants to keep talking and
        will retry quickly, so a shorter TTL is appropriate.  Self-termination is a
        natural hang-up: the room should stay quiet, so a longer TTL gives more
        cooldown before fresh budget is granted.  For now a single ttl_seconds is
        used for simplicity; split into force_drop_ttl / self_terminate_ttl if
        real-world tuning shows the single value is too coarse.
    """

    def __init__(self, ttl_seconds: float = 3600.0) -> None:
        self._ttl = ttl_seconds
        self._store: dict[tuple[str, str | None, str], _TurnContext] = {}

    # Key helpers
    @staticmethod
    def _key(room_id: str, thread_id: str | None, sender: str) -> tuple[str, str | None, str]:
        return (room_id, thread_id, sender)

    def generation(self, room_id: str, thread_id: str | None, sender: str) -> int:
        """How many times this context has been reset, and therefore which count is live.

        A turn number alone cannot identify the increment that produced it: a reset drops
        the counter and the next agent message starts again at one, so an in-flight
        delivery holding "I took turn 1" would match a turn 1 that belongs to somebody
        else. This distinguishes them, and it survives the reset because a reset zeroes
        the context rather than removing it.
        """
        ctx = self._store.get(self._key(room_id, thread_id, sender))
        return ctx.generation if ctx else 0

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

    def release_turn(
        self,
        room_id: str,
        thread_id: str | None,
        sender: str,
        taken_turn: int,
        generation: int,
    ) -> int:
        """Give back a turn taken by `check_and_increment` for a message not delivered.

        The budget counts *turns an agent took*, and a message the gateway hands back for
        a later retry has not taken one — but the increment already happened, because the
        filter runs before the capacity preflight and before the handler. Every retry of
        the same document would otherwise spend another turn, and the budget would run out
        on a message that was never dispatched: the filter then rejects it as complete,
        the replay reports success, and the window closes over a message nobody saw.

        `taken_turn` is the number `check_and_increment` returned to *this* delivery, and
        the release happens only while the counter still stands there. Deliveries overlap
        by design — replay and live traffic run concurrently — and in between, a human
        message can reset the context and another agent message can start a fresh count.
        An unconditional decrement would then take a turn away from a delivery that
        succeeded, and the chain could run past `max_turns`: a burn incident rather than a
        bookkeeping slip, which is why the check is on the value rather than on
        `turns > 0`.

        When the counter has moved on, the turn is simply not returned. That leaves the
        budget marginally stricter than it needs to be, which is the safe direction: too
        few turns costs one retryable message its budget slot, too many costs an unbounded
        agent conversation.
        """
        key = self._key(room_id, thread_id, sender)
        ctx = self._store.get(key)
        if ctx is None:
            # Garbage-collected since this delivery took its turn; nothing of it remains.
            return 0
        if ctx.generation != generation or ctx.turns != taken_turn:
            # A reset started a fresh count (which begins at one again, so the turn number
            # alone cannot tell them apart), or another delivery has taken a turn since.
            return ctx.turns
        ctx.turns -= 1
        ctx.last_updated = time.monotonic()
        return ctx.turns

    def current_turns(self, room_id: str, thread_id: str | None, sender: str) -> int:
        """Return current turn count for a sender (0 if not tracked)."""
        key = self._key(room_id, thread_id, sender)
        ctx = self._store.get(key)
        return ctx.turns if ctx else 0

    def reset_sender(self, room_id: str, thread_id: str | None, sender: str) -> None:
        """Reset turn counter for a specific sender (call on any drop).

        Zeroed rather than dropped, so the generation survives: a delivery still in flight
        has to be able to tell that the count it took its turn from is gone. `_gc` still
        reclaims the entry once it goes quiet, by which time no delivery can be holding a
        turn from it.
        """
        key = self._key(room_id, thread_id, sender)
        ctx = self._store.get(key)
        if ctx is not None:
            ctx.turns = 0
            ctx.generation += 1
            ctx.last_updated = time.monotonic()
        logger.debug("Agent chain counter reset for sender=%s thread=%s", sender, thread_id)

    def reset_all(self, room_id: str, thread_id: str | None) -> None:
        """Reset all agent counters for a room/thread context (call on human message)."""
        # Zeroed rather than dropped, for the reason `reset_sender` gives: an in-flight
        # delivery must be able to tell that the count it took its turn from is gone.
        keys_to_remove = [
            k for k in self._store if k[0] == room_id and k[1] == thread_id
        ]
        for k in keys_to_remove:
            ctx = self._store[k]
            ctx = self._store[k]
            ctx.turns = 0
            ctx.generation += 1
            ctx.last_updated = time.monotonic()
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
