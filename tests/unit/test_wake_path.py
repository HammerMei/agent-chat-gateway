"""The wake, end to end through the real funnel and the real lifecycle (§2.5).

The connector-level wake tests double the router, and the manager-level
recreation tests double the connector — so neither runs the wake through both
layers at once, and step 3's A1 lesson is that a mocked seam validates the
decision layer perfectly while the seam's *output* corrupts state. Here the
Rocket.Chat connector is real (built by its own constructor, transport and REST
doubled), the manager is real, the lifecycle is real, and the dispatcher is
real; only the agent backend, the message processor and the wire are doubles.

The pass this pins: create through the routing episode → simulate the idle
drop's postcondition (processor gone, dispatcher slot released, record and
connector room state intact) → the next message wakes the room through the
same episode → the *same session* resumes, the frozen record survives, and the
connector's subscription bookkeeping has not grown.
"""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from gateway.core.dispatch import MessageDispatcher, RoomCapacity
from gateway.core.injected_context_builder import InjectedContextBuilder
from gateway.core.room_pattern import RoomPattern
from gateway.core.session_maps import SessionMaps
from gateway.core.watcher_manager import WatcherManager
from gateway.core.watcher_rule import RoomMatcher, WatcherRule
from tests.helpers import (
    MockAgentBackend,
    install_record,
    make_core_config,
    make_lifecycle,
    make_rc_config,
    pop_processor,
)

ROOM_ID = "wake-1"


def _rule():
    return WatcherRule(
        name="eng",
        connector="rc",
        agent="default",
        rooms=RoomMatcher(include=(RoomPattern("eng-*"),)),
    )


def _doc(mid="m1", ts=1500):
    return {"_id": mid, "rid": ROOM_ID, "msg": "hi",
            "u": {"_id": "u9", "username": "alice"}, "ts": {"$date": ts}}


_ACCESS = {"roomParticipant": True, "roomType": "c", "roomName": "eng-backend"}


