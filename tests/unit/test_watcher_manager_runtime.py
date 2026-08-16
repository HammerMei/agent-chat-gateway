"""The runtime half of the WatcherManager (§2.7, §2.8): the creation decision.

The lifecycle owns the start machinery and is mocked at the seam these tests pin
(`record_for_room` / `processor_named` / `start_watcher_in_room` / `get_watcher_state`
/ `save_state`); one end-to-end class runs the real lifecycle to catch the seam
itself drifting.

What the mocked half asserts is the *decision layer*: sticky binding beats rule
matching, a pause is a deliberate drop, single-flight covers check-plus-create,
the cap answers audibly, and the §5.3 record fields are frozen at creation.
"""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock

from gateway.core.config import HistoryHandoffConfig
from gateway.core.room_pattern import RoomPattern
from gateway.core.state import CONFIG_SCHEMA_VERSION, WatcherState
from gateway.core.watcher_manager import (
    RoomRef,
    WatcherManager,
    config_from_record,
    materialize,
    rule_snapshot,
)
from gateway.core.watcher_rule import RoomKind, RoomMatcher, WatcherRule


def _rule(name="eng", connector="rc", agent="claude", include=("eng-*",),
          except_for=(), direct=False, group_direct=False, **kwargs):
    return WatcherRule(
        name=name,
        connector=connector,
        agent=agent,
        rooms=RoomMatcher(
            include=tuple(RoomPattern(p) for p in include),
            except_for=tuple(RoomPattern(p) for p in except_for),
            direct=direct,
            group_direct=group_direct,
        ),
        **kwargs,
    )


def _room(id="r1", kind=RoomKind.CHANNEL, name="eng-backend", participants=()):
    return RoomRef(id=id, kind=kind, name=name, participants=participants)


def _mock_lifecycle():
    lifecycle = MagicMock()
    lifecycle.record_for_room = MagicMock(return_value=None)
    lifecycle.processor_named = MagicMock(return_value=None)
    lifecycle.resolve_agent_name = MagicMock(side_effect=lambda ref: ref or "default")
    lifecycle.start_watcher_in_room = AsyncMock()
    lifecycle.get_watcher_state = MagicMock(return_value=None)
    lifecycle.save_state = MagicMock()
    return lifecycle


def _manager(lifecycle=None, rules=None, connector=None, **kwargs):
    lifecycle = lifecycle if lifecycle is not None else _mock_lifecycle()
    connector = connector if connector is not None else MagicMock(
        send_to_room=AsyncMock())
    manager = WatcherManager(
        "rc", connector, lifecycle,
        rules if rules is not None else [_rule()], **kwargs,
    )
    return manager, lifecycle, connector


class TestCreation(unittest.IsolatedAsyncioTestCase):
    async def test_a_matching_rule_creates_a_watcher(self):
        manager, lifecycle, _ = _manager()
        started = {}

        async def record_start(wc, state, room, history_before_ts=None):
            started["wc"] = wc
            started["room"] = room
            started["history_before_ts"] = history_before_ts
            ws = WatcherState(watcher_name=wc.name, session_id="s1", room_id=room.id)
            lifecycle.get_watcher_state = MagicMock(return_value=ws)
            started["ws"] = ws
            lifecycle.processor_named = MagicMock(return_value="the-processor")

        lifecycle.start_watcher_in_room = AsyncMock(side_effect=record_start)

        result = await manager.get_or_create("rc", _room())

        self.assertEqual(result, "the-processor")
        self.assertEqual(started["wc"].name, "rc-eng-backend")
        self.assertIsNone(started["history_before_ts"])
        # The state passed for a first-ever room is None — there is no record.
        self.assertIsNone(lifecycle.start_watcher_in_room.call_args.args[1])

    async def test_no_matching_rule_creates_nothing(self):
        manager, lifecycle, _ = _manager(rules=[_rule(include=("ops-*",))])
        result = await manager.get_or_create("rc", _room())
        self.assertIsNone(result)
        lifecycle.start_watcher_in_room.assert_not_called()

    async def test_the_trigger_timestamp_reaches_the_start(self):
        manager, lifecycle, _ = _manager()
        await manager.get_or_create(
            "rc", _room(), history_before_ts="2026-08-16T10:00:00+00:00")
        self.assertEqual(
            lifecycle.start_watcher_in_room.call_args.kwargs["history_before_ts"],
            "2026-08-16T10:00:00+00:00",
        )

    async def test_a_group_dm_room_is_typed_group_dm(self):
        """The platform Room the watcher subscribes with carries the classified
        kind, which is what puts a group DM on the mention-required side of the
        gate (§6.4) — RC's own resolver cannot tell the two DM kinds apart."""
        manager, lifecycle, _ = _manager(rules=[_rule(include=(), group_direct=True)])
        await manager.get_or_create(
            "rc", _room(id="g1", kind=RoomKind.GROUP_DM, name="",
                        participants=("alice", "bob")))
        platform_room = lifecycle.start_watcher_in_room.call_args.args[2]
        self.assertEqual(platform_room.type, "group_dm")
        self.assertEqual(platform_room.id, "g1")
        self.assertEqual(platform_room.name, "alice, bob")

    async def test_a_failed_creation_answers_none_and_is_retryable(self):
        manager, lifecycle, _ = _manager()
        lifecycle.start_watcher_in_room = AsyncMock(side_effect=RuntimeError("boom"))

        result = await manager.get_or_create("rc", _room())
        self.assertIsNone(result)
        lifecycle.save_state.assert_not_called()

        # The next message tries again — the failure held no reservation.
        lifecycle.start_watcher_in_room = AsyncMock()
        await manager.get_or_create("rc", _room())
        lifecycle.start_watcher_in_room.assert_called_once()

    async def test_a_wrong_connector_is_refused(self):
        manager, lifecycle, _ = _manager()
        result = await manager.get_or_create("mm", _room())
        self.assertIsNone(result)
        lifecycle.start_watcher_in_room.assert_not_called()


