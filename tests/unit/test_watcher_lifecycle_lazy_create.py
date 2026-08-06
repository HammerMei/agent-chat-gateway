"""Unit tests for WatcherLifecycle.try_lazy_create() (docs/design/on-the-fly-watchers.md).

Covers rule matching (no rules, DM exclusion, exclude_rooms, one-rule-per-
connector), the resolve-failure/no-match fallback, name-collision safety,
dormant-session resume, idempotency against a concurrent/repeat call, and
_start_watcher failure handling — all connector/agent/state_store calls are
mocked, same harness pattern as test_history_handoff.py's
TestHistoryHandoffSentSeparatelyFromHeader.
"""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from gateway.core.config import CoreConfig, WatcherConfig
from gateway.core.connector import Room
from gateway.core.injected_context_builder import InjectedContextBuilder
from gateway.core.session_maps import SessionMaps
from gateway.core.state import WatcherState
from gateway.core.watcher_lifecycle import WatcherLifecycle


def _rule(**overrides) -> WatcherConfig:
    defaults = dict(
        name="",
        connector="mm-home",
        room="*",
        agent="claude",
        exclude_rooms=[],
    )
    defaults.update(overrides)
    return WatcherConfig(**defaults)


def _make_lifecycle(rules: list[WatcherConfig] | None, resolved_room=None, state_by_name=None):
    config = CoreConfig()
    connector = AsyncMock()
    connector.agent_username = "hammer-mei"
    connector.resolve_room_by_id = AsyncMock(
        return_value=resolved_room or Room(id="chan-1", name="general", type="channel")
    )
    connector.subscribe_room = AsyncMock()
    connector.fetch_room_history = AsyncMock(return_value=[])
    connector.get_last_processed_ts = MagicMock(return_value=None)
    connector.update_last_processed_ts = MagicMock()
    connector.attachment_cache_dir = MagicMock(return_value=None)

    agent = AsyncMock()
    agent.create_session = AsyncMock(return_value="new-sess-id")
    agent.send = AsyncMock(return_value=MagicMock(is_error=False, text="ok"))
    agent.ensure_durable_instructions = AsyncMock(return_value=None)
    agent.delete_session = AsyncMock(return_value=True)

    state_store = MagicMock()
    state_store.load = MagicMock(return_value=dict(state_by_name or {}))
    state_store.save = MagicMock()
    dispatcher = MagicMock()
    dispatcher.add_processor = MagicMock()
    injector = InjectedContextBuilder(config)
    maps = SessionMaps()

    lifecycle = WatcherLifecycle(
        connector=connector,
        agents={"claude": agent},
        default_agent="claude",
        config=config,
        watcher_configs=[],
        state_store=state_store,
        dispatcher=dispatcher,
        injector=injector,
        permission_registry=None,
        maps=maps,
        watcher_rules=rules,
    )
    lifecycle._attachment_workspace = MagicMock()
    lifecycle._attachment_workspace.setup = MagicMock(return_value="/tmp/fake")
    return lifecycle, connector, agent, state_store


class TestTryLazyCreateNoMatch(unittest.IsolatedAsyncioTestCase):
    async def test_no_rules_configured_returns_false_without_resolving(self):
        lifecycle, connector, agent, state_store = _make_lifecycle(rules=None)

        result = await lifecycle.try_lazy_create("chan-1")

        self.assertFalse(result)
        connector.resolve_room_by_id.assert_not_called()
        agent.create_session.assert_not_called()

    async def test_dm_room_never_matches(self):
        lifecycle, connector, agent, state_store = _make_lifecycle(
            rules=[_rule()],
            resolved_room=Room(id="dm-1", name="@alice", type="dm"),
        )

        result = await lifecycle.try_lazy_create("dm-1")

        self.assertFalse(result)
        agent.create_session.assert_not_called()

    async def test_room_in_exclude_rooms_does_not_match(self):
        lifecycle, connector, agent, state_store = _make_lifecycle(
            rules=[_rule(exclude_rooms=["general"])],
        )

        result = await lifecycle.try_lazy_create("chan-1")

        self.assertFalse(result)
        agent.create_session.assert_not_called()

    async def test_resolve_failure_returns_false_not_raise(self):
        lifecycle, connector, agent, state_store = _make_lifecycle(rules=[_rule()])
        connector.resolve_room_by_id = AsyncMock(side_effect=RuntimeError("boom"))

        result = await lifecycle.try_lazy_create("chan-1")

        self.assertFalse(result)


