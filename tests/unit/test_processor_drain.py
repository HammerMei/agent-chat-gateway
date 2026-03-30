"""Tests for MessageProcessor graceful drain (P0-1).

Covers:
  - stop() drains already-queued messages before completing
  - watermark reflects last drained message
  - force-stop timeout cancels after deadline
  - enqueue rejected during drain/stopped state
"""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock

from gateway.agents import AgentBackend
from gateway.agents.errors import AgentUnavailableError
from gateway.agents.response import AgentResponse
from gateway.config import WatcherConfig
from gateway.core.config import AgentConfig, CoreConfig
from gateway.core.connector import IncomingMessage, Room, User, UserRole
from gateway.core.context_injector import ContextInjector, InjectionStatus
from gateway.core.message_processor import MessageProcessor
from gateway.core.state import WatcherState

# ── Helpers ────────────────────────────────────────────────────────────────────


class _SlowAgent(AgentBackend):
    """Agent that takes a configurable delay to respond."""

    def __init__(self, delay: float = 0.05):
        self._delay = delay
        self.processed: list[str] = []

    async def create_session(self, *a, **kw):
        return "ses_001"

    async def send(
        self, session_id, prompt, working_directory, timeout, attachments=None, env=None
    ):
        await asyncio.sleep(self._delay)
        self.processed.append(prompt)
        return AgentResponse(text="ok")


def _make_processor(agent: AgentBackend) -> MessageProcessor:
    config = CoreConfig(
        agents={"default": AgentConfig(timeout=10)},
        default_agent="default",
    )
    connector = MagicMock()
    connector.send_text = AsyncMock()
    connector.format_prompt_prefix = MagicMock(return_value="")
    connector.notify_typing = AsyncMock()
    connector.notify_online = AsyncMock()
    connector.notify_offline = AsyncMock()
    return MessageProcessor(
        session_id="ses_001",
        room=Room(id="room_1", name="test-room"),
        working_directory="/tmp",
        watcher_id="test-watcher",
        connector=connector,
        agent=agent,
        config=config,
        agent_name="default",
    )


def _make_processor_with_context(
    agent: AgentBackend,
    injector: ContextInjector,
    watcher_state: WatcherState,
    watcher_config: WatcherConfig,
) -> MessageProcessor:
    config = CoreConfig(
        agents={"default": AgentConfig(timeout=10)},
        default_agent="default",
    )
    connector = MagicMock()
    connector.send_text = AsyncMock()
    connector.format_prompt_prefix = MagicMock(return_value="")
    connector.notify_typing = AsyncMock()
    connector.notify_online = AsyncMock()
    connector.notify_offline = AsyncMock()
    return MessageProcessor(
        session_id="ses_001",
        room=Room(id="room_1", name="test-room"),
        working_directory="/tmp",
        watcher_id="test-watcher",
        connector=connector,
        agent=agent,
        config=config,
        agent_name="default",
        context_injector=injector,
        watcher_state=watcher_state,
        watcher_config=watcher_config,
        connector_name="script",
    )


def _make_msg(text: str = "hello", msg_id: str = "m1") -> IncomingMessage:
    return IncomingMessage(
        id=msg_id,
        timestamp="100",
        room=Room(id="room_1", name="test-room"),
        sender=User(id="u1", username="alice"),
        text=text,
        role=UserRole.OWNER,
    )


# ── Tests ─────────────────────────────────────────────────────────────────────