class TestTheRecordIsFrozenAtCreation(unittest.IsolatedAsyncioTestCase):
    """§5.3's on-the-fly fields exist so the record is self-sufficient: recreation
    reads `config`, drift detection reads `rule`, and neither consults the live
    config. All of them are known only at the creation moment."""

    async def test_every_creation_only_field_is_populated(self):
        rule = _rule()
        manager, lifecycle, _ = _manager(rules=[rule])
        room = _room()
        ws = WatcherState(watcher_name="rc-eng-backend", session_id="s1", room_id="r1")
        lifecycle.get_watcher_state = MagicMock(return_value=ws)

        await manager.get_or_create("rc", room)

        self.assertEqual(ws.room_kind, "channel")
        self.assertEqual(ws.connector, "rc")
        self.assertEqual(ws.agent, "claude")
        self.assertEqual(ws.rule_name, "eng")
        self.assertEqual(ws.rule, rule_snapshot(rule))
        self.assertEqual(ws.config_schema_version, CONFIG_SCHEMA_VERSION)
        self.assertTrue(ws.created_at)
        self.assertEqual(ws.created_at, ws.last_activity_at)
        # The frozen config round-trips into the exact WatcherConfig recreation needs.
        self.assertEqual(config_from_record(ws), materialize(rule, room))
        lifecycle.save_state.assert_called_once()

    async def test_participants_are_kept_for_a_group_dm(self):
        manager, lifecycle, _ = _manager(rules=[_rule(include=(), group_direct=True)])
        ws = WatcherState(watcher_name="w", session_id="s1", room_id="g1")
        lifecycle.get_watcher_state = MagicMock(return_value=ws)

        await manager.get_or_create(
            "rc", _room(id="g1", kind=RoomKind.GROUP_DM, name="",
                        participants=("alice", "bob")))

        self.assertEqual(ws.participants, ["alice", "bob"])
        self.assertEqual(ws.room_kind, "group_dm")


class TestStickyBinding(unittest.IsolatedAsyncioTestCase):
    """A room with a record is recreated from its own persisted config (§2.4);
    the current rules are never consulted."""

    def _record(self, paused=False, config=None):
        return WatcherState(
            watcher_name="rc-eng-backend", session_id="s1", room_id="r1",
            paused=paused,
            config=config if config is not None
            else {"name": "rc-eng-backend", "connector": "rc",
                  "room": "eng-backend", "agent": "claude",
                  "context_inject_files": [], "online_notification": None,
                  "offline_notification": None,
                  "history_handoff": {"enabled": True, "fetch_count": 50,
                                      "verbatim_tail": 15, "max_fetch_count": 200}},
        )

    async def test_a_resident_watcher_is_returned_not_recreated(self):
        manager, lifecycle, _ = _manager()
        lifecycle.record_for_room = MagicMock(return_value=self._record())
        lifecycle.processor_named = MagicMock(return_value="resident")

        result = await manager.get_or_create("rc", _room())

        self.assertEqual(result, "resident")
        lifecycle.start_watcher_in_room.assert_not_called()

    async def test_an_idle_record_is_recreated_from_its_config_not_the_rules(self):
        # The rule list would DENY this room — sticky binding must win.
        manager, lifecycle, _ = _manager(rules=[])
        record = self._record()
        lifecycle.record_for_room = MagicMock(return_value=record)

        await manager.get_or_create("rc", _room())

        lifecycle.start_watcher_in_room.assert_called_once()
        wc = lifecycle.start_watcher_in_room.call_args.args[0]
        self.assertEqual(wc.name, "rc-eng-backend")
        self.assertEqual(wc.agent, "claude")
        # The record itself rides along, so the session is resumed, not re-minted.
        self.assertIs(lifecycle.start_watcher_in_room.call_args.args[1], record)

    async def test_a_paused_record_is_a_deliberate_drop(self):
        """§4.4: an explicit pause is never overridden by inference. A message
        for a paused room creates nothing and unpauses nothing."""
        manager, lifecycle, _ = _manager()
        lifecycle.record_for_room = MagicMock(return_value=self._record(paused=True))

        result = await manager.get_or_create("rc", _room())

        self.assertIsNone(result)
        lifecycle.start_watcher_in_room.assert_not_called()

    async def test_a_record_with_no_frozen_config_is_left_to_the_static_path(self):
        manager, lifecycle, _ = _manager()
        lifecycle.record_for_room = MagicMock(return_value=self._record(config={}))

        result = await manager.get_or_create("rc", _room())

        self.assertIsNone(result)
        lifecycle.start_watcher_in_room.assert_not_called()