class TestTryLazyCreateMatch(unittest.IsolatedAsyncioTestCase):
    async def test_matching_rule_creates_watcher_and_returns_true(self):
        lifecycle, connector, agent, state_store = _make_lifecycle(rules=[_rule()])

        with patch("gateway.core.watcher_lifecycle.MessageProcessor") as MockProc:
            MockProc.return_value.start = MagicMock()
            result = await lifecycle.try_lazy_create("chan-1")

        self.assertTrue(result)
        agent.create_session.assert_awaited_once()
        connector.subscribe_room.assert_awaited_once()
        self.assertEqual(len(lifecycle._watcher_configs), 1)
        wc = lifecycle._watcher_configs[0]
        self.assertEqual(wc.name, "mm-home-general")
        self.assertEqual(wc.room, "general")
        self.assertEqual(wc.connector, "mm-home")
        self.assertEqual(wc.agent, "claude")
        state_store.save.assert_called()

    async def test_created_watcher_is_findable_via_get_watcher_config(self):
        lifecycle, connector, agent, state_store = _make_lifecycle(rules=[_rule()])

        with patch("gateway.core.watcher_lifecycle.MessageProcessor") as MockProc:
            MockProc.return_value.start = MagicMock()
            await lifecycle.try_lazy_create("chan-1")

        self.assertIsNotNone(lifecycle.get_watcher_config("mm-home-general"))

    async def test_resumes_dormant_session_from_persisted_state(self):
        persisted = {
            "mm-home-general": WatcherState(
                watcher_name="mm-home-general",
                session_id="old-sess-id",
                room_id="chan-1",
            )
        }
        lifecycle, connector, agent, state_store = _make_lifecycle(
            rules=[_rule()], state_by_name=persisted,
        )

        with patch("gateway.core.watcher_lifecycle.MessageProcessor") as MockProc:
            MockProc.return_value.start = MagicMock()
            result = await lifecycle.try_lazy_create("chan-1")

        self.assertTrue(result)
        agent.create_session.assert_not_called()  # resumed, not created fresh

    async def test_second_call_for_same_room_is_idempotent(self):
        lifecycle, connector, agent, state_store = _make_lifecycle(rules=[_rule()])

        with patch("gateway.core.watcher_lifecycle.MessageProcessor") as MockProc:
            MockProc.return_value.start = MagicMock()
            first = await lifecycle.try_lazy_create("chan-1")
            second = await lifecycle.try_lazy_create("chan-1")

        self.assertTrue(first)
        self.assertTrue(second)
        agent.create_session.assert_awaited_once()  # not called again
        self.assertEqual(len(lifecycle._watcher_configs), 1)  # not duplicated

    async def test_start_watcher_failure_returns_false_and_does_not_register(self):
        lifecycle, connector, agent, state_store = _make_lifecycle(rules=[_rule()])
        connector.subscribe_room = AsyncMock(side_effect=RuntimeError("subscribe failed"))

        with patch("gateway.core.watcher_lifecycle.MessageProcessor") as MockProc:
            MockProc.return_value.start = MagicMock()
            result = await lifecycle.try_lazy_create("chan-1")

        self.assertFalse(result)
        self.assertEqual(lifecycle._watcher_configs, [])

    async def test_name_collision_with_different_room_refuses(self):
        lifecycle, connector, agent, state_store = _make_lifecycle(rules=[_rule()])
        # Pre-seed a watcher whose auto-generated name would collide.
        lifecycle._watcher_configs.append(
            WatcherConfig(name="mm-home-general", connector="mm-home", room="a-different-room", agent="claude")
        )

        result = await lifecycle.try_lazy_create("chan-1")

        self.assertFalse(result)
        agent.create_session.assert_not_called()

    async def test_concurrent_calls_for_colliding_room_names_only_one_wins(self):
        """Regression test: two DIFFERENT rooms that sanitize to the SAME
        auto-generated watcher name (sanitize_room_for_name() strips '/' the
        same way it strips '-') must not both slip past the collision check
        — the check has to run INSIDE the per-name lock, not before it,
        since Mattermost delivers different channel_ids via independent
        per-channel worker queues (no natural serialization between them)."""
        import asyncio

        lifecycle, connector, agent, state_store = _make_lifecycle(rules=[_rule()])

        rooms_by_id = {
            "chan-a": Room(id="chan-a", name="a/b", type="channel"),
            "chan-b": Room(id="chan-b", name="a-b", type="channel"),
        }
        connector.resolve_room_by_id = AsyncMock(side_effect=lambda rid: rooms_by_id[rid])

        with patch("gateway.core.watcher_lifecycle.MessageProcessor") as MockProc:
            MockProc.return_value.start = MagicMock()
            result_a, result_b = await asyncio.gather(
                lifecycle.try_lazy_create("chan-a"),
                lifecycle.try_lazy_create("chan-b"),
            )

        # Exactly one of the two must have won — never both (that would mean
        # the second silently reused/overwrote the first's watcher).
        self.assertEqual(sorted([result_a, result_b]), [False, True])
        self.assertEqual(len(lifecycle._watcher_configs), 1)


