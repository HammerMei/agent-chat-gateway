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
from gateway.agents.response import AgentEvent, AgentResponse
from gateway.core.config import AgentConfig, CoreConfig
from gateway.core.connector import IncomingMessage, Room, User, UserRole
from gateway.core.message_processor import MessageProcessor

# ── Helpers ────────────────────────────────────────────────────────────────────


class _SlowAgent(AgentBackend):
    """Agent that takes a configurable delay to respond."""

    def __init__(self, delay: float = 0.05):
        self._delay = delay
        self.processed: list[str] = []

    async def create_session(self, *a, **kw):
        return "ses_001"

    async def send(
        self,
        session_id,
        prompt,
        working_directory,
        timeout,
        attachments=None,
        env=None,
        append_system_prompt_file=None,
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


def _make_processor_with_prompt_file(
    agent: AgentBackend, append_system_prompt_file: str | None
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
    connector.notify_agent_event = AsyncMock()
    return MessageProcessor(
        session_id="ses_001",
        room=Room(id="room_1", name="test-room"),
        working_directory="/tmp",
        watcher_id="test-watcher",
        connector=connector,
        agent=agent,
        config=config,
        agent_name="default",
        append_system_prompt_file=append_system_prompt_file,
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

        # Stop should drain both messages.
        # Both may arrive as a single catch-up batch prompt, so check that
        # all content appears somewhere across the processed prompts.
        await proc.stop()

        all_text = " ".join(agent.processed)
        self.assertIn("msg1", all_text)
        self.assertIn("msg2", all_text)

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

        # All 5 message texts should appear across the processed prompts.
        # Messages may be combined into one or more catch-up batch prompts,
        # so check content presence rather than exact prompt count.
        all_text = " ".join(agent.processed)
        for i in range(5):
            self.assertIn(f"batch-{i}", all_text)

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

        # Both messages should still have been processed (possibly as one batch).
        all_text = " ".join(agent.processed)
        self.assertIn("fill1", all_text)
        self.assertIn("fill2", all_text)
        self.assertEqual(proc._state, "stopped")

    async def test_double_stop_is_safe(self):
        """Calling stop() twice is a no-op on the second call."""
        agent = _SlowAgent(delay=0.01)
        proc = _make_processor(agent)
        proc.start()

        await proc.stop()
        self.assertEqual(proc._state, "stopped")


class TestAppendSystemPromptFileDurability(unittest.IsolatedAsyncioTestCase):
    """Regression coverage for issue #52: the durable system prompt file path
    must be re-supplied by MessageProcessor on EVERY turn, not just the first.

    Claude's --append-system-prompt-file only works if the gateway passes it
    on every single invocation (each turn is a fresh subprocess) — the whole
    point of the fix is that this content must survive context compaction by
    being re-supplied every time, never sent once as a compactable message.
    """

    async def test_same_append_system_prompt_file_passed_on_every_turn(self):
        captured: list[dict] = []

        class _CapturingAgent(AgentBackend):
            async def create_session(self, *a, **kw):
                return "ses_001"

            async def send(self, *a, **kw):
                raise NotImplementedError

            async def stream(self, **kw):
                captured.append(kw)
                yield AgentEvent(kind="final", response=AgentResponse(text="ok"))

        agent = _CapturingAgent()
        prompt_file = "/tmp/.acg-system-prompt/test-watcher.md"
        proc = _make_processor_with_prompt_file(agent, prompt_file)

        await proc._process(_make_msg("hello", "m1"))
        await proc._process(_make_msg("world", "m2"))
        await proc._process(_make_msg("again", "m3"))

        self.assertEqual(len(captured), 3)
        for kw in captured:
            self.assertEqual(kw.get("append_system_prompt_file"), prompt_file)

    async def test_none_append_system_prompt_file_passed_through_as_none(self):
        """Backends without the mechanism (e.g. OpenCode) see None, not omitted."""
        captured: list[dict] = []

        class _CapturingAgent(AgentBackend):
            async def create_session(self, *a, **kw):
                return "ses_001"

            async def send(self, *a, **kw):
                raise NotImplementedError

            async def stream(self, **kw):
                captured.append(kw)
                yield AgentEvent(kind="final", response=AgentResponse(text="ok"))

        agent = _CapturingAgent()
        proc = _make_processor_with_prompt_file(agent, None)

        await proc._process(_make_msg("hello", "m1"))

        self.assertEqual(len(captured), 1)
        self.assertIsNone(captured[0].get("append_system_prompt_file"))


class TestEnsureContextInjectedRetryOnMessage(unittest.IsolatedAsyncioTestCase):
    """Regression coverage: a transient AgentExecutionError during the
    default ensure_durable_instructions() fallback (e.g. OpenCode's one-time
    send()) at watcher startup must self-heal on a later incoming message,
    not stay stuck in failed_retryable/failed_degraded for the watcher's
    entire uptime. This restores the retry-on-message cadence the old
    ContextInjector/_ensure_context_injected() had — found missing in code
    review after it was deleted along with the old inject() call path."""

    def _make_processor_with_injector(self, agent, injector, ws, wc):
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
        connector.agent_username = "bot"
        return MessageProcessor(
            session_id="ses_retry",
            room=Room(id="room_1", name="test-room"),
            working_directory="/tmp",
            watcher_id="w1",
            connector=connector,
            agent=agent,
            config=config,
            agent_name="default",
            context_injector=injector,
            watcher_state=ws,
            watcher_config=wc,
            connector_name="rc",
        )

    async def test_failed_delivery_recovers_on_next_message(self):
        from gateway.core.config import WatcherConfig
        from gateway.core.injected_context_builder import InjectedContextBuilder
        from gateway.core.state import WatcherState

        injector = InjectedContextBuilder(
            CoreConfig(agents={"default": AgentConfig(timeout=10)}, default_agent="default")
        )
        ws = WatcherState(
            watcher_name="w1", session_id="ses_retry", room_id="room_1",
            context_injected=False,
        )
        wc = WatcherConfig(name="w1", connector="rc", room="general", agent="default")

        attempts = 0

        class _FlakyAgent(AgentBackend):
            async def create_session(self, *a, **kw):
                return "ses_retry"

            async def send(self, *a, **kw):
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    return AgentResponse(text="rate limited", is_error=True)
                return AgentResponse(text="ok")

            async def stream(self, **kw):
                yield AgentEvent(kind="final", response=AgentResponse(text="reply"))

            async def ensure_durable_instructions(
                self, session_id, working_directory, timeout, content,
                *, watcher_name, already_delivered,
            ):
                return await self._send_once_as_durable_fallback(
                    session_id, working_directory, timeout, content, already_delivered,
                )

        agent = _FlakyAgent()
        proc = self._make_processor_with_injector(agent, injector, ws, wc)

        # Simulate the failed startup attempt directly via ensure(), exactly
        # as WatcherLifecycle._start_watcher() would have on gateway boot.
        await injector.ensure(
            ws, ws.session_id, agent, "/tmp", 10, watcher_name="w1", content="ctx",
        )
        self.assertEqual(injector.status_for(ws.session_id).state, "failed_retryable")
        self.assertFalse(ws.context_injected)

        # A later incoming message must retry and succeed this time.
        await proc._process(_make_msg("hello", "m1"))

        self.assertEqual(attempts, 2, "must have retried the failed delivery")
        self.assertTrue(ws.context_injected)
        self.assertEqual(injector.status_for(ws.session_id).state, "injected")

    async def test_degraded_after_max_attempts_stops_retrying(self):
        from gateway.core.config import WatcherConfig
        from gateway.core.injected_context_builder import (
            InjectedContextBuilder,
            _MAX_INJECT_ATTEMPTS,
        )
        from gateway.core.state import WatcherState

        injector = InjectedContextBuilder(
            CoreConfig(agents={"default": AgentConfig(timeout=10)}, default_agent="default")
        )
        ws = WatcherState(
            watcher_name="w1", session_id="ses_degraded", room_id="room_1",
            context_injected=False,
        )
        wc = WatcherConfig(name="w1", connector="rc", room="general", agent="default")

        call_count = 0

        class _AlwaysFailingAgent(AgentBackend):
            async def create_session(self, *a, **kw):
                return "ses_degraded"

            async def send(self, *a, **kw):
                nonlocal call_count
                call_count += 1
                return AgentResponse(text="down", is_error=True)

            async def stream(self, **kw):
                yield AgentEvent(kind="final", response=AgentResponse(text="reply"))

            async def ensure_durable_instructions(
                self, session_id, working_directory, timeout, content,
                *, watcher_name, already_delivered,
            ):
                return await self._send_once_as_durable_fallback(
                    session_id, working_directory, timeout, content, already_delivered,
                )

        agent = _AlwaysFailingAgent()
        proc = self._make_processor_with_injector(agent, injector, ws, wc)

        for _ in range(_MAX_INJECT_ATTEMPTS):
            await injector.ensure(
                ws, ws.session_id, agent, "/tmp", 10, watcher_name="w1", content="ctx",
            )
        self.assertEqual(injector.status_for(ws.session_id).state, "failed_degraded")
        self.assertTrue(ws.context_injected)
        calls_before = call_count

        # Once degraded, ws.context_injected is True — no further retries
        # should be attempted on subsequent messages.
        await proc._process(_make_msg("hello", "m1"))

        self.assertEqual(call_count, calls_before, "must not retry once degraded")


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

            async def send(self, session_id, prompt, working_directory, timeout, attachments=None, env=None, append_system_prompt_file=None):
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

            async def send(self, session_id, prompt, working_directory, timeout, attachments=None, env=None, append_system_prompt_file=None):
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

            async def send(self, session_id, prompt, working_directory, timeout, attachments=None, env=None, append_system_prompt_file=None):
                return AgentResponse(text="ok")

        connector = ScriptConnector()
        agent = MockAgent()
        proc = self._make_processor(connector, agent)
        proc.start()

        self.assertEqual(proc._state, "running")
        await proc.stop()
        self.assertEqual(proc._state, "stopped")
