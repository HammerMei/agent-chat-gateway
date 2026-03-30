"""Tests for WatcherLifecycle lock, attachment workspace, and sync behaviours.

Consolidates tests from:
  - test_round4_fixes.py: TestWatcherLifecycleLock
  - test_round6_fixes.py: TestAttachmentWorkspaceInThread
  - test_round8_fixes.py: TestAttachmentWorkspaceRollback
  - test_round9_fixes.py: TestContextInjectedResetOnSubscribeFailure
  - test_round14_fixes.py: TestSyncWatchersHoldsLock, TestGetWatcherLock
  - test_round16_fixes.py: TestSyncWatchersPreservesBlockedAgents
  - test_code_review_fixes.py: TestWatcherLifecycleHardening, TestUnavailableAgentsBlocksWatchers
"""

from __future__ import annotations
import pytest

import asyncio
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from gateway.agents import AgentBackend
from gateway.agents.response import AgentResponse
from gateway.config import AgentConfig, WatcherConfig
from gateway.connectors.script import ScriptConnector
from gateway.core.config import CoreConfig
from gateway.core.session_manager import SessionManager

from tests.helpers import IsolatedTestCase

# Patch load_state/save_state globally so tests never touch live state files.
_patch_load_state = patch("gateway.core.state_store.load_state", return_value=[])
_patch_save_state = patch("gateway.core.state_store.save_state")


# ── Shared helpers ─────────────────────────────────────────────────────────────



pytestmark = pytest.mark.integration

class MockAgentBackend(AgentBackend):
    def __init__(self, responses=None, default_response="mock reply"):
        self._responses = list(responses or [])
        self._default_response = default_response
        self.sent_messages = []
        self._session_counter = 0

    async def create_session(self, working_directory, extra_args=None, session_title=None):
        self._session_counter += 1
        return f"mock-session-{self._session_counter:04d}"

    async def send(self, session_id, prompt, working_directory, timeout, attachments=None, env=None):
        self.sent_messages.append({"prompt": prompt, "session_id": session_id, "attachments": attachments})
        text = self._responses.pop(0) if self._responses else self._default_response
        return AgentResponse(text=text)


class CleanupTrackingAgent(MockAgentBackend):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.deleted_sessions = []

    async def delete_session(self, session_id: str) -> bool:
        self.deleted_sessions.append(session_id)
        return True


class FailingUnsubscribeConnector(ScriptConnector):
    def __init__(self, failing_room: str):
        super().__init__()
        self.failing_room = failing_room
        self.unsubscribed_rooms = []

    async def unsubscribe_room(self, room_id: str, watcher_id: str = "") -> None:
        self.unsubscribed_rooms.append((room_id, watcher_id))
        if room_id == self.failing_room:
            raise RuntimeError(f"boom for {room_id}")


def make_watcher(room="script", name=None):
    return WatcherConfig(
        name=name or room, connector="script", room=room, agent="default"
    )


def make_manager(connector, agent, watcher_configs=None, permission_registry=None):
    agent_cfg = AgentConfig(timeout=10)
    config = CoreConfig(agents={"default": agent_cfg}, default_agent="default")
    return SessionManager(
        connector,
        {"default": agent},
        "default",
        config,
        watcher_configs=watcher_configs or [],
        permission_registry=permission_registry,
    )


