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
    make_core_config,
    make_lifecycle,
    make_rc_config,
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
        connector._rest.user_id = "bot-id"
        connector._rest.is_room_member = AsyncMock(return_value=True)
        connector._ws = MagicMock()
        connector._ws.stream_active = False
        connector._ws.subscribe_room = AsyncMock()
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
        import asyncio

        while connector._routing_tasks:
            await asyncio.gather(*connector._routing_tasks)

    async def test_create_idle_wake_resumes_the_same_session(self):
        connector, lifecycle, dispatcher = await self._harness()

        with patch("gateway.core.watcher_lifecycle.MessageProcessor") as MockProc:
            MockProc.return_value.start = MagicMock()

            # 1. Creation, through the real routing episode.
            await connector._on_unrouted_message(_doc("m1", 1500), _ACCESS)

            name = "rc-eng-backend"
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
            lifecycle._processors.pop(name)
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
            name = "rc-eng-backend"
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
            self.assertEqual(created.last_processed_ts, "1500",
                             "the drop captured the live watermark")
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
        # watermark the drop captured.
        connector.replay_room_since.assert_awaited_once()
        self.assertEqual(
            connector.replay_room_since.await_args.kwargs.get("after_ts"), "1500")
        # And however many idle/wake cycles, the bookkeeping does not grow.
        self.assertEqual(connector._room_refcount[ROOM_ID], 1)
        self.assertEqual(len(connector._watcher_contexts[ROOM_ID]), 1)


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
            name = "rc-eng-backend"
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


if __name__ == "__main__":
    unittest.main()
