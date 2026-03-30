"""Tests for SessionManager.dispatch_command() and shutdown() ordering.

Covers the previously-untested control-command dispatch paths and the
critical shutdown ordering invariant (stop processors THEN save state).

Run with:
    uv run python -m pytest tests/test_session_manager_commands.py -v
"""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, call, patch


def _make_manager():
    """Build a minimal SessionManager with all collaborators mocked."""
    from gateway.core.session_manager import SessionManager

    mgr = SessionManager.__new__(SessionManager)
    mgr._connector = MagicMock()
    mgr._connector.register_handler = MagicMock()
    mgr._connector.register_capacity_check = MagicMock()
    mgr._connector.connect = AsyncMock()
    mgr._connector.disconnect = AsyncMock()
    mgr._lifecycle = MagicMock()
    mgr._lifecycle.list_watchers = MagicMock(return_value=[])
    mgr._lifecycle.pause_watcher = AsyncMock()
    mgr._lifecycle.resume_watcher = AsyncMock()
    mgr._lifecycle.reset_watcher = AsyncMock()
    mgr._lifecycle.stop_all = AsyncMock()
    mgr._lifecycle.save_state = MagicMock()
    mgr._lifecycle.sync_watchers = AsyncMock(return_value=[])
    mgr._dispatcher = MagicMock()
    mgr._dispatcher.dispatch = MagicMock()
    mgr._dispatcher.has_capacity = MagicMock()
    mgr._injector = MagicMock()
    mgr._state_store = MagicMock()
    return mgr