def _make_lifecycle_r14(watcher_names=None):
    """Build a minimal WatcherLifecycle with mocked collaborators."""
    from gateway.core.watcher_lifecycle import WatcherLifecycle
    from gateway.core.config import WatcherConfig as CoreWatcherConfig, CoreConfig

    if watcher_names is None:
        watcher_names = ["support"]

    watcher_configs = []
    for name in watcher_names:
        wc = MagicMock(spec=CoreWatcherConfig)
        wc.name = name
        wc.room = f"#{name}"
        wc.connector = "rc"
        wc.agent = None
        wc.session_id = None
        wc.online_notification = None
        wc.offline_notification = None
        watcher_configs.append(wc)

    connector = MagicMock()
    connector.resolve_room = AsyncMock(return_value=MagicMock(id="room_id", type="c"))
    connector.subscribe_room = AsyncMock()
    connector.unsubscribe_room = AsyncMock()
    connector.get_last_processed_ts = MagicMock(return_value=None)
    connector.update_last_processed_ts = MagicMock()

    agent = MagicMock()
    agent.create_session = AsyncMock(return_value="session-abc123")
    agent.delete_session = AsyncMock(return_value=True)

    config = MagicMock(spec=CoreConfig)
    agent_cfg = MagicMock()
    agent_cfg.working_directory = "/tmp"
    agent_cfg.session_prefix = None
    config.agent_config = MagicMock(return_value=agent_cfg)

    state_store = MagicMock()
    state_store.load = MagicMock(return_value={})
    state_store.save = MagicMock()

    dispatcher = MagicMock()
    dispatcher.add_processor = MagicMock()
    dispatcher.remove_processor = MagicMock()

    injector = MagicMock()
    injector.inject = AsyncMock()
    injector.reset_session = MagicMock()
    injector.status_for = MagicMock(return_value=MagicMock(state="done"))

    maps = MagicMock()
    maps.role = {}
    maps.permission_thread = {}
    maps.bind_session = MagicMock()
    maps.remove_session = MagicMock()

    lifecycle = WatcherLifecycle.__new__(WatcherLifecycle)
    lifecycle._connector = connector
    lifecycle._agents = {"default": agent}
    lifecycle._default_agent = "default"
    lifecycle._config = config
    lifecycle._watcher_configs = watcher_configs
    lifecycle._state_store = state_store
    lifecycle._dispatcher = dispatcher
    lifecycle._injector = injector
    lifecycle._permission_registry = None
    lifecycle._maps = maps
    lifecycle._processors = {}
    lifecycle._states = {}
    lifecycle._watcher_locks = {}
    lifecycle._blocked_agents = set()

    workspace = MagicMock()
    workspace.setup = MagicMock(return_value="/tmp/attachments")
    lifecycle._attachment_workspace = workspace

    return lifecycle, watcher_configs, connector, agent


# ── Tests from test_round4_fixes.py ───────────────────────────────────────────