class TestSingleFlight(unittest.IsolatedAsyncioTestCase):
    async def test_two_concurrent_offers_for_one_room_create_once(self):
        """§2.7 step 4: the lock covers the existence check and the creation
        together. The loser of the race must observe the winner's watcher, not
        start a second creation."""
        manager, lifecycle, _ = _manager()
        release = asyncio.Event()
        starts = []

        async def slow_start(wc, state, room, history_before_ts=None):
            starts.append(wc.name)
            await release.wait()
            ws = WatcherState(watcher_name=wc.name, session_id="s", room_id=room.id)
            lifecycle.get_watcher_state = MagicMock(return_value=ws)
            # What a finished creation looks like to the second caller:
            lifecycle.record_for_room = MagicMock(return_value=ws)
            lifecycle.processor_named = MagicMock(return_value="the-processor")

        lifecycle.start_watcher_in_room = AsyncMock(side_effect=slow_start)

        first = asyncio.create_task(manager.get_or_create("rc", _room()))
        second = asyncio.create_task(manager.get_or_create("rc", _room()))
        await asyncio.sleep(0)  # both tasks reach the lock
        release.set()
        results = await asyncio.gather(first, second)

        self.assertEqual(starts, ["rc-eng-backend"], "exactly one creation ran")
        self.assertEqual(results[1], "the-processor")

    async def test_two_different_rooms_create_concurrently(self):
        """The lock is per room — one room's slow creation must not serialize
        every other room behind it."""
        manager, lifecycle, _ = _manager(
            rules=[_rule(include=("eng-*",))])
        gate_a = asyncio.Event()
        in_start = asyncio.Event()

        async def start(wc, state, room, history_before_ts=None):
            if room.id == "ra":
                in_start.set()
                await gate_a.wait()

        lifecycle.start_watcher_in_room = AsyncMock(side_effect=start)

        task_a = asyncio.create_task(
            manager.get_or_create("rc", _room(id="ra", name="eng-a")))
        await in_start.wait()
        # Room B completes while room A is still inside its creation.
        await asyncio.wait_for(
            manager.get_or_create("rc", _room(id="rb", name="eng-b")), timeout=1)
        gate_a.set()
        await task_a


class TestTheCreationCap(unittest.IsolatedAsyncioTestCase):
    async def test_over_cap_answers_with_a_visible_notice_not_a_silent_drop(self):
        manager, lifecycle, connector = _manager(creation_cap=1)
        release = asyncio.Event()

        async def slow_start(wc, state, room, history_before_ts=None):
            await release.wait()

        lifecycle.start_watcher_in_room = AsyncMock(side_effect=slow_start)

        first = asyncio.create_task(
            manager.get_or_create("rc", _room(id="ra", name="eng-a")))
        await asyncio.sleep(0)

        result = await manager.get_or_create("rc", _room(id="rb", name="eng-b"))

        self.assertIsNone(result)
        connector.send_to_room.assert_awaited_once()
        self.assertEqual(connector.send_to_room.await_args.args[0], "rb")

        release.set()
        await first

    async def test_a_failed_notice_does_not_raise_out_of_routing(self):
        manager, lifecycle, connector = _manager(creation_cap=0)
        connector.send_to_room = AsyncMock(side_effect=RuntimeError("rest down"))

        result = await manager.get_or_create("rc", _room())
        self.assertIsNone(result)

    async def test_the_cap_releases_when_a_creation_finishes(self):
        manager, lifecycle, _ = _manager(creation_cap=1)
        await manager.get_or_create("rc", _room(id="ra", name="eng-a"))
        await manager.get_or_create("rc", _room(id="rb", name="eng-b"))
        self.assertEqual(lifecycle.start_watcher_in_room.await_count, 2)


