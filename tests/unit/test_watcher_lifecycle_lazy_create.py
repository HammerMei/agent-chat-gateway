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


def _make_lifecycle(
    rules: list[WatcherConfig] | None,
    resolved_room=None,
    state_by_name=None,
    reserve_global_name=None,
    release_global_name=None,
):
    config = CoreConfig()
    room = resolved_room or Room(id="chan-1", name="general", type="channel")
    connector = AsyncMock()
    connector.agent_username = "hammer-mei"
    connector.resolve_room_by_id = AsyncMock(return_value=room)
    # _start_watcher() (called by resume_watcher()/reset_watcher()/
    # sync_watchers(), not just the lazy path) resolves by NAME, not ID —
    # wired to the SAME room so its room.id is consistent with
    # resolve_room_by_id()'s, which the sixth-round mismatched-room-id
    # check (see WatcherLifecycle._start_watcher) now compares against.
    connector.resolve_room = AsyncMock(return_value=room)
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
        reserve_global_name=reserve_global_name,
        release_global_name=release_global_name,
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

    async def test_persisted_state_for_a_colliding_different_room_is_not_reused(self):
        """PR #79 review, fourth round: the persisted entry under this
        auto-generated NAME might belong to a DIFFERENT room from a PRIOR
        run (name collision across runs, not just within one) — reusing
        its session_id would leak that other room's conversation context
        into this one, violating the 1-session-per-room invariant. Must be
        checked by room_id, not by trusting the name-keyed lookup alone."""
        persisted = {
            "mm-home-general": WatcherState(
                watcher_name="mm-home-general",
                session_id="belongs-to-a-different-room",
                room_id="some-other-chan-id",  # NOT "chan-1"
            )
        }
        lifecycle, connector, agent, state_store = _make_lifecycle(
            rules=[_rule()], state_by_name=persisted,
        )

        result = await lifecycle.try_lazy_create("chan-1")

        self.assertFalse(result)
        agent.create_session.assert_not_called()
        connector.subscribe_room.assert_not_called()

    async def test_persisted_state_with_no_room_id_yet_is_still_usable(self):
        """A state with an empty room_id (e.g. seeded by pause_watcher()'s
        own not-found fallback, which sets room_id="") has nothing to
        compare against — must not be treated as a false collision."""
        persisted = {
            "mm-home-general": WatcherState(
                watcher_name="mm-home-general", session_id="old-sess-id", room_id="",
            )
        }
        lifecycle, connector, agent, state_store = _make_lifecycle(
            rules=[_rule()], state_by_name=persisted,
        )

        with patch("gateway.core.watcher_lifecycle.MessageProcessor") as MockProc:
            MockProc.return_value.start = MagicMock()
            result = await lifecycle.try_lazy_create("chan-1")

        self.assertTrue(result)
        agent.create_session.assert_not_called()  # resumed the old session_id anyway

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

    async def test_custom_named_static_watcher_owning_the_room_is_respected(self):
        """PR #79 review, fourth round: a static watcher for this exact
        room can have an explicit, custom `name:` that does NOT match the
        auto-generated name — get_watcher_config(auto_name) alone would
        never find it, so the room-based check must search ALL
        _watcher_configs by `.room`, not just look up the generated name."""
        lifecycle, connector, agent, state_store = _make_lifecycle(rules=[_rule()])
        lifecycle._watcher_configs.append(
            WatcherConfig(name="incident-agent", connector="mm-home", room="general", agent="claude")
        )
        # incident-agent is paused/stopped — no processor.

        result = await lifecycle.try_lazy_create("chan-1")

        self.assertFalse(result)
        agent.create_session.assert_not_called()
        connector.subscribe_room.assert_not_called()
        # No phantom second watcher created under the auto-generated name.
        self.assertIsNone(lifecycle.get_watcher_config("mm-home-general"))

    async def test_custom_named_static_watcher_owning_the_room_and_running(self):
        lifecycle, connector, agent, state_store = _make_lifecycle(rules=[_rule()])
        lifecycle._watcher_configs.append(
            WatcherConfig(name="incident-agent", connector="mm-home", room="general", agent="claude")
        )
        lifecycle._processors["incident-agent"] = MagicMock()

        result = await lifecycle.try_lazy_create("chan-1")

        self.assertTrue(result)
        agent.create_session.assert_not_called()

    async def test_renamed_room_still_matches_paused_watcher_by_stable_id(self):
        """PR #79 review, fifth round: WatcherConfig.room is a NAME, not
        stable — if the platform channel is renamed while its watcher is
        paused, the just-resolved room.name no longer matches the config's
        stale stored name even though it's the exact same room (same
        room.id). Must fall back to matching by state.room_id."""
        lifecycle, connector, agent, state_store = _make_lifecycle(
            rules=[_rule()],
            resolved_room=Room(id="chan-1", name="general-renamed", type="channel"),
        )
        lifecycle._watcher_configs.append(
            WatcherConfig(name="mm-home-general", connector="mm-home", room="general", agent="claude")
        )
        lifecycle._states["mm-home-general"] = WatcherState(
            watcher_name="mm-home-general", session_id="s1", room_id="chan-1", paused=True,
        )

        result = await lifecycle.try_lazy_create("chan-1")

        self.assertFalse(result)
        agent.create_session.assert_not_called()
        connector.subscribe_room.assert_not_called()
        # No phantom second watcher created under the new name either.
        self.assertIsNone(lifecycle.get_watcher_config("mm-home-general-renamed"))

    async def test_renamed_room_matches_running_watcher_by_stable_id_too(self):
        lifecycle, connector, agent, state_store = _make_lifecycle(
            rules=[_rule()],
            resolved_room=Room(id="chan-1", name="general-renamed", type="channel"),
        )
        lifecycle._watcher_configs.append(
            WatcherConfig(name="mm-home-general", connector="mm-home", room="general", agent="claude")
        )
        lifecycle._states["mm-home-general"] = WatcherState(
            watcher_name="mm-home-general", session_id="s1", room_id="chan-1",
        )
        lifecycle._processors["mm-home-general"] = MagicMock()

        result = await lifecycle.try_lazy_create("chan-1")

        self.assertTrue(result)
        agent.create_session.assert_not_called()

    async def test_no_room_id_match_and_no_name_match_still_creates_fresh(self):
        """Sanity check: an unrelated existing watcher for a genuinely
        different room (different room.id AND different name) must not
        block a fresh creation."""
        lifecycle, connector, agent, state_store = _make_lifecycle(rules=[_rule()])
        lifecycle._watcher_configs.append(
            WatcherConfig(name="mm-home-dev", connector="mm-home", room="dev", agent="claude")
        )
        lifecycle._states["mm-home-dev"] = WatcherState(
            watcher_name="mm-home-dev", session_id="s1", room_id="chan-dev",
        )

        with patch("gateway.core.watcher_lifecycle.MessageProcessor") as MockProc:
            MockProc.return_value.start = MagicMock()
            result = await lifecycle.try_lazy_create("chan-1")

        self.assertTrue(result)
        self.assertIsNotNone(lifecycle.get_watcher_config("mm-home-general"))

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