class TestWatcherLifecycleLock(unittest.IsolatedAsyncioTestCase):
    """pause/resume/reset must be serialized per watcher via _get_watcher_lock."""

    def _make_lifecycle(self):
        from gateway.core.watcher_lifecycle import WatcherLifecycle

        lc = WatcherLifecycle.__new__(WatcherLifecycle)
        lc._connector = MagicMock()
        lc._agents = {}
        lc._default_agent = "default"
        lc._config = MagicMock()
        lc._watcher_configs = []
        lc._state_store = MagicMock()
        lc._dispatcher = MagicMock()
        lc._injector = MagicMock()
        lc._permission_registry = None
        lc._maps = MagicMock()
        lc._attachment_workspace = MagicMock()
        lc._blocked_agents = set()
        lc._processors = {}
        lc._states = {}
        lc._watcher_locks = {}
        return lc

    def test_get_watcher_lock_creates_lock_lazily(self):
        """_get_watcher_lock creates a new asyncio.Lock on first call."""
        lc = self._make_lifecycle()
        lock = lc._get_watcher_lock("watcher_a")
        self.assertIsInstance(lock, asyncio.Lock)
        self.assertIn("watcher_a", lc._watcher_locks)

    def test_get_watcher_lock_returns_same_lock_on_repeat(self):
        """_get_watcher_lock returns the same lock on subsequent calls."""
        lc = self._make_lifecycle()
        lock1 = lc._get_watcher_lock("watcher_a")
        lock2 = lc._get_watcher_lock("watcher_a")
        self.assertIs(lock1, lock2)

    def test_different_watchers_have_different_locks(self):
        """Two different watcher names must get independent locks."""
        lc = self._make_lifecycle()
        lock_a = lc._get_watcher_lock("watcher_a")
        lock_b = lc._get_watcher_lock("watcher_b")
        self.assertIsNot(lock_a, lock_b)

    async def test_concurrent_operations_on_same_watcher_are_serialized(self):
        """Two concurrent lifecycle ops on the same watcher must not interleave."""
        lc = self._make_lifecycle()
        execution_order = []

        async def op1():
            async with lc._get_watcher_lock("w1"):
                execution_order.append("op1_start")
                await asyncio.sleep(0)
                execution_order.append("op1_end")

        async def op2():
            async with lc._get_watcher_lock("w1"):
                execution_order.append("op2_start")
                execution_order.append("op2_end")

        await asyncio.gather(op1(), op2())

        self.assertEqual(
            execution_order,
            ["op1_start", "op1_end", "op2_start", "op2_end"],
        )

    async def test_concurrent_operations_on_different_watchers_run_in_parallel(self):
        """Two concurrent lifecycle ops on DIFFERENT watchers must not block each other."""
        lc = self._make_lifecycle()
        started = []

        async def op_a():
            async with lc._get_watcher_lock("watcher_a"):
                started.append("a")
                await asyncio.sleep(0)

        async def op_b():
            async with lc._get_watcher_lock("watcher_b"):
                started.append("b")
                await asyncio.sleep(0)

        await asyncio.gather(op_a(), op_b())
        self.assertIn("a", started)
        self.assertIn("b", started)


# ── Tests from test_round6_fixes.py ───────────────────────────────────────────


class TestAttachmentWorkspaceInThread(unittest.IsolatedAsyncioTestCase):
    """WatcherLifecycle._start_watcher() must call setup() via asyncio.to_thread."""

    async def test_setup_called_via_to_thread(self):
        """setup() must be wrapped in asyncio.to_thread(), not called directly."""
        from gateway.core.watcher_lifecycle import WatcherLifecycle
        from gateway.state import WatcherState

        lc = WatcherLifecycle.__new__(WatcherLifecycle)
        lc._states = {}
        lc._processors = {}
        lc._watcher_locks = {}

        maps = MagicMock()
        maps.bind_session = MagicMock()
        maps.remove_session = MagicMock()
        lc._maps = maps

        room = MagicMock()
        room.id = "room_1"
        room.type = "dm"
        connector = MagicMock()
        connector.resolve_room = AsyncMock(return_value=room)
        lc._connector = connector

        injector = MagicMock()
        injector.inject = AsyncMock()
        injector.reset_session = MagicMock()
        lc._injector = injector

        workspace = MagicMock()
        workspace.setup.return_value = "/tmp/attachments"
        lc._attachment_workspace = workspace

        agent_cfg = AgentConfig(timeout=30, working_directory="/tmp/work")
        config = MagicMock()
        config.agent_config.return_value = agent_cfg
        lc._config = config

        agent = MagicMock()
        agent.create_session = AsyncMock(return_value="ses_123")
        lc._agents = {"default": agent}

        wc = WatcherConfig(
            name="test-watcher",
            connector="rc",
            room="general",
            agent="default",
        )

        to_thread_calls: list = []
        original_to_thread = asyncio.to_thread

        async def spy_to_thread(fn, *args, **kwargs):
            to_thread_calls.append(fn)
            if fn is workspace.setup:
                return "/tmp/attachments"
            return await original_to_thread(fn, *args, **kwargs)

        with (
            patch("gateway.core.watcher_lifecycle.asyncio.to_thread", side_effect=spy_to_thread),
            patch.object(lc, "_resolve_agent_name", return_value="default"),
            patch.object(lc, "_provision_session", new_callable=AsyncMock, return_value=("ses_123", True)),
            patch.object(lc, "_cleanup_startup_session_best_effort", new_callable=AsyncMock),
        ):
            try:
                await lc._start_watcher(wc, None)
            except Exception:
                pass

        setup_calls = [fn for fn in to_thread_calls if fn is workspace.setup]
        self.assertGreaterEqual(len(setup_calls), 1, "setup() must be called via asyncio.to_thread")