class TestTheWakeResumesTheSameSession(unittest.IsolatedAsyncioTestCase):

    async def _harness(self):
        from gateway.connectors.rocketchat.connector import RocketChatConnector

        connector = RocketChatConnector(make_rc_config())
        # The wire is the seam being doubled — everything above it is real.
        await connector._rest._client.aclose()
        connector._rest = MagicMock()
        # Pre-login: agent_username falls back to the configured spelling.
        connector._rest.bot_username = None
        connector._rest.user_id = "bot-id"
        connector._rest.is_room_member = AsyncMock(return_value=True)
        connector._ws = MagicMock()
        connector._ws.stream_active = False
        connector._ws.subscribe_room = AsyncMock()
        connector._ws.unsubscribe_room = AsyncMock()
        self.delivered: list[str] = []
        connector._ws.deliver_to_room = MagicMock(
            side_effect=lambda rid, doc, access=None, **kw:
                self.delivered.append(doc["_id"]))
        connector._config.require_mention = False
        connector._config.filter_sender = False
        # REST-backed connector methods the lifecycle calls — wire seams too.
        connector.fetch_room_history = AsyncMock(return_value=[])
        connector.replay_room_since = AsyncMock()

        config = make_core_config()
        dispatcher = MessageDispatcher(connector)
        lifecycle = make_lifecycle(
            connector=connector,
            agents={"default": MockAgentBackend()},
            config=config,
            dispatcher=dispatcher,
            injector=InjectedContextBuilder(config),
            maps=SessionMaps(),
            state_store=MagicMock(load=MagicMock(return_value={}),
                                  save=MagicMock()),
        )
        lifecycle._attachment_workspace = MagicMock(
            setup=MagicMock(return_value="/tmp/fake"))
        manager = WatcherManager("rc", connector, lifecycle, [_rule()])
        # Exposed for the suites that reuse this harness (the membership
        # money test enters through `register_on_join`, not the router).
        self.manager = manager

        # The same wiring `SessionManager` performs, in the same shape.
        async def router(room, trigger):
            await manager.get_or_create(
                "rc", room,
                history_before_ts=connector.trigger_history_bound(trigger))

        connector.register_router(router)
        connector.register_capacity_check(dispatcher.capacity)
        connector.register_handler(AsyncMock(return_value=True))
        return connector, lifecycle, dispatcher

    async def _settle(self, connector):
        from tests.helpers import settle_routing_tasks

        await settle_routing_tasks(connector)

    async def test_create_idle_wake_resumes_the_same_session(self):
        connector, lifecycle, dispatcher = await self._harness()

        with patch("gateway.core.watcher_lifecycle.MessageProcessor") as MockProc:
            MockProc.return_value.start = MagicMock()

            # 1. Creation, through the real routing episode.
            await connector._on_unrouted_message(_doc("m1", 1500), _ACCESS)

            name = "rc:eng-backend"
            created = lifecycle.get_watcher_state(name)
            self.assertIsNotNone(created, "the episode created the watcher")
            session_id = created.session_id
            self.assertTrue(session_id)
            self.assertEqual(self.delivered, ["m1"], "the trigger was delivered")
            self.assertEqual(connector._room_refcount[ROOM_ID], 1)

            # 2. The idle drop's postcondition (§2.2): processor gone, dispatcher
            # slot released — record, session and connector room state intact.
            proc = lifecycle.processor_named(name)
            self.assertIsNotNone(proc)
            pop_processor(lifecycle, name)
            dispatcher.remove_processor(ROOM_ID, proc)
            self.assertIs(dispatcher.capacity(ROOM_ID), RoomCapacity.UNROUTED)
            self.assertIn(ROOM_ID, connector._rooms,
                          "an idle drop does not unsubscribe")
            # Give the record a watermark, so the recreation owes the room a replay.
            created.last_processed_ts = "1400"

            # 3. The wake: the next message takes the *tracked* path.
            handled = await connector._on_raw_ddp_message(ROOM_ID, _doc("m2", 1600))
            await self._settle(connector)
            self.assertTrue(handled)

        # The same session, not a re-mint — and the frozen record survived the
        # round trip (the A1 shape: recreation must not wipe what it reads).
        woken = lifecycle.get_watcher_state(name)
        self.assertEqual(woken.session_id, session_id, "the same session resumed")
        self.assertEqual(woken.rule_name, "eng")
        self.assertTrue(dict(woken.config), "the frozen config snapshot survived")
        self.assertIsNotNone(lifecycle.processor_named(name))
        self.assertIs(dispatcher.capacity(ROOM_ID), RoomCapacity.AVAILABLE)

        # The recreation replayed the interval the room owes, from the record's
        # own watermark.
        connector.replay_room_since.assert_awaited_once()
        self.assertEqual(
            connector.replay_room_since.await_args.kwargs.get("after_ts"), "1400")

        # The trigger came back through the room's worker, after the creation.
        self.assertEqual(self.delivered, ["m1", "m2"])

        # And the wake's re-subscribe did not leak bookkeeping: one refcount,
        # one context, however many idle/wake cycles the room has seen.
        self.assertEqual(connector._room_refcount[ROOM_ID], 1)
        self.assertEqual(len(connector._watcher_contexts[ROOM_ID]), 1)