class TestTryLazyCreateGlobalNameUniqueness(unittest.IsolatedAsyncioTestCase):
    """PR #79 review, fourth AND seventh rounds: try_lazy_create()'s own
    collision checks only see THIS connector's watchers — ControlServer
    routing and the scheduler both assume watcher names are globally
    unique across every connector, so an atomic reserve_global_name/
    release_global_name pair (wired from GatewayService, which alone has
    cross-connector visibility) must be consulted before finalizing a lazy
    creation."""

    async def test_name_taken_by_a_different_connector_refuses(self):
        lifecycle, connector, agent, state_store = _make_lifecycle(
            rules=[_rule()],
            reserve_global_name=AsyncMock(return_value=False),
        )

        result = await lifecycle.try_lazy_create("chan-1")

        self.assertFalse(result)
        agent.create_session.assert_not_called()
        connector.subscribe_room.assert_not_called()

    async def test_name_available_globally_still_creates(self):
        lifecycle, connector, agent, state_store = _make_lifecycle(
            rules=[_rule()],
            reserve_global_name=AsyncMock(return_value=True),
        )

        with patch("gateway.core.watcher_lifecycle.MessageProcessor") as MockProc:
            MockProc.return_value.start = MagicMock()
            result = await lifecycle.try_lazy_create("chan-1")

        self.assertTrue(result)

    async def test_no_callback_provided_defaults_to_available(self):
        """Existing callers/tests constructing a WatcherLifecycle with no
        GatewayService above them (no cross-connector name space to
        protect) must keep working unchanged."""
        lifecycle, connector, agent, state_store = _make_lifecycle(rules=[_rule()])

        with patch("gateway.core.watcher_lifecycle.MessageProcessor") as MockProc:
            MockProc.return_value.start = MagicMock()
            result = await lifecycle.try_lazy_create("chan-1")

        self.assertTrue(result)

    async def test_reservation_released_after_successful_creation(self):
        """The transient reservation must not be left held forever once the
        watcher is durably registered in _watcher_configs — a caller that
        never releases it would permanently block that name from ever
        being reserved again by anyone (including a legitimate later
        reconstruction probe of the very watcher just created)."""
        release = MagicMock()
        lifecycle, connector, agent, state_store = _make_lifecycle(
            rules=[_rule()],
            reserve_global_name=AsyncMock(return_value=True),
            release_global_name=release,
        )

        with patch("gateway.core.watcher_lifecycle.MessageProcessor") as MockProc:
            MockProc.return_value.start = MagicMock()
            result = await lifecycle.try_lazy_create("chan-1")

        self.assertTrue(result)
        release.assert_called_once_with("mm-home-general")

    async def test_reservation_released_after_refused_reservation_is_a_noop(self):
        """release_global_name is NOT called when the reservation itself
        was refused — nothing was reserved, so there is nothing to
        release; calling it anyway would incorrectly clear a DIFFERENT
        in-flight caller's legitimate reservation for the same name."""
        release = MagicMock()
        lifecycle, connector, agent, state_store = _make_lifecycle(
            rules=[_rule()],
            reserve_global_name=AsyncMock(return_value=False),
            release_global_name=release,
        )

        result = await lifecycle.try_lazy_create("chan-1")

        self.assertFalse(result)
        release.assert_not_called()

    async def test_reservation_released_after_start_watcher_failure(self):
        """A failure partway through creation (e.g. _start_watcher raising)
        must still release the reservation — otherwise a single transient
        failure (network blip subscribing, etc.) would permanently blackhole
        that name for every future attempt, on every connector."""
        release = MagicMock()
        lifecycle, connector, agent, state_store = _make_lifecycle(
            rules=[_rule()],
            reserve_global_name=AsyncMock(return_value=True),
            release_global_name=release,
        )
        connector.subscribe_room = AsyncMock(side_effect=RuntimeError("boom"))

        result = await lifecycle.try_lazy_create("chan-1")

        self.assertFalse(result)
        release.assert_called_once_with("mm-home-general")