# ── Tests from test_round8_fixes.py ───────────────────────────────────────────


class TestAttachmentWorkspaceRollback(unittest.IsolatedAsyncioTestCase):
    """When setup() raises, _states and _maps must be rolled back."""

    async def test_states_and_maps_rolled_back_on_setup_failure(self):
        """If attachment_workspace.setup() raises, state and maps must be cleaned up."""
        from gateway.core.watcher_lifecycle import WatcherLifecycle
        from gateway.state import WatcherState

        lc = WatcherLifecycle.__new__(WatcherLifecycle)
        lc._states = {}
        lc._processors = {}
        lc._watcher_locks = {}

        maps = MagicMock()
        maps.bind_session = MagicMock()
        maps.remove_session = MagicMock()
        lc._maps = maps

        room = MagicMock()
        room.id = "room_1"
        room.type = "dm"
        connector = MagicMock()
        connector.resolve_room = AsyncMock(return_value=room)
        lc._connector = connector

        injector = MagicMock()
        injector.inject = AsyncMock()
        injector.reset_session = MagicMock()
        lc._injector = injector

        workspace = MagicMock()
        workspace.setup.side_effect = OSError("permission denied")
        lc._attachment_workspace = workspace

        agent_cfg = AgentConfig(timeout=30, working_directory="/tmp/work")
        config = MagicMock()
        config.agent_config.return_value = agent_cfg
        lc._config = config

        agent = MagicMock()
        lc._agents = {"default": agent}

        wc = WatcherConfig(
            name="test-watcher", connector="rc", room="general", agent="default"
        )

        with (
            patch.object(lc, "_resolve_agent_name", return_value="default"),
            patch.object(lc, "_provision_session", new_callable=AsyncMock, return_value=("ses_123", True)),
            patch.object(lc, "_cleanup_startup_session_best_effort", new_callable=AsyncMock),
            patch("gateway.core.watcher_lifecycle.asyncio.to_thread", new_callable=AsyncMock,
                  side_effect=OSError("permission denied")),
        ):
            with self.assertRaises(OSError):
                await lc._start_watcher(wc, None)

        self.assertNotIn("test-watcher", lc._states, "_states must be rolled back after setup() failure")
        maps.remove_session.assert_called_once()


# ── Tests from test_round9_fixes.py ───────────────────────────────────────────


