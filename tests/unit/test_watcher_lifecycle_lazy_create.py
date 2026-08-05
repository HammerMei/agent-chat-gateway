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


if __name__ == "__main__":
    unittest.main()