class TestTryLazyCreateDormantWatcherReclaimsOwnName(unittest.IsolatedAsyncioTestCase):
    """PR #79 review, seventh round finding #20, revised in the ninth
    round: a dynamic watcher's persisted state can outlive its
    WatcherConfig across a restart. The seventh round's cross-connector
    reservation fans out to a probe on every connector — including the
    caller's own — which would truthfully report an un-paused dynamic
    watcher's ordinary reclaim of its own name as "already occupied," by
    itself, and refuse forever.

    The eighth round's fix worked around this INSIDE WatcherLifecycle by
    skipping the reservation call entirely for a self-reclaim — but that
    then let a DIFFERENT connector's meanwhile-configured static watcher
    claim the very same name completely unchecked (ninth round, finding
    #22). The real fix belongs at the GatewayService layer instead
    (`_reserve_watcher_name()`, tested separately in
    test_gateway_service.py): it excludes the REQUESTING connector's own
    entry from the "does anyone already have this" scan, so
    `reserve_global_name` is now called UNCONDITIONALLY by
    try_lazy_create() again — these tests exercise WatcherLifecycle's side
    of that contract: it must always call the callback and trust the
    result, never trying to special-case self-reclaim on its own."""

    def _dormant_state(self, *, paused: bool = False) -> dict:
        return {
            "mm-home-general": WatcherState(
                watcher_name="mm-home-general",
                session_id="old-sess-id",
                room_id="chan-1",
                paused=paused,
                dynamically_created=True,
            )
        }

    async def test_unpaused_dormant_watcher_resumes_when_reservation_grants_it(self):
        """A correctly self-excluding reserve_global_name (as
        GatewayService now provides) returns True for a self-reclaim — the
        watcher must resume using its OLD session, not a fresh one."""
        reserve = AsyncMock(return_value=True)
        lifecycle, connector, agent, state_store = _make_lifecycle(
            rules=[_rule()],
            state_by_name=self._dormant_state(),
            reserve_global_name=reserve,
        )

        with patch("gateway.core.watcher_lifecycle.MessageProcessor") as MockProc:
            MockProc.return_value.start = MagicMock()
            result = await lifecycle.try_lazy_create("chan-1")

        self.assertTrue(result)
        reserve.assert_awaited_once_with("mm-home-general")
        agent.create_session.assert_not_called()  # resumed the OLD session
        connector.subscribe_room.assert_awaited_once()

    async def test_reservation_released_after_a_successful_self_reclaim(self):
        """Reservation is now always taken (even for a self-reclaim), so it
        must always be released too, exactly like any other successful
        creation."""
        release = MagicMock()
        lifecycle, connector, agent, state_store = _make_lifecycle(
            rules=[_rule()],
            state_by_name=self._dormant_state(),
            reserve_global_name=AsyncMock(return_value=True),
            release_global_name=release,
        )

        with patch("gateway.core.watcher_lifecycle.MessageProcessor") as MockProc:
            MockProc.return_value.start = MagicMock()
            await lifecycle.try_lazy_create("chan-1")

        release.assert_called_once_with("mm-home-general")

    async def test_paused_dormant_watcher_still_not_auto_resumed(self):
        """The existing (second-round) paused check still applies after a
        granted reservation — only "not paused" dormant watchers auto-resume."""
        lifecycle, connector, agent, state_store = _make_lifecycle(
            rules=[_rule()],
            state_by_name=self._dormant_state(paused=True),
            reserve_global_name=AsyncMock(return_value=True),
        )

        result = await lifecycle.try_lazy_create("chan-1")

        self.assertFalse(result)
        connector.subscribe_room.assert_not_called()

    async def test_foreign_ghost_state_under_the_same_name_still_uses_reservation(self):
        """A DIFFERENT room's stale persisted state colliding on this exact
        auto-generated name is caught by the existing room_id-mismatch
        refusal, same as before — unaffected by the self-reclaim fix."""
        reserve = AsyncMock(return_value=True)
        lifecycle, connector, agent, state_store = _make_lifecycle(
            rules=[_rule()],
            state_by_name={
                "mm-home-general": WatcherState(
                    watcher_name="mm-home-general",
                    session_id="ghost-sess-id",
                    room_id="some-other-chan-id",
                    dynamically_created=True,
                )
            },
            reserve_global_name=reserve,
        )

        result = await lifecycle.try_lazy_create("chan-1")

        self.assertFalse(result)
        reserve.assert_awaited_once_with("mm-home-general")
        connector.subscribe_room.assert_not_called()