class TestContextInjectedResetOnSubscribeFailure(unittest.IsolatedAsyncioTestCase):
    """ws.context_injected must be reset when session destroyed on subscribe failure."""

    async def test_context_injected_reset_when_new_session_destroyed(self):
        """If new session is destroyed after subscribe fails, context_injected must be False."""
        from gateway.core.watcher_lifecycle import WatcherLifecycle

        lc = WatcherLifecycle.__new__(WatcherLifecycle)
        lc._states = {}
        lc._processors = {}
        lc._watcher_locks = {}
        lc._permission_registry = MagicMock()

        maps = MagicMock()
        maps.bind_session = MagicMock()
        maps.remove_session = MagicMock()
        lc._maps = maps

        room = MagicMock()
        room.id = "room_1"
        room.type = "dm"
        connector = MagicMock()
        connector.resolve_room = AsyncMock(return_value=room)
        connector.subscribe_room = AsyncMock(side_effect=RuntimeError("subscribe failed"))
        lc._connector = connector

        injector = MagicMock()
        injector.inject = AsyncMock()
        injector.reset_session = MagicMock()
        lc._injector = injector

        workspace = MagicMock()
        workspace.setup = MagicMock()
        lc._attachment_workspace = workspace

        agent_cfg = AgentConfig(timeout=30, working_directory="/tmp/work")
        config = MagicMock()
        config.agent_config.return_value = agent_cfg
        lc._config = config

        agent = MagicMock()
        lc._agents = {"default": agent}

        wc = WatcherConfig(
            name="test-watcher", connector="rc", room="general", agent="default"
        )

        with (
            patch.object(lc, "_resolve_agent_name", return_value="default"),
            patch.object(lc, "_provision_session", new_callable=AsyncMock,
                         return_value=("ses_new", True)),
            patch.object(lc, "_cleanup_startup_session_best_effort",
                         new_callable=AsyncMock, return_value=True),
            patch("gateway.core.watcher_lifecycle.asyncio.to_thread",
                  new_callable=AsyncMock, return_value=None),
        ):
            with self.assertRaises(RuntimeError):
                await lc._start_watcher(wc, None)

        saved_ws = lc._states.get("test-watcher")
        self.assertIsNotNone(saved_ws)
        self.assertFalse(saved_ws.context_injected)

    async def test_context_injected_preserved_when_no_new_session(self):
        """If wc.session_id is sticky (not empty), context_injected must be preserved."""
        from gateway.core.watcher_lifecycle import WatcherLifecycle

        lc = WatcherLifecycle.__new__(WatcherLifecycle)
        lc._states = {}
        lc._processors = {}
        lc._watcher_locks = {}
        lc._permission_registry = MagicMock()

        maps = MagicMock()
        maps.bind_session = MagicMock()
        maps.remove_session = MagicMock()
        lc._maps = maps

        room = MagicMock()
        room.id = "room_1"
        room.type = "dm"
        connector = MagicMock()
        connector.resolve_room = AsyncMock(return_value=room)
        connector.subscribe_room = AsyncMock(side_effect=RuntimeError("subscribe failed"))
        lc._connector = connector

        injector = MagicMock()
        injector.inject = AsyncMock()
        injector.reset_session = MagicMock()
        lc._injector = injector

        workspace = MagicMock()
        workspace.setup = MagicMock()
        lc._attachment_workspace = workspace

        agent_cfg = AgentConfig(timeout=30, working_directory="/tmp/work")
        config = MagicMock()
        config.agent_config.return_value = agent_cfg
        lc._config = config

        agent = MagicMock()
        lc._agents = {"default": agent}

        wc = WatcherConfig(
            name="test-watcher2", connector="rc", room="general", agent="default",
            session_id="sticky-session",
        )

        with (
            patch.object(lc, "_resolve_agent_name", return_value="default"),
            patch.object(lc, "_provision_session", new_callable=AsyncMock,
                         return_value=("sticky-session", False)),
            patch.object(lc, "_cleanup_startup_session_best_effort",
                         new_callable=AsyncMock, return_value=True),
            patch("gateway.core.watcher_lifecycle.asyncio.to_thread",
                  new_callable=AsyncMock, return_value=None),
        ):
            with self.assertRaises(RuntimeError):
                await lc._start_watcher(wc, None)

        saved_ws = lc._states.get("test-watcher2")
        self.assertIsNotNone(saved_ws)
        self.assertEqual(saved_ws.session_id, "sticky-session")


# ── Tests from test_round14_fixes.py ──────────────────────────────────────────