class TestDispatchCommandList(unittest.IsolatedAsyncioTestCase):
    """dispatch_command({'cmd': 'list'}) returns watcher data."""

    async def test_list_returns_watchers(self):
        mgr = _make_manager()
        mgr._lifecycle.list_watchers.return_value = [
            {"watcher_name": "support", "active": True}
        ]
        result = await mgr.dispatch_command({"cmd": "list"})
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["data"]), 1)
        self.assertEqual(result["data"][0]["watcher_name"], "support")

    async def test_list_returns_empty_list(self):
        mgr = _make_manager()
        mgr._lifecycle.list_watchers.return_value = []
        result = await mgr.dispatch_command({"cmd": "list"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"], [])


class TestDispatchCommandPause(unittest.IsolatedAsyncioTestCase):
    """dispatch_command({'cmd': 'pause', ...})"""

    async def test_pause_success(self):
        mgr = _make_manager()
        result = await mgr.dispatch_command({"cmd": "pause", "watcher_name": "support"})
        self.assertTrue(result["ok"])
        mgr._lifecycle.pause_watcher.assert_called_once_with("support")

    async def test_pause_failure_returns_error(self):
        mgr = _make_manager()
        mgr._lifecycle.pause_watcher.side_effect = RuntimeError("not found")
        result = await mgr.dispatch_command({"cmd": "pause", "watcher_name": "ghost"})
        self.assertFalse(result["ok"])
        self.assertIn("not found", result["error"])

    async def test_pause_empty_watcher_name(self):
        """Q1: Empty watcher_name must return a structured error immediately,
        without forwarding the call to the lifecycle layer."""
        mgr = _make_manager()
        result = await mgr.dispatch_command({"cmd": "pause"})
        self.assertFalse(result["ok"])
        self.assertIn("watcher_name", result["error"])
        # Lifecycle must NOT be called — the guard fires before delegation
        mgr._lifecycle.pause_watcher.assert_not_called()

    async def test_pause_explicit_empty_string_watcher_name(self):
        """Explicitly passing watcher_name='' must also be rejected early."""
        mgr = _make_manager()
        result = await mgr.dispatch_command({"cmd": "pause", "watcher_name": ""})
        self.assertFalse(result["ok"])
        mgr._lifecycle.pause_watcher.assert_not_called()


class TestDispatchCommandResume(unittest.IsolatedAsyncioTestCase):
    """dispatch_command({'cmd': 'resume', ...})"""

    async def test_resume_success(self):
        mgr = _make_manager()
        result = await mgr.dispatch_command({"cmd": "resume", "watcher_name": "support"})
        self.assertTrue(result["ok"])
        mgr._lifecycle.resume_watcher.assert_called_once_with("support")

    async def test_resume_failure_returns_error(self):
        mgr = _make_manager()
        mgr._lifecycle.resume_watcher.side_effect = ValueError("not paused")
        result = await mgr.dispatch_command({"cmd": "resume", "watcher_name": "support"})
        self.assertFalse(result["ok"])
        self.assertIn("not paused", result["error"])

    async def test_resume_empty_watcher_name_rejected_early(self):
        """Q1: Empty watcher_name for 'resume' must be rejected before lifecycle."""
        mgr = _make_manager()
        result = await mgr.dispatch_command({"cmd": "resume"})
        self.assertFalse(result["ok"])
        self.assertIn("watcher_name", result["error"])
        mgr._lifecycle.resume_watcher.assert_not_called()


class TestDispatchCommandReset(unittest.IsolatedAsyncioTestCase):
    """dispatch_command({'cmd': 'reset', ...})"""

    async def test_reset_success(self):
        mgr = _make_manager()
        result = await mgr.dispatch_command({"cmd": "reset", "watcher_name": "support"})
        self.assertTrue(result["ok"])
        mgr._lifecycle.reset_watcher.assert_called_once_with("support")

    async def test_reset_failure_returns_error(self):
        mgr = _make_manager()
        mgr._lifecycle.reset_watcher.side_effect = RuntimeError("watcher not found")
        result = await mgr.dispatch_command({"cmd": "reset", "watcher_name": "ghost"})
        self.assertFalse(result["ok"])
        self.assertIn("watcher not found", result["error"])

    async def test_reset_empty_watcher_name_rejected_early(self):
        """Q1: Empty watcher_name for 'reset' must be rejected before lifecycle."""
        mgr = _make_manager()
        result = await mgr.dispatch_command({"cmd": "reset"})
        self.assertFalse(result["ok"])
        self.assertIn("watcher_name", result["error"])
        mgr._lifecycle.reset_watcher.assert_not_called()


class TestDispatchCommandUnknown(unittest.IsolatedAsyncioTestCase):
    """Unknown commands return ok=False."""

    async def test_unknown_command(self):
        mgr = _make_manager()
        result = await mgr.dispatch_command({"cmd": "reboot"})
        self.assertFalse(result["ok"])
        self.assertIn("reboot", result["error"])

    async def test_missing_cmd_key(self):
        mgr = _make_manager()
        result = await mgr.dispatch_command({})
        self.assertFalse(result["ok"])


class TestShutdownOrdering(unittest.IsolatedAsyncioTestCase):
    """shutdown() must stop processors BEFORE saving state.

    This ordering is critical: if save_state() ran first, it would persist
    stale watermarks and cause duplicate message delivery on the next restart.
    """

    async def test_stop_all_called_before_save_state(self):
        mgr = _make_manager()

        call_order: list[str] = []

        async def _stop_all():
            call_order.append("stop_all")

        def _save_state():
            call_order.append("save_state")

        mgr._lifecycle.stop_all = _stop_all
        mgr._lifecycle.save_state = _save_state

        await mgr.shutdown()

        self.assertEqual(call_order[:2], ["stop_all", "save_state"])

    async def test_disconnect_called_after_save_state(self):
        mgr = _make_manager()

        call_order: list[str] = []

        async def _stop_all():
            call_order.append("stop_all")

        def _save_state():
            call_order.append("save_state")

        async def _disconnect():
            call_order.append("disconnect")

        mgr._lifecycle.stop_all = _stop_all
        mgr._lifecycle.save_state = _save_state
        mgr._connector.disconnect = _disconnect

        await mgr.shutdown()

        self.assertEqual(call_order, ["stop_all", "save_state", "disconnect"])

    async def test_shutdown_calls_all_three_steps(self):
        mgr = _make_manager()
        await mgr.shutdown()
        mgr._lifecycle.stop_all.assert_called_once()
        mgr._lifecycle.save_state.assert_called_once()
        mgr._connector.disconnect.assert_called_once()


class TestRunOnce(unittest.IsolatedAsyncioTestCase):
    """run_once() wires the dispatcher and syncs watchers."""

    async def test_run_once_connects_and_syncs(self):
        mgr = _make_manager()
        errors = await mgr.run_once()
        mgr._connector.connect.assert_called_once()
        mgr._lifecycle.sync_watchers.assert_called_once()
        self.assertEqual(errors, [])

    async def test_run_once_registers_handler(self):
        mgr = _make_manager()
        await mgr.run_once()
        mgr._connector.register_handler.assert_called_once()
        mgr._connector.register_capacity_check.assert_called_once()

    async def test_run_once_forwards_unavailable_agents(self):
        mgr = _make_manager()
        unavailable = {"slow-agent"}
        await mgr.run_once(unavailable_agents=unavailable)
        mgr._lifecycle.sync_watchers.assert_called_once_with(unavailable_agents=unavailable)


if __name__ == "__main__":
    unittest.main()


# ── Appended from test_code_review_fixes.py ───────────────────────────────────

from gateway.agents import AgentBackend as _AgentBackend2
from gateway.agents.response import AgentResponse as _AgentResponse2
from tests.helpers import IsolatedTestCase as _IsolatedTestCase2


class _MockAgentBackend2(_AgentBackend2):
    def __init__(self):
        self.sent_messages = []
        self._session_counter = 0

    async def create_session(self, working_directory, extra_args=None, session_title=None):
        self._session_counter += 1
        return f"mock-session-{self._session_counter:04d}"

    async def send(self, session_id, prompt, working_directory, timeout, attachments=None, env=None):
        self.sent_messages.append({"prompt": prompt, "session_id": session_id, "attachments": attachments})
        return _AgentResponse2(text="mock reply")


def _make_watcher_sm(room="script", name=None):
    from gateway.config import WatcherConfig
    return WatcherConfig(
        name=name or room, connector="script", room=room, agent="default"
    )


def _make_manager_sm(connector, agent, watcher_configs=None):
    from gateway.config import AgentConfig
    from gateway.core.config import CoreConfig
    from gateway.core.session_manager import SessionManager

    agent_cfg = AgentConfig(timeout=10)
    config = CoreConfig(agents={"default": agent_cfg}, default_agent="default")
    return SessionManager(
        connector,
        {"default": agent},
        "default",
        config,
        watcher_configs=watcher_configs or [],
    )


class TestDispatchCommandPublic(_IsolatedTestCase2):
    """Issue #3: dispatch_command must be public (no underscore prefix)."""

    async def test_dispatch_command_is_public(self):
        from gateway.connectors.script import ScriptConnector

        connector = ScriptConnector()
        agent = _MockAgentBackend2()
        manager = _make_manager_sm(connector, agent, watcher_configs=[_make_watcher_sm()])
        await manager.run_once()

        result = await manager.dispatch_command({"cmd": "list"})
        self.assertTrue(result["ok"])
        self.assertIsInstance(result["data"], list)

        await manager.shutdown()

    async def test_dispatch_unknown_command(self):
        from gateway.connectors.script import ScriptConnector

        connector = ScriptConnector()
        agent = _MockAgentBackend2()
        manager = _make_manager_sm(connector, agent, watcher_configs=[])
        await manager.run_once()

        result = await manager.dispatch_command({"cmd": "nonexistent"})
        self.assertFalse(result["ok"])
        self.assertIn("Unknown command", result["error"])

        await manager.shutdown()