class TestTryLazyCreateRenamedRoomRespectsOwnership(unittest.IsolatedAsyncioTestCase):
    """PR #79 review, seventh round, finding #21: a dynamic watcher's
    persisted state (room_id-stable) can be found by the fifth round's
    stable-ID fallback while its WatcherConfig is still absent (never
    reconstructed) — get_watcher_config() returning None there must not be
    read as "nothing to see here" when the room was actually renamed;
    silently building a brand-new config under the new name would abandon
    the old session and bypass a persisted pause."""

    def _old_name_state(self, *, paused: bool) -> WatcherState:
        return WatcherState(
            watcher_name="mm-home-old-name",
            session_id="old-sess-id",
            room_id="chan-1",
            paused=paused,
            dynamically_created=True,
        )

    async def test_renamed_room_reconstructs_old_watcher_and_refuses_new_one(self):
        renamed_room = Room(id="chan-1", name="new-name", type="channel")
        old_state = self._old_name_state(paused=False)
        lifecycle, connector, agent, state_store = _make_lifecycle(
            rules=[_rule()],
            resolved_room=renamed_room,
            state_by_name={"mm-home-old-name": old_state},
        )
        # Simulates what sync_watchers()'s startup preservation logic
        # (round four/six) already seeded into self._states at boot —
        # `_make_lifecycle()` doesn't run sync_watchers(), so it's seeded
        # directly here, same as the fifth round's own rename tests above.
        lifecycle._states["mm-home-old-name"] = old_state

        result = await lifecycle.try_lazy_create("chan-1")

        self.assertFalse(result)
        # Reconstructed under its OLD name, not silently replaced by a new one.
        self.assertIsNotNone(lifecycle.get_watcher_config("mm-home-old-name"))
        self.assertIsNone(lifecycle.get_watcher_config("mm-home-new-name"))
        agent.create_session.assert_not_called()
        connector.subscribe_room.assert_not_called()

    async def test_renamed_paused_watcher_also_reconstructed_and_refused(self):
        renamed_room = Room(id="chan-1", name="new-name", type="channel")
        old_state = self._old_name_state(paused=True)
        lifecycle, connector, agent, state_store = _make_lifecycle(
            rules=[_rule()],
            resolved_room=renamed_room,
            state_by_name={"mm-home-old-name": old_state},
        )
        lifecycle._states["mm-home-old-name"] = old_state

        result = await lifecycle.try_lazy_create("chan-1")

        self.assertFalse(result)
        self.assertIsNotNone(lifecycle.get_watcher_config("mm-home-old-name"))

    async def test_renamed_room_found_via_disk_during_startup_window(self):
        """PR #79 review, tenth round, finding #24: SessionManager.run_once()
        starts the Mattermost websocket (connector.connect()) BEFORE
        sync_watchers(), and sync_watchers() only copies a dormant dynamic
        watcher's persisted state into self._states as its VERY LAST step.
        A message for a renamed dynamic watcher's room arriving in that
        window sees self._states still empty — deliberately NOT seeded
        here, unlike the two tests above — and must fall back to a direct
        disk read instead of silently creating a brand-new watcher/session
        under the new name."""
        renamed_room = Room(id="chan-1", name="new-name", type="channel")
        old_state = self._old_name_state(paused=False)
        lifecycle, connector, agent, state_store = _make_lifecycle(
            rules=[_rule()],
            resolved_room=renamed_room,
            state_by_name={"mm-home-old-name": old_state},
        )
        self.assertEqual(lifecycle._states, {})  # the startup-window condition

        result = await lifecycle.try_lazy_create("chan-1")

        self.assertFalse(result)
        self.assertIsNotNone(lifecycle.get_watcher_config("mm-home-old-name"))
        self.assertIsNone(lifecycle.get_watcher_config("mm-home-new-name"))
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
        # PR #79 review (tenth round, finding #25): preservation is now
        # gated on the connector still having an active wildcard rule —
        # rules=[_rule()] here, not rules=None, to exercise the
        # "still active" case this test is meant to cover.
        lifecycle, connector, agent, state_store = _make_lifecycle(rules=[_rule()])
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

    async def test_dynamically_created_state_is_pruned_once_its_rule_is_removed(self):
        """PR #79 review (tenth round, finding #25): once this connector's
        room: "*" rule is removed entirely, _reconstruct_dynamic_watcher_config()
        explicitly refuses to bring this watcher back (nothing to match
        against) — preserving its state forever anyway would keep it (and
        its generated name, via is_watcher_name_known()) permanently
        unreclaimable garbage. Must be pruned instead, same as any other
        genuinely-orphaned entry."""
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
        self.assertNotIn("mm-home-general", saved_states)