class TestTryLazyCreateFailClosedAndPause(unittest.IsolatedAsyncioTestCase):
    """PR #79 review findings: lazy creation must respect the same
    fail-closed-for-unavailable-agents posture as sync_watchers(), and must
    never implicitly (re)start an already-known (e.g. paused) watcher."""

    async def test_blocked_agent_refuses_to_create(self):
        lifecycle, connector, agent, state_store = _make_lifecycle(rules=[_rule(agent="claude")])
        lifecycle._blocked_agents = {"claude"}

        result = await lifecycle.try_lazy_create("chan-1")

        self.assertFalse(result)
        connector.subscribe_room.assert_not_called()
        agent.create_session.assert_not_called()
        self.assertEqual(lifecycle._watcher_configs, [])

    async def test_unblocked_agent_still_creates(self):
        """Sanity check for the fail-closed test above — confirms the
        refusal is actually gated on _blocked_agents, not some other bug."""
        lifecycle, connector, agent, state_store = _make_lifecycle(rules=[_rule(agent="claude")])
        lifecycle._blocked_agents = {"some-other-agent"}

        with patch("gateway.core.watcher_lifecycle.MessageProcessor") as MockProc:
            MockProc.return_value.start = MagicMock()
            result = await lifecycle.try_lazy_create("chan-1")

        self.assertTrue(result)

    async def test_paused_watcher_for_same_room_is_not_resumed(self):
        lifecycle, connector, agent, state_store = _make_lifecycle(rules=[_rule()])
        # A watcher config for this exact room already exists (could be
        # static or a previous lazy creation) and is paused — no processor.
        lifecycle._watcher_configs.append(
            WatcherConfig(name="mm-home-general", connector="mm-home", room="general", agent="claude")
        )
        lifecycle._states["mm-home-general"] = WatcherState(
            watcher_name="mm-home-general", session_id="sess-1", room_id="chan-1", paused=True,
        )

        result = await lifecycle.try_lazy_create("chan-1")

        self.assertFalse(result)
        agent.create_session.assert_not_called()
        connector.subscribe_room.assert_not_called()

    async def test_stopped_unpaused_watcher_for_same_room_is_not_restarted(self):
        """Even without an explicit pause flag, a WatcherConfig for this
        room with no running processor (e.g. it failed to start earlier)
        must not be silently retried via the lazy path — only
        pause_watcher()/resume_watcher()/reset_watcher() manage it."""
        lifecycle, connector, agent, state_store = _make_lifecycle(rules=[_rule()])
        lifecycle._watcher_configs.append(
            WatcherConfig(name="mm-home-general", connector="mm-home", room="general", agent="claude")
        )

        result = await lifecycle.try_lazy_create("chan-1")

        self.assertFalse(result)
        agent.create_session.assert_not_called()

    async def test_running_watcher_for_same_room_returns_true_without_recreating(self):
        lifecycle, connector, agent, state_store = _make_lifecycle(rules=[_rule()])
        wc = WatcherConfig(name="mm-home-general", connector="mm-home", room="general", agent="claude")
        lifecycle._watcher_configs.append(wc)
        lifecycle._processors["mm-home-general"] = MagicMock()

        result = await lifecycle.try_lazy_create("chan-1")

        self.assertTrue(result)
        agent.create_session.assert_not_called()

    async def test_paused_persisted_state_after_restart_is_not_auto_resumed(self):
        """PR #79 review, second round: after a restart, a lazily-created
        watcher's WatcherConfig is gone from _watcher_configs (never
        persisted, only its runtime state is — see dynamically_created),
        so get_watcher_config() returns None and the earlier same-process
        pause check never fires. Without a SECOND check against the loaded
        WatcherState itself, a paused lazy watcher would get silently
        resumed by the very next message post-restart."""
        lifecycle, connector, agent, state_store = _make_lifecycle(
            rules=[_rule()],
            state_by_name={
                "mm-home-general": WatcherState(
                    watcher_name="mm-home-general",
                    session_id="old-sess-id",
                    room_id="chan-1",
                    paused=True,
                    dynamically_created=True,
                )
            },
        )
        # Simulates post-restart: _watcher_configs has nothing for this room.
        self.assertIsNone(lifecycle.get_watcher_config("mm-home-general"))

        result = await lifecycle.try_lazy_create("chan-1")

        self.assertFalse(result)
        agent.create_session.assert_not_called()
        connector.subscribe_room.assert_not_called()