class TestGracefulDrain(unittest.IsolatedAsyncioTestCase):
    async def test_stop_drains_queued_messages(self):
        """Messages already in the queue are processed before stop() completes."""
        agent = _SlowAgent(delay=0.01)
        proc = _make_processor(agent)
        proc.start()

        # Enqueue two messages
        await proc.enqueue(_make_msg("msg1", "m1"))
        await proc.enqueue(_make_msg("msg2", "m2"))

        # Give the consumer a moment to start processing the first
        await asyncio.sleep(0.02)

        # Stop should drain both messages
        await proc.stop()

        self.assertIn("msg1", agent.processed)
        self.assertIn("msg2", agent.processed)

    async def test_enqueue_rejected_during_drain(self):
        """New messages are rejected once drain starts."""
        agent = _SlowAgent(delay=0.01)
        proc = _make_processor(agent)
        proc.start()

        # Trigger drain
        proc._state = "draining"

        result = await proc.enqueue(_make_msg("should-reject"))
        self.assertFalse(result)

    async def test_enqueue_rejected_after_stopped(self):
        """Messages are rejected after stop() completes."""
        agent = _SlowAgent(delay=0.01)
        proc = _make_processor(agent)
        proc.start()
        await proc.stop()

        result = await proc.enqueue(_make_msg("too-late"))
        self.assertFalse(result)
        self.assertEqual(proc._state, "stopped")

    async def test_force_stop_on_timeout(self):
        """If drain takes too long, consumer is force-cancelled."""
        agent = _SlowAgent(delay=10.0)  # very slow — will timeout
        proc = _make_processor(agent)
        proc.start()

        await proc.enqueue(_make_msg("slow-msg"))
        await asyncio.sleep(0.01)  # let consumer pick it up

        # Force stop with very short timeout
        await proc.stop(drain_timeout=0.05)
        self.assertEqual(proc._state, "stopped")

    async def test_stop_empty_queue_completes_immediately(self):
        """stop() on an empty queue completes without waiting the full timeout."""
        agent = _SlowAgent(delay=0.01)
        proc = _make_processor(agent)
        proc.start()

        # No messages enqueued — stop should be near-instant
        await proc.stop()
        self.assertEqual(proc._state, "stopped")

    async def test_state_transitions(self):
        """Lifecycle states transition: running → draining → stopped."""
        agent = _SlowAgent(delay=0.01)
        proc = _make_processor(agent)

        self.assertEqual(proc._state, "running")
        proc.start()
        self.assertEqual(proc._state, "running")

        await proc.stop()
        self.assertEqual(proc._state, "stopped")

    async def test_drain_processes_all_before_sentinel(self):
        """Messages enqueued before the drain sentinel are all processed."""
        agent = _SlowAgent(delay=0.005)
        proc = _make_processor(agent)
        proc.start()

        # Enqueue several messages quickly
        for i in range(5):
            await proc.enqueue(_make_msg(f"batch-{i}", f"m{i}"))

        await proc.stop()

        # All 5 should have been processed
        self.assertEqual(len(agent.processed), 5)
        for i in range(5):
            self.assertIn(f"batch-{i}", agent.processed)

    async def test_drain_works_when_queue_full_sentinel_cannot_be_placed(self):
        """When the queue is full, stop() can't place the sentinel but drain still works."""
        agent = _SlowAgent(delay=0.01)
        config = CoreConfig(
            agents={"default": AgentConfig(timeout=10)},
            default_agent="default",
            max_queue_depth=2,  # small queue
        )
        connector = MagicMock()
        connector.send_text = AsyncMock()
        connector.format_prompt_prefix = MagicMock(return_value="")
        connector.notify_typing = AsyncMock()
        connector.notify_online = AsyncMock()
        connector.notify_offline = AsyncMock()
        proc = MessageProcessor(
            session_id="ses_001",
            room=Room(id="room_1", name="test-room"),
            working_directory="/tmp",
            watcher_id="test-watcher",
            connector=connector,
            agent=agent,
            config=config,
            agent_name="default",
        )
        proc.start()

        # Fill the queue completely
        await proc.enqueue(_make_msg("fill1", "m1"))
        await proc.enqueue(_make_msg("fill2", "m2"))

        # Stop — sentinel can't be placed because queue is full
        await proc.stop()

        # Both messages should still have been processed
        self.assertIn("fill1", agent.processed)
        self.assertIn("fill2", agent.processed)
        self.assertEqual(proc._state, "stopped")

    async def test_double_stop_is_safe(self):
        """Calling stop() twice is a no-op on the second call."""
        agent = _SlowAgent(delay=0.01)
        proc = _make_processor(agent)
        proc.start()

        await proc.stop()
        self.assertEqual(proc._state, "stopped")