class TestTheSweepIdlesAndTheNextMessageWakes(unittest.IsolatedAsyncioTestCase):
    """The money test (§2.5): the full idle lifecycle through the real stack.

    Create through the real routing episode → advance the injected clock →
    the sweep drops the room (`dropped_at` set, processor gone, **connector
    room state intact** — that assertion pins §2.2) → the next message wakes
    it → the *same session* resumes, replaying from the watermark the drop
    captured. No layer is doubled between the sweep, the lifecycle, the
    manager and the connector; only the wire, the processor and the agent are.
    """

    # Reuse the wake harness wholesale — same seams, same reasons.
    _harness = TestTheWakeResumesTheSameSession._harness
    _settle = TestTheWakeResumesTheSameSession._settle

    async def test_create_sweep_idle_wake(self):
        from datetime import datetime, timedelta

        from gateway.core.lifecycle_sweep import LifecycleSweep

        connector, lifecycle, dispatcher = await self._harness()
        clock = {"now": datetime.now().astimezone()}
        sweep = LifecycleSweep(lifecycle, now=lambda: clock["now"])

        with patch("gateway.core.watcher_lifecycle.MessageProcessor") as MockProc:
            MockProc.return_value.start = MagicMock()
            MockProc.return_value.stop = AsyncMock()
            MockProc.return_value.has_work_in_flight = False

            # 1. Creation, through the real routing episode.
            await connector._on_unrouted_message(_doc("m1", 1500), _ACCESS)
            name = "rc:eng-backend"
            created = lifecycle.get_watcher_state(name)
            self.assertIsNotNone(created)
            session_id = created.session_id
            # What the room reached before going quiet — the connector's live
            # state the drop must capture and the wake must replay from.
            sub = connector._rooms[ROOM_ID]
            sub.last_processed_ts = "1500"
            sub.remember("m1")

            # 2. Sixteen days pass (rule defaults: 15/15). The sweep drops it.
            clock["now"] += timedelta(days=16)
            self.assertEqual(await sweep.run_once(), [name])

            self.assertTrue(created.dropped_at, "the idle clock was stamped")
            # The drop captures the oldest OWED mark (round 26): the creation
            # drain claimed just-below-m1 (delivery is attempted, a filtered
            # frame is a deferral not a loss), and that claim is open until a
            # replay discharges it — so the durable record carries 1499, and
            # the wake re-reads one page the dedup window absorbs. Older is
            # the safe direction; 1500 here would spend a window nothing read.
            self.assertEqual(created.last_processed_ts, "1499",
                             "the drop captured the owed mark, not merely "
                             "the processed one")
            self.assertIsNone(lifecycle.processor_named(name))
            self.assertIs(dispatcher.capacity(ROOM_ID), RoomCapacity.UNROUTED)
            # §2.2, pinned: the drop does NOT unsubscribe — the room entry,
            # its watermark and its dedup window all survive.
            self.assertIs(connector._rooms.get(ROOM_ID), sub)
            self.assertIn("m1", sub.seen_ids_set)
            self.assertEqual(connector._room_refcount[ROOM_ID], 1)

            # 3. The next message wakes it through the tracked path.
            handled = await connector._on_raw_ddp_message(ROOM_ID, _doc("m2", 1600))
            await self._settle(connector)
            self.assertTrue(handled)

        woken = lifecycle.get_watcher_state(name)
        self.assertEqual(woken.session_id, session_id, "the same session resumed")
        self.assertEqual(woken.dropped_at, "", "no longer idle")
        self.assertIsNotNone(lifecycle.processor_named(name))
        self.assertIs(dispatcher.capacity(ROOM_ID), RoomCapacity.AVAILABLE)
        # The wake replayed the interval the room owes, from the very
        # watermark the drop captured — the OWED mark (round 26).
        connector.replay_room_since.assert_awaited_once()
        self.assertEqual(
            connector.replay_room_since.await_args.kwargs.get("after_ts"), "1499")
        # And however many idle/wake cycles, the bookkeeping does not grow.
        self.assertEqual(connector._room_refcount[ROOM_ID], 1)
        self.assertEqual(len(connector._watcher_contexts[ROOM_ID]), 1)