class TestReconstructDynamicWatcherConfig(unittest.IsolatedAsyncioTestCase):
    """PR #79 review, third round: pause_watcher()/resume_watcher()/
    reset_watcher() must be able to reconstruct a dynamically-created
    watcher's WatcherConfig after a restart (never persisted, only its
    WatcherState is, via dynamically_created) — otherwise the one CLI path
    meant to bring a paused lazy watcher back is permanently broken for it."""

    def _post_restart_state(self, paused: bool) -> dict:
        return {
            "mm-home-general": WatcherState(
                watcher_name="mm-home-general",
                session_id="old-sess-id",
                room_id="chan-1",
                paused=paused,
                dynamically_created=True,
            )
        }

    async def test_resume_reconstructs_and_resumes_a_paused_dynamic_watcher(self):
        lifecycle, connector, agent, state_store = _make_lifecycle(
            rules=[_rule()], state_by_name=self._post_restart_state(paused=True),
        )
        self.assertIsNone(lifecycle.get_watcher_config("mm-home-general"))  # gone post-restart

        with patch("gateway.core.watcher_lifecycle.MessageProcessor") as MockProc:
            MockProc.return_value.start = MagicMock()
            await lifecycle.resume_watcher("mm-home-general")

        self.assertIsNotNone(lifecycle.get_watcher_config("mm-home-general"))
        agent.create_session.assert_not_called()  # resumed the old session, not fresh
        connector.subscribe_room.assert_awaited_once()

    async def test_resume_preserves_dynamically_created_marker(self):
        """PR #79 review, fifth round: _start_watcher() used to build a
        brand-new WatcherState with dynamically_created defaulting to
        False, silently un-marking a dynamic watcher the moment it was
        legitimately resumed — so sync_watchers() would drop its state on
        the NEXT restart after all, defeating the fourth-round fix for
        any watcher that's ever been resumed/reset even once."""
        lifecycle, connector, agent, state_store = _make_lifecycle(
            rules=[_rule()], state_by_name=self._post_restart_state(paused=True),
        )

        with patch("gateway.core.watcher_lifecycle.MessageProcessor") as MockProc:
            MockProc.return_value.start = MagicMock()
            await lifecycle.resume_watcher("mm-home-general")

        self.assertTrue(lifecycle._states["mm-home-general"].dynamically_created)

    async def test_reset_reconstructs_and_restarts_a_dynamic_watcher(self):
        lifecycle, connector, agent, state_store = _make_lifecycle(
            rules=[_rule()], state_by_name=self._post_restart_state(paused=False),
        )

        with patch("gateway.core.watcher_lifecycle.MessageProcessor") as MockProc:
            MockProc.return_value.start = MagicMock()
            await lifecycle.reset_watcher("mm-home-general")

        self.assertIsNotNone(lifecycle.get_watcher_config("mm-home-general"))
        connector.subscribe_room.assert_awaited_once()

    async def test_reset_preserves_dynamically_created_marker(self):
        lifecycle, connector, agent, state_store = _make_lifecycle(
            rules=[_rule()], state_by_name=self._post_restart_state(paused=False),
        )

        with patch("gateway.core.watcher_lifecycle.MessageProcessor") as MockProc:
            MockProc.return_value.start = MagicMock()
            await lifecycle.reset_watcher("mm-home-general")

        self.assertTrue(lifecycle._states["mm-home-general"].dynamically_created)

    async def test_pause_reconstructs_a_not_yet_running_dynamic_watcher(self):
        """A user pre-emptively pausing a room's dynamic watcher after
        restart, before its next message would otherwise re-create it."""
        lifecycle, connector, agent, state_store = _make_lifecycle(
            rules=[_rule()], state_by_name=self._post_restart_state(paused=False),
        )

        await lifecycle.pause_watcher("mm-home-general")

        self.assertIsNotNone(lifecycle.get_watcher_config("mm-home-general"))
        self.assertTrue(lifecycle._states["mm-home-general"].paused)

    async def test_genuinely_unknown_watcher_still_raises(self):
        lifecycle, connector, agent, state_store = _make_lifecycle(rules=[_rule()])

        with self.assertRaises(RuntimeError):
            await lifecycle.resume_watcher("no-such-watcher")

    async def test_non_dynamic_persisted_state_is_not_reconstructed(self):
        """A persisted-but-not-dynamically-created entry (e.g. a genuinely
        removed static watcher) must NOT be resurrected via reconstruction —
        only entries explicitly flagged dynamically_created qualify."""
        lifecycle, connector, agent, state_store = _make_lifecycle(
            rules=[_rule()],
            state_by_name={
                "rc-removed-watcher": WatcherState(
                    watcher_name="rc-removed-watcher", session_id="s1", room_id="r1",
                    dynamically_created=False,
                )
            },
        )

        with self.assertRaises(RuntimeError):
            await lifecycle.resume_watcher("rc-removed-watcher")

    async def test_rule_removed_since_creation_is_not_reconstructed(self):
        """If the connector's wildcard rule was since removed entirely, a
        dynamically-created watcher's persisted state has nothing to
        reconstruct against — genuinely orphaned, correctly still raises."""
        lifecycle, connector, agent, state_store = _make_lifecycle(
            rules=None, state_by_name=self._post_restart_state(paused=True),
        )

        with self.assertRaises(RuntimeError):
            await lifecycle.resume_watcher("mm-home-general")

    async def test_room_now_excluded_is_not_reconstructed(self):
        """If the room was added to exclude_room: since this watcher was
        lazily created, reconstruction correctly refuses too."""
        lifecycle, connector, agent, state_store = _make_lifecycle(
            rules=[_rule(exclude_rooms=["general"])],
            state_by_name=self._post_restart_state(paused=True),
        )

        with self.assertRaises(RuntimeError):
            await lifecycle.resume_watcher("mm-home-general")