class TestSyncWatchersHoldsLock(unittest.IsolatedAsyncioTestCase):
    """sync_watchers must hold the per-watcher lock during _start_watcher."""

    async def test_sync_watchers_acquires_watcher_lock(self):
        """sync_watchers must acquire the per-watcher lock before calling _start_watcher."""
        lifecycle, watcher_configs, connector, agent = _make_lifecycle_r14(["support"])

        lock_was_held_during_start = False
        start_watcher_entered = asyncio.Event()
        start_watcher_proceed = asyncio.Event()

        async def slow_start_watcher(wc, state):
            start_watcher_entered.set()
            await start_watcher_proceed.wait()
            lifecycle._processors[wc.name] = MagicMock()

        async def try_acquire_lock():
            nonlocal lock_was_held_during_start
            await start_watcher_entered.wait()
            lock = lifecycle._get_watcher_lock("support")
            if lock.locked():
                lock_was_held_during_start = True
            start_watcher_proceed.set()

        with patch.object(lifecycle, "_start_watcher", slow_start_watcher):
            prober = asyncio.create_task(try_acquire_lock())
            await lifecycle.sync_watchers()
            await prober

        self.assertTrue(lock_was_held_during_start)

    async def test_pause_during_sync_watchers_blocks_until_start_completes(self):
        """pause_watcher must wait for sync_watchers' _start_watcher to finish."""
        lifecycle, watcher_configs, connector, agent = _make_lifecycle_r14(["support"])

        ordering: list[str] = []
        start_entered = asyncio.Event()
        pause_can_run = asyncio.Event()

        async def controlled_start(wc, state):
            ordering.append("start:begin")
            start_entered.set()
            await pause_can_run.wait()
            await asyncio.sleep(0)
            lifecycle._processors[wc.name] = MagicMock(stop=AsyncMock(), start=MagicMock())
            ordering.append("start:end")

        async def concurrent_pause():
            await start_entered.wait()
            pause_can_run.set()
            ordering.append("pause:attempt")
            try:
                await asyncio.wait_for(lifecycle.pause_watcher("support"), timeout=2.0)
                ordering.append("pause:done")
            except Exception:
                ordering.append("pause:error")

        with patch.object(lifecycle, "_start_watcher", controlled_start):
            pause_task = asyncio.create_task(concurrent_pause())
            await lifecycle.sync_watchers()
            await pause_task

        start_end_idx = ordering.index("start:end")
        pause_done_idx = ordering.index("pause:done") if "pause:done" in ordering else ordering.index("pause:error")
        self.assertGreater(pause_done_idx, start_end_idx)

    async def test_sync_watchers_still_starts_watcher_normally(self):
        """The lock acquisition must not break normal startup flow."""
        lifecycle, watcher_configs, connector, agent = _make_lifecycle_r14(["support"])

        processor_mock = MagicMock(start=MagicMock(), stop=AsyncMock())

        async def simple_start(wc, state):
            lifecycle._processors[wc.name] = processor_mock

        with patch.object(lifecycle, "_start_watcher", simple_start):
            errors = await lifecycle.sync_watchers()

        self.assertEqual(errors, [])
        self.assertIn("support", lifecycle._processors)

    async def test_sync_watchers_error_still_captured_with_lock(self):
        """If _start_watcher raises, the error is captured and the lock is released."""
        lifecycle, watcher_configs, connector, agent = _make_lifecycle_r14(["support"])

        async def failing_start(wc, state):
            raise RuntimeError("subscribe failed")

        with patch.object(lifecycle, "_start_watcher", failing_start):
            errors = await lifecycle.sync_watchers()

        self.assertEqual(len(errors), 1)
        self.assertIn("failed to start", errors[0])

        lock = lifecycle._get_watcher_lock("support")
        self.assertFalse(lock.locked(), "Lock is still held after _start_watcher raised!")

    async def test_multiple_watchers_each_get_own_lock(self):
        """Each watcher gets an independent lock."""
        lifecycle, watcher_configs, connector, agent = _make_lifecycle_r14(["alpha", "beta"])

        started: list[str] = []

        async def simple_start(wc, state):
            started.append(wc.name)
            lifecycle._processors[wc.name] = MagicMock(start=MagicMock(), stop=AsyncMock())

        with patch.object(lifecycle, "_start_watcher", simple_start):
            errors = await lifecycle.sync_watchers()

        self.assertEqual(errors, [])
        self.assertEqual(set(started), {"alpha", "beta"})

        for name in ["alpha", "beta"]:
            lock = lifecycle._get_watcher_lock(name)
            self.assertFalse(lock.locked())


