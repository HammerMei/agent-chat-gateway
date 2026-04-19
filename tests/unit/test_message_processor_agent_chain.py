"""Integration tests for MessageProcessor agent-chain path.

Covers the terminated=True → on_agent_chain_drop flow that was previously
untested — the most critical path connecting AgentTurnRunner's return value
to the connector's counter-reset callback.
"""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from gateway.agents import AgentBackend
from gateway.agents.response import AgentResponse
from gateway.config import AgentConfig
from gateway.core.config import CoreConfig
from gateway.core.connector import IncomingMessage, Room, User, UserRole
from gateway.core.message_processor import MessageProcessor


# ── Helpers ────────────────────────────────────────────────────────────────────


class _MockAgent(AgentBackend):
    async def create_session(self, *a, **kw):
        return "ses_001"

    async def send(self, *a, **kw):
        return AgentResponse(text="ok")


def _make_processor() -> tuple[MessageProcessor, MagicMock]:
    connector = MagicMock()
    connector.notify_online = AsyncMock()
    connector.notify_offline = AsyncMock()
    connector.send_text = AsyncMock()
    connector.notify_typing = AsyncMock()
    connector.notify_agent_event = AsyncMock()
    connector.format_prompt_prefix = MagicMock(return_value="")
    connector.on_agent_chain_drop = MagicMock()

    config = CoreConfig(
        agents={"default": AgentConfig(timeout=10)},
        default_agent="default",
    )
    room = Room(id="room_1", name="test-room", type="channel")
    processor = MessageProcessor(
        session_id="ses_001",
        room=room,
        working_directory="/tmp",
        watcher_id="watcher_001",
        connector=connector,
        agent=_MockAgent(),
        config=config,
    )
    return processor, connector


def _make_agent_chain_msg(
    sender: str = "agentA",
    room_id: str = "room_1",
    thread_id: str | None = None,
    turn: int = 1,
    max_turns: int = 5,
) -> IncomingMessage:
    room = Room(id=room_id, name="test-room", type="channel")
    msg = IncomingMessage(
        id="msg_001",
        timestamp="1000",
        room=room,
        sender=User(id=sender, username=sender, display_name=sender),
        role=UserRole.GUEST,
        text="hello from agent",
        thread_id=thread_id,
    )
    msg.extra_context["is_agent_chain"] = True
    msg.extra_context["agent_chain_turn"] = turn
    msg.extra_context["agent_chain_max_turns"] = max_turns
    return msg


# ── Tests ─────────────────────────────────────────────────────────────────────


class TestMessageProcessorAgentChain(unittest.IsolatedAsyncioTestCase):
    async def test_terminated_turn_does_not_call_on_agent_chain_drop(self):
        """Self-termination does NOT reset the counter (no on_agent_chain_drop call).

        Counter stays at its current value so the budget is not renewed, preventing
        the loop from restarting on the next queued message.
        """
        processor, connector = _make_processor()
        msg = _make_agent_chain_msg(sender="agentA", room_id="room_1", thread_id=None)

        with patch.object(processor._turn_runner, "run_turn", new=AsyncMock(return_value=True)):
            await processor._process(msg)

        connector.on_agent_chain_drop.assert_not_called()

    async def test_terminated_turn_in_thread_does_not_call_on_agent_chain_drop(self):
        """Self-termination in a thread also does NOT call on_agent_chain_drop."""
        processor, connector = _make_processor()
        msg = _make_agent_chain_msg(sender="agentB", room_id="room_2", thread_id="t99")

        with patch.object(processor._turn_runner, "run_turn", new=AsyncMock(return_value=True)):
            await processor._process(msg)

        connector.on_agent_chain_drop.assert_not_called()

    async def test_normal_turn_does_not_call_on_agent_chain_drop(self):
        """When run_turn returns False (normal delivery), on_agent_chain_drop is NOT called."""
        processor, connector = _make_processor()
        msg = _make_agent_chain_msg(sender="agentA")

        with patch.object(processor._turn_runner, "run_turn", new=AsyncMock(return_value=False)):
            await processor._process(msg)

        connector.on_agent_chain_drop.assert_not_called()

    async def test_non_agent_chain_turn_does_not_call_on_agent_chain_drop(self):
        """Non-agent-chain messages (is_agent_chain=False) never call on_agent_chain_drop."""
        processor, connector = _make_processor()
        room = Room(id="room_1", name="test-room", type="channel")
        msg = IncomingMessage(
            id="msg_002",
            timestamp="2000",
            room=room,
            sender=User(id="human1", username="human1", display_name="Human"),
            role=UserRole.OWNER,
            text="hello from human",
        )
        # No is_agent_chain in extra_context → defaults to False

        with patch.object(processor._turn_runner, "run_turn", new=AsyncMock(return_value=False)):
            await processor._process(msg)

        connector.on_agent_chain_drop.assert_not_called()

    async def test_agent_chain_context_injected_into_run_turn_call(self):
        """When is_agent_chain=True, run_turn is called with non-empty agent_chain_context."""
        processor, connector = _make_processor()
        msg = _make_agent_chain_msg(turn=2, max_turns=5)

        captured: dict = {}

        async def _capture_run_turn(**kw):
            captured.update(kw)
            return False

        with patch.object(processor._turn_runner, "run_turn", new=_capture_run_turn):
            await processor._process(msg)

        self.assertTrue(captured.get("is_agent_chain"))
        ctx = captured.get("agent_chain_context", "")
        self.assertIn("[Agent chain: turn 2/5]", ctx)

    async def test_anonymous_message_rejected_before_agent_chain_path(self):
        """ANONYMOUS role is rejected at the top gate — on_agent_chain_drop never called."""
        processor, connector = _make_processor()
        room = Room(id="room_1", name="test-room", type="channel")
        msg = IncomingMessage(
            id="msg_anon",
            timestamp="3000",
            room=room,
            sender=User(id="anon", username="anon", display_name="Anon"),
            role=UserRole.ANONYMOUS,
            text="intruder",
        )
        msg.extra_context["is_agent_chain"] = True
        msg.extra_context["agent_chain_turn"] = 1
        msg.extra_context["agent_chain_max_turns"] = 5

        # run_turn should never be reached
        with patch.object(processor._turn_runner, "run_turn", new=AsyncMock(return_value=True)) as mock_run:
            await processor._process(msg)
            mock_run.assert_not_called()

        connector.on_agent_chain_drop.assert_not_called()


if __name__ == "__main__":
    unittest.main()