class TestCanFindOrReconstructWatcher(unittest.IsolatedAsyncioTestCase):
    """Direct coverage of the non-raising probe itself, isolated from
    ControlServer routing (see TestResetRouting in test_control_server.py
    for the routing-level coverage)."""

    async def test_true_for_an_already_known_watcher(self):
        lifecycle, connector, agent, state_store = _make_lifecycle(rules=[_rule()])
        lifecycle._watcher_configs.append(
            WatcherConfig(name="mm-home-general", connector="mm-home", room="general", agent="claude")
        )

        self.assertTrue(await lifecycle.can_find_or_reconstruct_watcher("mm-home-general"))

    async def test_true_for_a_reconstructible_dynamic_watcher(self):
        lifecycle, connector, agent, state_store = _make_lifecycle(
            rules=[_rule()],
            state_by_name={
                "mm-home-general": WatcherState(
                    watcher_name="mm-home-general", session_id="s1", room_id="chan-1",
                    dynamically_created=True,
                )
            },
        )

        result = await lifecycle.can_find_or_reconstruct_watcher("mm-home-general")

        self.assertTrue(result)
        # Side effect: the reconstruction actually happened, so a
        # subsequent plain lookup finds it without reconstructing again.
        self.assertIsNotNone(lifecycle.get_watcher_config("mm-home-general"))

    async def test_false_for_a_genuinely_unknown_watcher(self):
        lifecycle, connector, agent, state_store = _make_lifecycle(rules=[_rule()])

        self.assertFalse(await lifecycle.can_find_or_reconstruct_watcher("no-such-watcher"))