class TestSyncWatchersPreservesLazyState(unittest.IsolatedAsyncioTestCase):
    """PR #79 review finding: sync_watchers()'s final save() only persists
    self._states, built solely from _watcher_configs — a lazily-created
    watcher's WatcherState (never in _watcher_configs, since its
    WatcherConfig only ever lived in memory) would otherwise be silently
    dropped on the very next restart, breaking dormant-session resume after
    exactly one restart."""

    async def test_dynamically_created_state_survives_a_sync_watchers_call(self):
        lifecycle, connector, agent, state_store = _make_lifecycle(rules=None)
        persisted_lazy_state = WatcherState(
            watcher_name="mm-home-general",
            session_id="old-sess-id",
            room_id="chan-1",
            dynamically_created=True,
        )
        state_store.load = MagicMock(return_value={"mm-home-general": persisted_lazy_state})

        await lifecycle.sync_watchers()

        saved_states = state_store.save.call_args.args[0]
        self.assertIn("mm-home-general", saved_states)
        self.assertEqual(saved_states["mm-home-general"].session_id, "old-sess-id")

    async def test_non_dynamically_created_orphan_state_is_still_dropped(self):
        """Unchanged pre-existing behavior: a genuinely-removed static
        watcher's old state must still be pruned on the next save — only
        dynamically-created entries get the preservation treatment."""
        lifecycle, connector, agent, state_store = _make_lifecycle(rules=None)
        persisted_orphan_state = WatcherState(
            watcher_name="rc-old-watcher", session_id="old-sess-id", room_id="r1",
            dynamically_created=False,
        )
        state_store.load = MagicMock(return_value={"rc-old-watcher": persisted_orphan_state})

        await lifecycle.sync_watchers()

        saved_states = state_store.save.call_args.args[0]
        self.assertNotIn("rc-old-watcher", saved_states)


if __name__ == "__main__":
    unittest.main()