class TestTheFullLifecycleEndsInAFreshWatcher(unittest.IsolatedAsyncioTestCase):
    """The whole §2.5 arc through the real stack: create → idle → expire →
    the room's next message creates a *fresh* watcher.

    Expiry is the destructive step — after it there is no record, no
    watermark and no session, and the room's connector state is reclaimed
    too, so the next message arrives *untracked* and takes the creation path
    against the current rules with a brand-new session. That last assertion
    is the point of the whole leg: expiry is how a rule edit eventually
    reaches a room (§2.5, "the blunt route").
    """

    _harness = TestTheWakeResumesTheSameSession._harness
    _settle = TestTheWakeResumesTheSameSession._settle

    async def test_create_idle_expire_fresh_create(self):
        from datetime import datetime, timedelta

        from gateway.core.lifecycle_sweep import LifecycleSweep

        connector, lifecycle, dispatcher = await self._harness()
        clock = {"now": datetime.now().astimezone()}
        sweep = LifecycleSweep(lifecycle, now=lambda: clock["now"])

        with patch("gateway.core.watcher_lifecycle.MessageProcessor") as MockProc:
            MockProc.return_value.start = MagicMock()
            MockProc.return_value.stop = AsyncMock()
            MockProc.return_value.has_work_in_flight = False

            # 1. Create, 2. idle — the arc the money test already pins.
            await connector._on_unrouted_message(_doc("m1", 1500), _ACCESS)
            name = "rc:eng-backend"
            first_session = lifecycle.get_watcher_state(name).session_id
            clock["now"] += timedelta(days=16)
            self.assertEqual(await sweep.run_once(), [name])

            # 3. A full expire-TTL after the drop: reclaimed.
            clock["now"] += timedelta(days=15)
            self.assertEqual(await sweep.run_once(), [name])

            self.assertIsNone(lifecycle.get_watcher_state(name),
                              "the record is gone")
            self.assertNotIn(ROOM_ID, connector._rooms,
                             "expiry reclaims the connector's room state too")
            self.assertNotIn(ROOM_ID, connector._room_refcount)

            # 4. The next message arrives UNTRACKED and creates fresh.
            await connector._on_unrouted_message(_doc("m3", 9999), _ACCESS)

        fresh = lifecycle.get_watcher_state(name)
        self.assertIsNotNone(fresh, "the room's next message created a watcher")
        self.assertNotEqual(fresh.session_id, first_session,
                            "a fresh session — expiry deleted the old one")
        self.assertEqual(fresh.dropped_at, "")
        self.assertIs(dispatcher.capacity(ROOM_ID), RoomCapacity.AVAILABLE)
        self.assertEqual(self.delivered[-1], "m3", "the trigger was delivered")


class TestAWakeLandingMidDropWaits(unittest.IsolatedAsyncioTestCase):
    """The teardown/wake race the per-watcher lock closes.

    An idle drop removes the dispatcher slot first and drains last, so a
    message landing mid-drain finds the room UNROUTED and opens a wake — a
    recreation racing the very teardown that made it possible. Without the
    manager taking the lifecycle's per-watcher lock, the recreation runs
    against the state object the teardown is still dismantling, and the
    teardown's last steps remove the session binding the recreation just made
    and stamp `dropped_at` over the recreation's reset. The observable tell is
    exactly that stamp: a woken room that still reads idle.
    """

    _harness = TestTheWakeResumesTheSameSession._harness
    _settle = TestTheWakeResumesTheSameSession._settle

    async def test_the_wake_waits_for_the_teardown_to_settle(self):
        import asyncio
        from datetime import datetime, timedelta

        from gateway.core.lifecycle_sweep import LifecycleSweep

        connector, lifecycle, dispatcher = await self._harness()
        clock = {"now": datetime.now().astimezone()}
        sweep = LifecycleSweep(lifecycle, now=lambda: clock["now"])
        release = asyncio.Event()

        with patch("gateway.core.watcher_lifecycle.MessageProcessor") as MockProc:
            MockProc.return_value.start = MagicMock()
            MockProc.return_value.has_work_in_flight = False

            async def _slow_stop():
                await release.wait()

            MockProc.return_value.stop = AsyncMock(side_effect=_slow_stop)

            await connector._on_unrouted_message(_doc("m1", 1500), _ACCESS)
            name = "rc:eng-backend"
            session_id = lifecycle.get_watcher_state(name).session_id

            clock["now"] += timedelta(days=16)
            drop = asyncio.create_task(sweep.run_once())
            # Let the drop release the dispatcher slot and park in the drain,
            # holding the per-watcher lock.
            for _ in range(10):
                await asyncio.sleep(0)
            self.assertIsNone(lifecycle.processor_named(name),
                              "the drop is mid-teardown")

            # The wake lands exactly there.
            wake = asyncio.create_task(
                connector._on_raw_ddp_message(ROOM_ID, _doc("m2", 1600)))
            for _ in range(10):
                await asyncio.sleep(0)

            release.set()
            self.assertEqual(await drop, [name])
            self.assertTrue(await wake)
            await self._settle(connector)

        woken = lifecycle.get_watcher_state(name)
        self.assertEqual(
            woken.dropped_at, "",
            "the recreation ran after the teardown settled — a dropped_at "
            "surviving here means the teardown stamped over the wake's reset",
        )
        self.assertEqual(woken.session_id, session_id)
        self.assertIsNotNone(lifecycle.processor_named(name))
        self.assertIs(dispatcher.capacity(ROOM_ID), RoomCapacity.AVAILABLE)
        # The tell that actually bites: without the lock, the recreation runs
        # mid-drain and the teardown's LAST step then removes the session
        # binding the recreation just made — a woken watcher whose responses
        # have no room to go to, silently. The binding must have survived.
        self.assertEqual(
            lifecycle._maps.get_room(session_id), ROOM_ID,
            "the teardown removed the session binding the wake just made",
        )