class TestSyncWatchersConcurrentLazyAppend(unittest.IsolatedAsyncioTestCase):
    """PR #79 review, sixth round: sync_watchers()'s startup loop iterates
    self._watcher_configs directly. The Mattermost websocket listen loop is
    already running concurrently by the time sync_watchers() is called
    (SessionManager.run_once() starts it via connector.connect() BEFORE
    calling sync_watchers()) — if a concurrent try_lazy_create() call
    finishes and appends to that SAME list while this loop is still
    awaiting an earlier static watcher's own _start_watcher() call,
    Python's list iterator would eventually revisit and re-start the
    already-fully-started lazy watcher a second time."""

    async def test_concurrently_appended_watcher_is_not_double_started(self):
        config = CoreConfig()
        static_wc = WatcherConfig(name="static-1", connector="mm-home", room="general", agent="claude")
        lazy_wc = WatcherConfig(name="mm-home-dev", connector="mm-home", room="dev", agent="claude")

        connector = AsyncMock()
        connector.agent_username = "hammer-mei"
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
        state_store.load = MagicMock(return_value={})
        state_store.save = MagicMock()
        dispatcher = MagicMock()
        dispatcher.add_processor = MagicMock()
        injector = InjectedContextBuilder(config)
        maps = SessionMaps()

        lifecycle = WatcherLifecycle(
            connector=connector, agents={"claude": agent}, default_agent="claude",
            config=config, watcher_configs=[static_wc], state_store=state_store,
            dispatcher=dispatcher, injector=injector, permission_registry=None, maps=maps,
        )
        lifecycle._attachment_workspace = MagicMock()
        lifecycle._attachment_workspace.setup = MagicMock(return_value="/tmp/fake")

        async def resolve_room_with_concurrent_append(room_name):
            # Simulates the race: WHILE _start_watcher() is awaiting room
            # resolution for the static watcher, a concurrent
            # try_lazy_create() call for a DIFFERENT room finishes and
            # appends its own already-fully-started WatcherConfig to the
            # SAME list this sync_watchers() call is iterating.
            lifecycle._watcher_configs.append(lazy_wc)
            lifecycle._processors[lazy_wc.name] = MagicMock()
            return Room(id="chan-general", name=room_name, type="channel")

        connector.resolve_room = AsyncMock(side_effect=resolve_room_with_concurrent_append)

        with patch("gateway.core.watcher_lifecycle.MessageProcessor") as MockProc:
            MockProc.return_value.start = MagicMock()
            await lifecycle.sync_watchers()

        # The static watcher started normally...
        self.assertIn("static-1", lifecycle._processors)
        # ...but the concurrently-appended lazy watcher must NOT have been
        # revisited/re-started by this same loop — resolve_room() is only
        # ever awaited for "general" (the static watcher), never "dev".
        connector.resolve_room.assert_awaited_once_with("general")


