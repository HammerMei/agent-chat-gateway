"""Tests for SessionManager.dispatch_command() and shutdown() ordering.

Covers the previously-untested control-command dispatch paths and the
critical shutdown ordering invariant (stop processors THEN save state).

Run with:
    uv run python -m pytest tests/test_session_manager_commands.py -v
"""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock


def _make_manager():
    """Build a minimal SessionManager with all collaborators mocked."""
    from tests.helpers import make_bare_session_manager

    mgr = make_bare_session_manager()
    mgr._lifecycle.list_watchers = MagicMock(return_value=[])
    mgr._lifecycle.save_state = MagicMock()
    return mgr


class TestDispatchCommandList(unittest.IsolatedAsyncioTestCase):
    """dispatch_command({'cmd': 'list'}) returns watcher data."""

    async def test_the_wire_filter_reaches_the_lifecycle(self):
        """`request["states"]` → `StateFilter` is the only join between the CLI
        and the reader, and both halves being tested in isolation left it
        uncovered: mutating this call to ignore the request passed the entire
        suite while the daemon silently answered every query with the default.
        """
        from gateway.core.state import StateFilter

        mgr = _make_manager()

        await mgr.dispatch_command({"cmd": "list", "states": ["idle"]})
        self.assertEqual(
            mgr._lifecycle.list_watchers.call_args[0][0], StateFilter.IDLE
        )

        await mgr.dispatch_command(
            {"cmd": "list", "states": ["active", "failed"]}
        )
        self.assertEqual(
            mgr._lifecycle.list_watchers.call_args[0][0],
            StateFilter.ACTIVE | StateFilter.FAILED,
        )

    async def test_no_states_field_uses_the_server_side_default(self):
        """The CLI expresses "the default" by sending nothing, so the default
        has exactly one definition and it lives here."""
        from gateway.core.state import StateFilter

        mgr = _make_manager()

        await mgr.dispatch_command({"cmd": "list"})

        self.assertEqual(
            mgr._lifecycle.list_watchers.call_args[0][0], StateFilter.OPERABLE
        )

    async def test_a_non_iterable_filter_is_a_bad_request_not_a_broken_daemon(self):
        """`parse_state_filter` iterates what it is handed, so a hand-written
        socket client sending `"states": 5` raises `TypeError`.

        Escaping uncaught turns a malformed request into a per-connector
        "failed to list watchers" warning, which reads as the daemon being
        broken rather than the request being wrong. (Written because injecting
        this fault changed nothing: the `TypeError` arm shipped without a test,
        which is what a fix-and-test-in-one-edit always leaves behind.)
        """
        mgr = _make_manager()

        result = await mgr.dispatch_command({"cmd": "list", "states": 5})

        self.assertFalse(result["ok"])
        self.assertIn("states", result["error"])
        mgr._lifecycle.list_watchers.assert_not_called()

    async def test_an_unparseable_filter_is_an_error_not_a_silent_default(self):
        """A caller cannot tell from the rows that it was answered with a
        different question than the one it asked."""
        mgr = _make_manager()

        result = await mgr.dispatch_command(
            {"cmd": "list", "states": ["sleeping"]}
        )

        self.assertFalse(result["ok"])
        self.assertIn("sleeping", result["error"])
        mgr._lifecycle.list_watchers.assert_not_called()

    async def test_list_returns_watchers(self):
        mgr = _make_manager()
        mgr._lifecycle.list_watchers.return_value = [
            {"watcher_name": "support", "state": "active"}
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

    async def test_the_manager_is_drained_before_anything_stops(self):
        """The wake arms stay reachable until the connector disconnects, so a
        shutdown that stops things before disarming leaves a window where an
        idle room's message recreates a watcher nothing below will stop —
        absent from stop_all's snapshot, its save rewriting the state file
        after the final save (§2.5). Since Codex round 5 the first step is
        `drain()` — disarm PLUS waiting out in-flight starts, because an
        episode already inside start_watcher_in_room installs its processor
        after stop_all's snapshot."""
        mgr = _make_manager()
        call_order: list[str] = []

        mgr._watcher_manager = MagicMock()

        async def _drain():
            call_order.append("drain")

        mgr._watcher_manager.drain = _drain
        sweep = MagicMock()

        async def _sweep_stop():
            call_order.append("sweep_stop")

        sweep.stop = _sweep_stop
        mgr._sweep = sweep

        async def _stop_all():
            call_order.append("stop_all")

        mgr._lifecycle.stop_all = _stop_all

        await mgr.shutdown()

        self.assertEqual(call_order[:3], ["drain", "sweep_stop", "stop_all"])

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
        mgr._connector.start_inbound.assert_awaited_once()
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



class TestTheRouterWiring(unittest.IsolatedAsyncioTestCase):
    """Rules give the manager runtime effect (§2.8), and the registration order
    is load-bearing: Rocket.Chat's start_inbound attempts subscribe-all only
    when a router is already registered."""

    def _real_manager(self, rules):
        from gateway.core.session_manager import SessionManager
        from tests.helpers import make_core_config

        connector = MagicMock()
        connector.register_handler = MagicMock()
        connector.register_capacity_check = MagicMock()
        connector.register_router = MagicMock()
        connector.connect = AsyncMock()
        connector.start_inbound = AsyncMock()
        connector.trigger_history_bound = MagicMock(
            return_value="2026-08-16T10:00:00+00:00")
        return SessionManager(
            connector, {"default": MagicMock()}, "default", make_core_config(),
            state_name="rc", watcher_rules=rules,
        ), connector

    def _rule(self):
        from gateway.core.room_pattern import RoomPattern
        from gateway.core.watcher_rule import RoomMatcher, WatcherRule

        return WatcherRule(
            name="eng", connector="rc", agent="default",
            rooms=RoomMatcher(include=(RoomPattern("eng-*"),)))

    async def test_rules_register_a_router_before_connect(self):
        mgr, connector = self._real_manager([self._rule()])
        parent = MagicMock()
        parent.attach_mock(connector.register_router, "register_router")
        parent.attach_mock(connector.connect, "connect")

        await mgr.connect_only()

        names = [c[0] for c in parent.mock_calls]
        self.assertEqual(names, ["register_router", "connect"])

    async def test_omitted_rules_normalize_to_an_empty_list(self):
        """Codex round 10: the always-on manager received the declared
        default None, and the first new room's first_matching_rule raised
        iterating it instead of declining the room."""
        from tests.helpers import make_manager

        mgr = make_manager()  # watcher_rules omitted → None
        self.assertEqual(mgr._watcher_manager._rules, [],
                         "None normalizes to [] before reaching the manager")

    async def test_no_rules_still_registers_the_router(self):
        """INVERTED with Codex round 5's fix (the old pin protected
        static-only deployments, which no longer load): the manager and the
        router now exist unconditionally — removing a connector's last rule
        must not strand its hydrated rule-derived records with no router, no
        recreation and no replay. RC running subscribe-all with zero rules
        (every offer declined) is the named, accepted consequence."""
        mgr, connector = self._real_manager([])
        await mgr.connect_only()
        connector.register_router.assert_called_once()

    # The startup-replay ordering is pinned in test_startup_replay.py, which
    # asserts all four points (sync -> snapshot -> inbound -> replay). Stating
    # a weaker version of the same rule here would be a second copy of it.

    async def test_the_router_asks_the_manager_with_the_triggers_bound(self):
        from gateway.core.watcher_manager import RoomRef
        from gateway.core.watcher_rule import RoomKind

        mgr, connector = self._real_manager([self._rule()])
        mgr._watcher_manager = MagicMock()
        mgr._watcher_manager.get_or_create = AsyncMock()
        room = RoomRef(id="r1", kind=RoomKind.CHANNEL, name="eng-backend")
        trigger = {"_id": "m1"}

        await mgr._route_unclaimed_room(room, trigger)

        connector.trigger_history_bound.assert_called_once_with(trigger)
        mgr._watcher_manager.get_or_create.assert_awaited_once_with(
            "rc", room, history_before_ts="2026-08-16T10:00:00+00:00")


class TestNotifyWatcherRoomNeedsLoadedState(unittest.IsolatedAsyncioTestCase):
    """`notify_watcher_room` reads in-memory state only, so a record this
    process never loaded gets no notice.

    Pinned rather than fixed: `list` shows such a record as `failed`, so the
    two disagree — but a disk-only record can name a room the watcher has since
    moved away from, and posting an alert into that room is worse than posting
    none. The policy belongs with the notification issue, not here. This test
    exists so that changing it is a decision rather than an accident.
    """

    async def test_a_record_this_process_never_loaded_gets_no_notice(self):
        mgr = _make_manager()
        mgr._lifecycle.get_watcher_state = MagicMock(return_value=None)

        sent = await mgr.notify_watcher_room("w1", "hello")

        self.assertFalse(sent)
        mgr._connector.send_text.assert_not_called()

    async def test_a_loaded_record_does_get_one(self):
        from gateway.core.state import WatcherState

        mgr = _make_manager()
        mgr._connector.send_text = AsyncMock()
        mgr._lifecycle.get_watcher_state = MagicMock(
            return_value=WatcherState(watcher_name="w1", session_id="s", room_id="r1")
        )

        sent = await mgr.notify_watcher_room("w1", "hello")

        self.assertTrue(sent)
        self.assertEqual(mgr._connector.send_text.call_args[0][0], "r1")

if __name__ == "__main__":
    unittest.main()


# ── Appended from test_code_review_fixes.py ───────────────────────────────────

from gateway.agents import AgentBackend as _AgentBackend2  # noqa: E402
from gateway.agents.response import AgentResponse as _AgentResponse2  # noqa: E402
from tests.helpers import IsolatedTestCase as _IsolatedTestCase2  # noqa: E402


class _MockAgentBackend2(_AgentBackend2):
    def __init__(self):
        self.sent_messages = []
        self._session_counter = 0

    async def create_session(self, working_directory, extra_args=None, session_title=None):
        self._session_counter += 1
        return f"mock-session-{self._session_counter:04d}"

    async def send(self, session_id, prompt, working_directory, timeout, attachments=None, env=None, append_system_prompt_file=None):
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


class TestTheBareSessionManagerMatchesARealOne(unittest.IsolatedAsyncioTestCase):
    """`make_bare_session_manager` builds via `__new__`, so every field is set
    by hand — the same drift the connector fixture test pins, on the object
    that broke seven tests across two files when `_sweep` arrived."""

    async def test_no_field_from_init_is_missing(self):
        from tests.helpers import make_bare_session_manager, make_manager

        real = make_manager()
        missing = set(vars(real)) - set(vars(make_bare_session_manager()))
        self.assertEqual(
            missing, set(),
            "fields on a real SessionManager that make_bare_session_manager "
            "never sets — add them there, with the value __init__ gives them",
        )