class TestGetWatcherLock(unittest.IsolatedAsyncioTestCase):
    """_get_watcher_lock must create locks lazily and return the same lock each time."""

    async def test_returns_same_lock_on_repeated_calls(self):
        lifecycle, _, _, _ = _make_lifecycle_r14(["support"])
        lock1 = lifecycle._get_watcher_lock("support")
        lock2 = lifecycle._get_watcher_lock("support")
        self.assertIs(lock1, lock2)

    async def test_different_watchers_get_different_locks(self):
        lifecycle, _, _, _ = _make_lifecycle_r14(["alpha", "beta"])
        lock_a = lifecycle._get_watcher_lock("alpha")
        lock_b = lifecycle._get_watcher_lock("beta")
        self.assertIsNot(lock_a, lock_b)

    async def test_lock_is_asyncio_lock(self):
        lifecycle, _, _, _ = _make_lifecycle_r14(["support"])
        lock = lifecycle._get_watcher_lock("support")
        self.assertIsInstance(lock, asyncio.Lock)


# ── Tests from test_round16_fixes.py ──────────────────────────────────────────


class TestSyncWatchersPreservesBlockedAgents(unittest.IsolatedAsyncioTestCase):
    """sync_watchers(unavailable_agents=None) must not reset _blocked_agents."""

    def _make_lifecycle(self):
        from gateway.core.watcher_lifecycle import WatcherLifecycle

        lc = WatcherLifecycle.__new__(WatcherLifecycle)
        lc._connector = MagicMock()
        lc._agents = {"default": MagicMock()}
        lc._default_agent = "default"
        lc._config = MagicMock()
        lc._watcher_configs = []
        lc._state_store = MagicMock()
        lc._state_store.load = MagicMock(return_value={})
        lc._state_store.save = MagicMock()
        lc._dispatcher = MagicMock()
        lc._injector = MagicMock()
        lc._permission_registry = None
        lc._maps = MagicMock()
        lc._processors = {}
        lc._states = {}
        lc._watcher_locks = {}
        lc._blocked_agents = set()
        lc._attachment_workspace = MagicMock()
        return lc

    async def test_none_unavailable_agents_preserves_blocked_set(self):
        """sync_watchers(None) must not overwrite a previously populated _blocked_agents."""
        lc = self._make_lifecycle()
        lc._blocked_agents = {"opencode"}

        await lc.sync_watchers(unavailable_agents=None)

        self.assertIn("opencode", lc._blocked_agents)

    async def test_explicit_empty_set_clears_blocked_agents(self):
        """sync_watchers(set()) explicitly clears blocked agents."""
        lc = self._make_lifecycle()
        lc._blocked_agents = {"opencode"}

        await lc.sync_watchers(unavailable_agents=set())

        self.assertNotIn("opencode", lc._blocked_agents)

    async def test_explicit_set_updates_blocked_agents(self):
        """sync_watchers({'agent-x'}) replaces _blocked_agents with the new set."""
        lc = self._make_lifecycle()
        lc._blocked_agents = {"old-agent"}

        await lc.sync_watchers(unavailable_agents={"new-agent"})

        self.assertNotIn("old-agent", lc._blocked_agents)
        self.assertIn("new-agent", lc._blocked_agents)

    async def test_first_call_with_none_starts_with_empty_set(self):
        """On first startup (previously empty), None leaves _blocked_agents empty."""
        lc = self._make_lifecycle()
        lc._blocked_agents = set()

        await lc.sync_watchers(unavailable_agents=None)

        self.assertEqual(lc._blocked_agents, set())


# ── Tests from test_code_review_fixes.py ──────────────────────────────────────