class TestStartWatcherDiscardsMismatchedRoomState(unittest.IsolatedAsyncioTestCase):
    """PR #79 review, sixth round: _start_watcher() must not reuse a
    retained state whose room_id doesn't match the room it just resolved —
    same class of cross-room leak as try_lazy_create()'s own room_id check
    (an earlier review round), but for ANY caller. sync_watchers()'s
    static-watcher startup loop looks up persisted state by NAME alone
    (`persisted.get(wc.name)`) with no room check of its own, so a name
    later reassigned to a different room (config edit, wildcard rule
    removed, sanitized-name collision) would otherwise silently reuse the
    wrong room's session/context."""

    async def test_mismatched_room_id_discards_state_and_creates_fresh_session(self):
        lifecycle, connector, agent, state_store = _make_lifecycle(rules=None)
        connector.resolve_room = AsyncMock(
            return_value=Room(id="chan-1", name="general", type="channel")
        )
        wc = WatcherConfig(name="static-1", connector="mm-home", room="general", agent="claude")
        stale_state = WatcherState(
            watcher_name="static-1",
            session_id="belongs-to-a-different-room",
            room_id="some-other-chan-id",  # NOT "chan-1"
            context_injected=True,
            dynamically_created=True,
        )

        with patch("gateway.core.watcher_lifecycle.MessageProcessor") as MockProc:
            MockProc.return_value.start = MagicMock()
            await lifecycle._start_watcher(wc, stale_state)

        agent.create_session.assert_awaited_once()  # fresh session, not reused
        ws = lifecycle._states["static-1"]
        self.assertNotEqual(ws.session_id, "belongs-to-a-different-room")
        # context_injected ends up True here — NOT because the stale
        # state's flag carried over (it didn't: discarding treats this as
        # a brand-new session, so injection actually ran and legitimately
        # succeeded for it), confirmed by ensure_durable_instructions
        # actually having been called for this fresh session.
        self.assertTrue(ws.context_injected)
        agent.ensure_durable_instructions.assert_awaited_once()
        self.assertFalse(ws.dynamically_created)

    async def test_matching_room_id_state_is_still_reused_normally(self):
        """Sanity check: the new check must not break the legitimate
        resume-a-dormant-session case when the room genuinely matches."""
        lifecycle, connector, agent, state_store = _make_lifecycle(rules=None)
        connector.resolve_room = AsyncMock(
            return_value=Room(id="chan-1", name="general", type="channel")
        )
        wc = WatcherConfig(name="static-1", connector="mm-home", room="general", agent="claude")
        matching_state = WatcherState(
            watcher_name="static-1", session_id="old-sess-id", room_id="chan-1",
        )

        with patch("gateway.core.watcher_lifecycle.MessageProcessor") as MockProc:
            MockProc.return_value.start = MagicMock()
            await lifecycle._start_watcher(wc, matching_state)

        agent.create_session.assert_not_called()
        self.assertEqual(lifecycle._states["static-1"].session_id, "old-sess-id")

    async def test_empty_room_id_state_is_not_treated_as_a_mismatch(self):
        """A state with no room_id yet (e.g. seeded by pause_watcher()'s
        not-found fallback) has nothing to compare against — must not be
        wrongly discarded."""
        lifecycle, connector, agent, state_store = _make_lifecycle(rules=None)
        connector.resolve_room = AsyncMock(
            return_value=Room(id="chan-1", name="general", type="channel")
        )
        wc = WatcherConfig(name="static-1", connector="mm-home", room="general", agent="claude")
        state_no_room_id = WatcherState(
            watcher_name="static-1", session_id="old-sess-id", room_id="",
        )

        with patch("gateway.core.watcher_lifecycle.MessageProcessor") as MockProc:
            MockProc.return_value.start = MagicMock()
            await lifecycle._start_watcher(wc, state_no_room_id)

        agent.create_session.assert_not_called()  # still resumed


if __name__ == "__main__":
    unittest.main()