class TestConfigFromRecord(unittest.TestCase):
    def test_round_trips_a_materialized_config(self):
        rule = _rule()
        wc = materialize(rule, _room())
        from gateway.core.watcher_manager import _jsonable

        record = WatcherState(
            watcher_name=wc.name, session_id="s", room_id="r1", config=_jsonable(wc))
        self.assertEqual(config_from_record(record), wc)

    def test_an_empty_config_is_not_recreatable(self):
        record = WatcherState(watcher_name="w", session_id="s", room_id="r1")
        self.assertIsNone(config_from_record(record))

    def test_a_nameless_config_is_not_recreatable(self):
        record = WatcherState(
            watcher_name="w", session_id="s", room_id="r1",
            config={"connector": "rc"})
        self.assertIsNone(config_from_record(record))

    def test_wrongly_typed_handoff_values_fall_back_to_defaults(self):
        record = WatcherState(
            watcher_name="w", session_id="s", room_id="r1",
            config={"name": "w", "connector": "rc", "room": "x", "agent": "a",
                    "history_handoff": {"enabled": "yes", "fetch_count": "many"}})
        wc = config_from_record(record)
        self.assertEqual(wc.history_handoff, HistoryHandoffConfig())


class TestEndToEndThroughTheRealLifecycle(unittest.IsolatedAsyncioTestCase):
    """One pass with nothing mocked between the manager and the lifecycle, so a
    drift in the seam's signature or in what `_start_watcher` builds fails here
    rather than in production. The connector and agent are still doubles."""

    def _real_lifecycle(self):
        from unittest.mock import patch as _patch

        from gateway.core.dispatch import MessageDispatcher
        from gateway.core.injected_context_builder import InjectedContextBuilder
        from gateway.core.session_maps import SessionMaps
        from tests.helpers import MockAgentBackend, make_core_config, make_lifecycle

        connector = MagicMock()
        connector.agent_username = "hammer-mei"
        connector.subscribe_room = AsyncMock()
        connector.unsubscribe_room = AsyncMock()
        connector.fetch_room_history = AsyncMock(return_value=[])
        connector.get_last_processed_ts = MagicMock(return_value=None)
        connector.update_last_processed_ts = MagicMock()
        connector.attachment_cache_dir = MagicMock(return_value=None)
        connector.send_to_room = AsyncMock()

        config = make_core_config()
        lifecycle = make_lifecycle(
            connector=connector,
            agents={"default": MockAgentBackend()},
            config=config,
            dispatcher=MessageDispatcher(connector),
            injector=InjectedContextBuilder(config),
            maps=SessionMaps(),
            state_store=MagicMock(load=MagicMock(return_value={}),
                                  save=MagicMock()),
        )
        lifecycle._attachment_workspace = MagicMock(
            setup=MagicMock(return_value="/tmp/fake"))
        return lifecycle, connector, _patch

    async def test_a_creation_runs_the_real_start_and_freezes_a_real_record(self):
        lifecycle, connector, _patch = self._real_lifecycle()
        rule = _rule(agent="default")
        manager = WatcherManager("rc", connector, lifecycle, [rule])
        room = _room()

        with _patch("gateway.core.watcher_lifecycle.MessageProcessor") as MockProc:
            MockProc.return_value.start = MagicMock()
            result = await manager.get_or_create(
                "rc", room, history_before_ts="2026-08-16T10:00:00+00:00")

        self.assertIsNotNone(result)
        ws = lifecycle.get_watcher_state("rc-eng-backend")
        self.assertEqual(ws.room_id, "r1")
        self.assertEqual(ws.rule_name, "eng")
        self.assertEqual(config_from_record(ws), materialize(rule, room))
        self.assertTrue(ws.session_id, "a session was provisioned")
        # The trigger bound reached the real history fetch.
        self.assertEqual(
            connector.fetch_room_history.call_args.kwargs.get("before_ts"),
            "2026-08-16T10:00:00+00:00",
        )
        # And the record is queryable through the sticky-binding lookup.
        self.assertIs(lifecycle.record_for_room("r1"), ws)

    async def test_a_second_message_reuses_the_watcher_it_created(self):
        lifecycle, connector, _patch = self._real_lifecycle()
        manager = WatcherManager("rc", connector, lifecycle, [_rule(agent="default")])

        with _patch("gateway.core.watcher_lifecycle.MessageProcessor") as MockProc:
            MockProc.return_value.start = MagicMock()
            first = await manager.get_or_create("rc", _room())
            second = await manager.get_or_create("rc", _room())

        self.assertIs(first, second, "one watcher, not two")
        connector.subscribe_room.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