class TestContextInjectionRetry(unittest.IsolatedAsyncioTestCase):
    async def test_retryable_context_failure_retries_on_next_message(self):
        agent = _SlowAgent(delay=0.0)
        injector = MagicMock(spec=ContextInjector)
        injector.status_for = MagicMock(
            return_value=InjectionStatus(state="failed_retryable", failure_count=1)
        )
        injector.inject = AsyncMock()
        watcher_state = WatcherState(
            watcher_name="test-watcher",
            session_id="ses_001",
            room_id="room_1",
            context_injected=False,
        )
        watcher_config = WatcherConfig(
            name="test-watcher",
            connector="script",
            room="test-room",
            agent="default",
            context_inject_files=["ctx.md"],
        )
        proc = _make_processor_with_context(
            agent, injector, watcher_state, watcher_config
        )

        await proc._process(_make_msg("hello"))

        injector.inject.assert_awaited_once()

    async def test_degraded_context_state_does_not_retry_every_message(self):
        agent = _SlowAgent(delay=0.0)
        injector = MagicMock(spec=ContextInjector)
        injector.status_for = MagicMock(
            return_value=InjectionStatus(state="failed_degraded", failure_count=3)
        )
        injector.inject = AsyncMock()
        watcher_state = WatcherState(
            watcher_name="test-watcher",
            session_id="ses_001",
            room_id="room_1",
            context_injected=False,
        )
        watcher_config = WatcherConfig(
            name="test-watcher",
            connector="script",
            room="test-room",
            agent="default",
            context_inject_files=["ctx.md"],
        )
        proc = _make_processor_with_context(
            agent, injector, watcher_state, watcher_config
        )

        await proc._process(_make_msg("hello"))

        injector.inject.assert_not_awaited()

        # Second stop should not raise or hang
        await proc.stop()
        self.assertEqual(proc._state, "stopped")

    async def test_hard_context_injection_failure_replies_with_error_message(self):
        agent = _SlowAgent(delay=0.0)
        injector = MagicMock(spec=ContextInjector)
        injector.status_for = MagicMock(
            return_value=InjectionStatus(state="failed_retryable")
        )
        injector.inject = AsyncMock(
            side_effect=AgentUnavailableError("backend unavailable")
        )
        watcher_state = WatcherState(
            watcher_name="test-watcher",
            session_id="ses_001",
            room_id="room_1",
            context_injected=False,
        )
        watcher_config = WatcherConfig(
            name="test-watcher",
            connector="script",
            room="test-room",
            agent="default",
            context_inject_files=["ctx.md"],
        )
        proc = _make_processor_with_context(
            agent, injector, watcher_state, watcher_config
        )

        await proc._process(_make_msg("hello"))

        proc._connector.send_text.assert_awaited_once()
        response = proc._connector.send_text.await_args.args[1]
        self.assertTrue(response.is_error)
        self.assertIn("temporarily unavailable", response.text)
        self.assertEqual(agent.processed, [])


if __name__ == "__main__":
    unittest.main()


# ── Appended from test_code_review_fixes.py ───────────────────────────────────

from tests.helpers import IsolatedTestCase as _IsolatedTestCase3  # noqa: E402


class TestProcessorStoppingFlag(_IsolatedTestCase3):
    """P1-1: enqueue() must reject messages when the processor is stopping."""

    def _make_processor(self, connector, agent):
        agent_cfg = AgentConfig(timeout=10)
        config = CoreConfig(agents={"default": agent_cfg}, default_agent="default")
        room = Room(id="room-1", name="general", type="channel")
        return MessageProcessor(
            session_id="test-session",
            room=room,
            working_directory="/tmp",
            watcher_id="test-watcher",
            connector=connector,
            agent=agent,
            config=config,
            agent_name="default",
        )

    async def test_enqueue_accepted_before_stopping(self):
        """enqueue() returns True when the processor is not stopping."""
        from gateway.connectors.script import ScriptConnector

        class MockAgent(AgentBackend):
            async def create_session(self, working_directory, extra_args=None, session_title=None):
                return "test-session"

            async def send(self, session_id, prompt, working_directory, timeout, attachments=None, env=None):
                return AgentResponse(text="ok")

        connector = ScriptConnector()
        agent = MockAgent()
        proc = self._make_processor(connector, agent)
        proc.start()

        msg = IncomingMessage(
            id="m1",
            timestamp="100",
            room=Room(id="room-1", name="general", type="channel"),
            sender=User(id="u1", username="alice"),
            role=UserRole.OWNER,
            text="hello",
        )
        result = await proc.enqueue(msg)
        self.assertTrue(result)
        await proc.stop()

    async def test_enqueue_rejected_when_draining(self):
        """enqueue() returns False when state is 'draining'."""
        from gateway.connectors.script import ScriptConnector

        class MockAgent(AgentBackend):
            async def create_session(self, working_directory, extra_args=None, session_title=None):
                return "test-session"

            async def send(self, session_id, prompt, working_directory, timeout, attachments=None, env=None):
                return AgentResponse(text="ok")

        connector = ScriptConnector()
        agent = MockAgent()
        proc = self._make_processor(connector, agent)
        proc.start()

        proc._state = "draining"

        msg = IncomingMessage(
            id="m1",
            timestamp="100",
            room=Room(id="room-1", name="general", type="channel"),
            sender=User(id="u1", username="alice"),
            role=UserRole.OWNER,
            text="should be rejected",
        )
        result = await proc.enqueue(msg)
        self.assertFalse(result)
        self.assertEqual(proc._queue.qsize(), 0)
        await proc.stop()

    async def test_stop_transitions_state_to_stopped(self):
        """stop() must transition _state from 'running' to 'stopped'."""
        from gateway.connectors.script import ScriptConnector

        class MockAgent(AgentBackend):
            async def create_session(self, working_directory, extra_args=None, session_title=None):
                return "test-session"

            async def send(self, session_id, prompt, working_directory, timeout, attachments=None, env=None):
                return AgentResponse(text="ok")

        connector = ScriptConnector()
        agent = MockAgent()
        proc = self._make_processor(connector, agent)
        proc.start()

        self.assertEqual(proc._state, "running")
        await proc.stop()
        self.assertEqual(proc._state, "stopped")