class TestAFailedRecreationKeepsTheRecord(unittest.IsolatedAsyncioTestCase):
    """Codex round 4 (P1): a recreation that fails mid-start used to POP the
    record from `_states` in its rollback — the room was then recordless in
    memory, so the retry took `_create` against the current rules and minted
    a fresh session, silently discarding the frozen binding and watermark
    still sitting on disk. The rollback must restore the prior record, so the
    retry goes back through `_recreate`."""

    _harness = TestTheWakeResumesTheSameSession._harness
    _settle = TestTheWakeResumesTheSameSession._settle

    async def test_the_prior_record_survives_and_the_retry_resumes(self):
        connector, lifecycle, dispatcher = await self._harness()
        name = "rc:eng-backend"

        with patch("gateway.core.watcher_lifecycle.MessageProcessor") as MockProc:
            MockProc.return_value.start = MagicMock()

            # 1. Creation, then the idle drop's postcondition.
            await connector._on_unrouted_message(_doc("m1", 1500), _ACCESS)
            record = lifecycle.get_watcher_state(name)
            session_id = record.session_id
            proc = lifecycle.processor_named(name)
            pop_processor(lifecycle, name)
            dispatcher.remove_processor(ROOM_ID, proc)

            # 2. The wake, with a failure inside the start (context injection
            # — one of the three rollback paths that popped). Raises for as
            # long as it is installed: the routing episode retries within the
            # same wake, and this pin needs the FAILED outcome to observe.
            real_ensure = lifecycle._injector.ensure

            async def failing_ensure(*args, **kwargs):
                raise RuntimeError("injection failure")

            lifecycle._injector.ensure = failing_ensure

            await connector._on_raw_ddp_message(ROOM_ID, _doc("m2", 1600))
            await self._settle(connector)

            # The rollback restored the PRIOR record — the exact object, not
            # a copy and not an empty map.
            self.assertIs(
                lifecycle.get_watcher_state(name), record,
                "the failed recreation must restore the record it replaced — "
                "popping it converts the retry into a first-time _create",
            )
            self.assertIsNone(lifecycle.processor_named(name))

            # 3. The failure clears; the next message must go through
            # _recreate against the restored record.
            lifecycle._injector.ensure = real_ensure
            await connector._on_raw_ddp_message(ROOM_ID, _doc("m3", 1700))
            await self._settle(connector)

        woken = lifecycle.get_watcher_state(name)
        self.assertEqual(woken.session_id, session_id,
                         "the retry RESUMED the frozen session — _recreate, "
                         "not a fresh _create")
        self.assertEqual(woken.rule_name, "eng", "the frozen binding survived")
        self.assertIsNotNone(lifecycle.processor_named(name))