class TestWatcherLifecycleHardening(IsolatedTestCase):
    async def test_new_session_cleaned_up_when_context_injection_fails(self):
        connector = ScriptConnector()
        agent = CleanupTrackingAgent()
        manager = make_manager(connector, agent, watcher_configs=[make_watcher()])

        with patch.object(
            manager._lifecycle._injector,
            "inject",
            AsyncMock(side_effect=RuntimeError("inject failed")),
        ):
            errors = await manager.run_once()

        self.assertEqual(len(errors), 1)
        self.assertEqual(agent.deleted_sessions, ["mock-session-0001"])
        self.assertFalse(manager._lifecycle._processors)

        await manager.shutdown()

    async def test_shutdown_continues_when_one_unsubscribe_fails(self):
        connector = FailingUnsubscribeConnector(failing_room="room-a")
        agent = MockAgentBackend()
        manager = make_manager(
            connector,
            agent,
            watcher_configs=[
                make_watcher(room="room-a", name="w1"),
                make_watcher(room="room-b", name="w2"),
            ],
        )
        await manager.run_once()

        await manager.shutdown()

        self.assertEqual(manager._lifecycle._processors, {})
        self.assertIn(("room-a", "w1"), connector.unsubscribed_rooms)
        self.assertIn(("room-b", "w2"), connector.unsubscribed_rooms)

    async def test_subscribe_failure_clears_deleted_fresh_session_from_state(self):
        connector = ScriptConnector()
        agent = CleanupTrackingAgent()
        manager = make_manager(connector, agent, watcher_configs=[make_watcher()])

        with patch.object(
            connector,
            "subscribe_room",
            AsyncMock(side_effect=RuntimeError("subscribe failed")),
        ):
            errors = await manager.run_once()

        self.assertEqual(len(errors), 1)
        state = manager._lifecycle.get_watcher_state("script")
        self.assertIsNotNone(state)
        self.assertEqual(state.session_id, "")
        self.assertEqual(agent.deleted_sessions, ["mock-session-0001"])

        await manager.shutdown()


class TestUnavailableAgentsBlocksWatchers(IsolatedTestCase):
    """P0-2: sync_watchers() must skip watchers whose agent's broker failed."""

    async def test_unavailable_agent_skips_watcher_with_error(self):
        """Watcher using a permission-broker-failed agent must not start."""
        connector = ScriptConnector()
        agent = MockAgentBackend()

        manager = make_manager(
            connector, agent, watcher_configs=[make_watcher(name="w1")]
        )

        errors = await manager.run_once(unavailable_agents={"default"})

        watchers = {w["watcher_name"]: w for w in manager.list_watchers()}
        self.assertFalse(watchers["w1"]["active"])
        self.assertTrue(
            any("default" in e for e in errors),
            f"Expected 'default' in errors: {errors}",
        )

        await manager.shutdown()

    async def test_non_blocked_agent_starts_normally(self):
        """Watchers using an agent with a healthy broker must start as usual."""
        connector = ScriptConnector()
        agent = MockAgentBackend()

        manager = make_manager(
            connector, agent, watcher_configs=[make_watcher(name="w1")]
        )

        errors = await manager.run_once(unavailable_agents={"other-agent"})

        watchers = {w["watcher_name"]: w for w in manager.list_watchers()}
        self.assertTrue(watchers["w1"]["active"])
        self.assertFalse(any("w1" in e for e in errors), f"Unexpected errors: {errors}")

        await manager.shutdown()

    async def test_empty_unavailable_agents_no_effect(self):
        """Empty unavailable_agents set must not affect normal startup."""
        connector = ScriptConnector()
        agent = MockAgentBackend()

        manager = make_manager(
            connector, agent, watcher_configs=[make_watcher(name="w1")]
        )

        errors = await manager.run_once(unavailable_agents=set())

        watchers = {w["watcher_name"]: w for w in manager.list_watchers()}
        self.assertTrue(watchers["w1"]["active"])
        self.assertEqual(errors, [])

        await manager.shutdown()


if __name__ == "__main__":
    unittest.main()