class TestAWakeParkedOnTheLockRespectsShutdown(unittest.IsolatedAsyncioTestCase):
    """TOCTOU sweep after Codex round 4: the disarm check ran only at
    `get_or_create`'s entry. A wake that passed it and then parked on the
    watcher lock could be released BY the shutdown itself (stopping the
    sweep cancels the drop that held the lock) — and because the drop
    mutates the record in place, the staleness fence cannot catch it. The
    start then produced a processor `stop_all` never saw. The disarm is now
    re-checked under both locks."""

    _harness = TestTheWakeResumesTheSameSession._harness
    _settle = TestTheWakeResumesTheSameSession._settle

    async def test_the_late_wake_creates_nothing(self):
        connector, lifecycle, dispatcher = await self._harness()
        manager = self.manager
        name = "rc:eng-backend"

        with patch("gateway.core.watcher_lifecycle.MessageProcessor") as MockProc:
            MockProc.return_value.start = MagicMock()

            await connector._on_unrouted_message(_doc("m1", 1500), _ACCESS)
            proc = lifecycle.processor_named(name)
            pop_processor(lifecycle, name)
            dispatcher.remove_processor(ROOM_ID, proc)

            # The wake, parked on the watcher lock (what the sweep's drop
            # holds mid-teardown).
            lock = lifecycle._get_watcher_lock(name)
            await lock.acquire()
            await connector._on_raw_ddp_message(ROOM_ID, _doc("m2", 1600))
            for _ in range(10):  # let the episode reach the lock
                await asyncio.sleep(0)

            # Shutdown begins while the wake waits; the lock then releases —
            # in production, because the sweep task was cancelled.
            manager.disarm()
            lock.release()
            await self._settle(connector)

        self.assertIsNone(
            lifecycle.processor_named(name),
            "a wake released by the shutdown must not start a processor "
            "stop_all never saw",
        )


class TestDrainWaitsOutInflightStarts(unittest.IsolatedAsyncioTestCase):
    """Codex round 5 (P1): the disarm flag only stops NEW episodes — one
    already inside `start_watcher_in_room` (awaiting session creation)
    installs its processor after `stop_all` snapshots `_processors` and is
    never stopped, its save rewriting state after the final save. `drain()`
    disarms and then waits the in-flight episode out, so the snapshot taken
    after it returns includes the processor."""

    _harness = TestTheWakeResumesTheSameSession._harness
    _settle = TestTheWakeResumesTheSameSession._settle

    async def test_drain_waits_and_the_processor_is_visible_after(self):
        connector, lifecycle, dispatcher = await self._harness()
        manager = self.manager
        gate = asyncio.Event()
        agent = lifecycle._agents["default"]
        real_create = agent.create_session

        async def gated_create(*args, **kwargs):
            await gate.wait()
            return await real_create(*args, **kwargs)

        agent.create_session = gated_create

        with patch("gateway.core.watcher_lifecycle.MessageProcessor") as MockProc:
            MockProc.return_value.start = MagicMock()
            MockProc.return_value.stop = AsyncMock()

            # The episode, in flight: parked inside start_watcher_in_room on
            # the gated session creation. As a task, because the unrouted
            # path awaits creation inline.
            episode = asyncio.create_task(
                connector._on_unrouted_message(_doc("m1", 1500), _ACCESS))
            for _ in range(10):
                await asyncio.sleep(0)

            drain_task = asyncio.create_task(manager.drain())
            for _ in range(10):
                await asyncio.sleep(0)
            self.assertFalse(drain_task.done(),
                             "drain must wait for the in-flight start")

            gate.set()
            await drain_task
            await episode
            await self._settle(connector)

        self.assertIsNotNone(
            lifecycle.processor_named("rc:eng-backend"),
            "the episode finished BEFORE drain returned — its processor is "
            "inside the snapshot stop_all takes next",
        )
        self.assertTrue(manager.disarmed, "drain disarms first")


class TestARuleLessManagerStillWakesRecords(unittest.IsolatedAsyncioTestCase):
    """Codex round 5 (P1): the manager used to exist only when rules did — a
    pre-cutover gate protecting static-only deployments, which no longer
    load. Its post-cutover victim: removing a connector's LAST rule left its
    hydrated rule-derived records with no router, no boot recreation and no
    replay — every existing session unreachable, despite §2.4 keeping the
    records until expiry. The manager now always exists; with an empty rule
    list it recreates persisted records and declines genuinely new rooms."""

    _harness = TestTheWakeResumesTheSameSession._harness
    _settle = TestTheWakeResumesTheSameSession._settle

    async def test_the_session_manager_constructs_it_unconditionally(self):
        from tests.helpers import make_manager

        mgr = make_manager(watcher_rules=[])
        self.assertIsNotNone(mgr._watcher_manager,
                             "no rules is not no manager — persisted records "
                             "still need recreation, replay and the sweep")

    async def test_a_persisted_record_wakes_under_an_empty_rule_list(self):
        connector, lifecycle, dispatcher = await self._harness()
        name = "rc:eng-backend"

        with patch("gateway.core.watcher_lifecycle.MessageProcessor") as MockProc:
            MockProc.return_value.start = MagicMock()

            # Created while the rule existed.
            await connector._on_unrouted_message(_doc("m1", 1500), _ACCESS)
            record = lifecycle.get_watcher_state(name)
            session_id = record.session_id
            proc = lifecycle.processor_named(name)
            pop_processor(lifecycle, name)
            dispatcher.remove_processor(ROOM_ID, proc)

            # The operator removes the last rule and restarts, in miniature:
            # a manager with NO current rules over the same lifecycle.
            ruleless = WatcherManager("rc", connector, lifecycle, [])

            async def router(room, trigger):
                await ruleless.get_or_create(
                    "rc", room,
                    history_before_ts=connector.trigger_history_bound(trigger))

            connector.register_router(router)

            await connector._on_raw_ddp_message(ROOM_ID, _doc("m2", 1600))
            await self._settle(connector)

        woken = lifecycle.get_watcher_state(name)
        self.assertEqual(woken.session_id, session_id,
                         "sticky binding: the record is the recreation "
                         "source — current rules are never consulted")
        self.assertIsNotNone(lifecycle.processor_named(name))


class TestAWakePreservesTheRecordsRoomName(unittest.IsolatedAsyncioTestCase):
    """Codex round 6: a wake from tracked state offers a ref with NO
    participants, and deriving the platform room's name from that ref
    degraded a DM's description to the dm-/gdm-digest — overwriting the
    meaningful room_name the creation wrote. §2.4: the record is the source."""

    _harness = TestTheWakeResumesTheSameSession._harness
    _settle = TestTheWakeResumesTheSameSession._settle

    async def test_recreate_names_the_room_from_the_record(self):
        from gateway.core.watcher_manager import RoomRef
        from gateway.core.watcher_rule import RoomKind
        from tests.helpers import make_rule_derived_record

        connector, lifecycle, dispatcher = await self._harness()
        manager = self.manager
        record = make_rule_derived_record(
            name="rc-gdm-1", room_id="gdm-1",
            room_name="alice, bob", room_kind="group_dm",
        )
        install_record(lifecycle, record, as_name="rc-gdm-1")

        # The wake's offered ref: no participants — the tracked channel
        # state does not retain them.
        ref = RoomRef(id="gdm-1", kind=RoomKind.GROUP_DM, name="",
                      participants=())

        with patch.object(lifecycle, "start_watcher_in_room",
                          new=AsyncMock()) as start:
            await manager._recreate(record, ref, None)

        platform_room = start.await_args.args[2]
        self.assertEqual(platform_room.name, "alice, bob",
                         "the record's own name, not the digest fallback "
                         "derived from a participant-less ref")
        self.assertEqual(platform_room.type, "group_dm")


if __name__ == "__main__":
    unittest.main()
