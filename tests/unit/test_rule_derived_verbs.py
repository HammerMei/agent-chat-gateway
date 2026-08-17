"""The operator verbs learn rule-derived records (§2.8, step 7a).

pause/resume/reset dispatch on the record: a name whose record carries a
materialized config takes the record path; a static name keeps the config
path byte-identical until the cutover deletes it. What these pin, in the
design's words: pause acts on a record and an unseen room has none (§4.4);
resume restarts the clock — the one deliberate exception to "a recreation
carries the clock" (§2.5); reset must not silently clear paused (§2.5); and
the expire verb is the forced-reclamation path with the operator watching.

The resume/reset/race tests run the wake suite's real harness — connector,
manager, lifecycle and dispatcher all real — because the defect this layer
can produce (a resume that wipes the frozen snapshots) is invisible to a
mocked seam (the A1 lesson).
"""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from tests.helpers import make_rule_derived_record
from tests.unit.test_wake_path import (
    _ACCESS,
    _doc,
)
from tests.unit.test_wake_path import (
    TestTheWakeResumesTheSameSession as _WakeSuite,
)

ROOM_ID = "wake-1"
NAME = "rc-eng-backend"


class TestVerbsOnRuleDerivedRecords(unittest.IsolatedAsyncioTestCase):

    _harness = _WakeSuite._harness
    _settle = _WakeSuite._settle

    async def _create(self, connector, lifecycle):
        """A watcher created through the real routing episode."""
        await connector._on_unrouted_message(_doc("m1", 1500), _ACCESS)
        record = lifecycle.get_watcher_state(NAME)
        self.assertIsNotNone(record)
        return record

    async def test_pause_learns_the_record(self):
        connector, lifecycle, dispatcher = await self._harness()
        with patch("gateway.core.watcher_lifecycle.MessageProcessor") as MockProc:
            MockProc.return_value.start = MagicMock()
            MockProc.return_value.stop = AsyncMock()
            record = await self._create(connector, lifecycle)

            await lifecycle.pause_watcher(NAME)

        state = lifecycle.get_watcher_state(NAME)
        self.assertIs(state, record, "the record is mutated, never replaced")
        self.assertTrue(state.paused)
        self.assertIsNone(lifecycle.processor_named(NAME))
        # The frozen snapshots are untouched by a pause.
        self.assertEqual(state.rule_name, "eng")
        self.assertTrue(dict(state.config))

    async def test_pause_on_an_unseen_room_is_rejected_not_fabricated(self):
        """§4.4: pause acts on a record, and an unobserved room has none.
        Fabricating a blank record here is #118's defect 1 — the rejection
        must point at `rooms.except_for`, the durable alternative."""
        _, lifecycle, _ = await self._harness()

        with self.assertRaises(RuntimeError) as ctx:
            await lifecycle.pause_watcher("rc-never-seen")

        self.assertIn("except_for", str(ctx.exception))
        self.assertIsNone(lifecycle.get_watcher_state("rc-never-seen"),
                          "no blank record was fabricated")

    async def test_resume_restarts_the_clock_and_keeps_the_snapshots(self):
        """The owed test (§2.5): `last_activity_at` is stamped at the moment
        of resume — a watcher paused longer than its idle TTL must not be
        re-idled by the next sweep pass — while everything frozen at creation
        survives the fresh WatcherState the start constructs."""
        connector, lifecycle, dispatcher = await self._harness()
        with patch("gateway.core.watcher_lifecycle.MessageProcessor") as MockProc:
            MockProc.return_value.start = MagicMock()
            MockProc.return_value.stop = AsyncMock()
            record = await self._create(connector, lifecycle)
            session_id = record.session_id
            created_at = record.created_at

            await lifecycle.pause_watcher(NAME)
            # A pause long enough that a carried clock would idle instantly.
            paused = lifecycle.get_watcher_state(NAME)
            paused.last_activity_at = "2020-01-01T00:00:00+00:00"
            paused.dropped_at = "2020-01-02T00:00:00+00:00"

            await lifecycle.resume_watcher(NAME)

        resumed = lifecycle.get_watcher_state(NAME)
        self.assertFalse(resumed.paused)
        self.assertEqual(resumed.session_id, session_id, "the same session resumed")
        self.assertEqual(resumed.rule_name, "eng", "the frozen rule survived")
        self.assertTrue(dict(resumed.config), "the frozen config survived")
        self.assertEqual(resumed.created_at, created_at)
        self.assertEqual(resumed.dropped_at, "", "resume returns it to active")
        self.assertNotEqual(resumed.last_activity_at, "2020-01-01T00:00:00+00:00",
                            "resume RESTARTS the clock (§2.5) — the deliberate "
                            "exception to 'a recreation carries the clock'")
        self.assertIsNotNone(lifecycle.processor_named(NAME))
        # Messages that arrived while paused were deliberately dropped, not
        # deferred (§4.4) — a resume must not replay the muted interval.
        connector.replay_room_since.assert_not_awaited()

    async def test_reset_refuses_a_paused_record(self):
        """§2.5: reset must not silently clear `paused` — the operator's one
        durable mute must not be erased as a side effect of session hygiene."""
        connector, lifecycle, _ = await self._harness()
        with patch("gateway.core.watcher_lifecycle.MessageProcessor") as MockProc:
            MockProc.return_value.start = MagicMock()
            MockProc.return_value.stop = AsyncMock()
            await self._create(connector, lifecycle)
            await lifecycle.pause_watcher(NAME)

            with self.assertRaises(RuntimeError) as ctx:
                await lifecycle.reset_watcher(NAME)

        self.assertIn("resume", str(ctx.exception).lower())
        self.assertTrue(lifecycle.get_watcher_state(NAME).paused,
                        "the pause survived the refused reset")

    async def test_reset_mints_a_fresh_session_and_keeps_the_snapshots(self):
        connector, lifecycle, _ = await self._harness()
        with patch("gateway.core.watcher_lifecycle.MessageProcessor") as MockProc:
            MockProc.return_value.start = MagicMock()
            MockProc.return_value.stop = AsyncMock()
            record = await self._create(connector, lifecycle)
            old_session = record.session_id

            await lifecycle.reset_watcher(NAME)

        reset = lifecycle.get_watcher_state(NAME)
        self.assertTrue(reset.session_id)
        self.assertNotEqual(reset.session_id, old_session, "a fresh session")
        self.assertEqual(reset.rule_name, "eng", "the frozen rule survived")
        self.assertTrue(dict(reset.config), "the frozen config survived")
        self.assertTrue(reset.last_activity_at, "reset stamps the clock too")

    async def test_pause_wins_the_race_against_a_wake(self):
        """The owed race test: a wake dispatched against a record whose pause
        is mid-teardown must wait on the watcher lock and then decline — the
        re-read under the lock is what makes §4.4 hold against a message
        arriving in the pause's own drain window."""
        connector, lifecycle, dispatcher = await self._harness()
        stop_gate = asyncio.Event()
        with patch("gateway.core.watcher_lifecycle.MessageProcessor") as MockProc:
            MockProc.return_value.start = MagicMock()

            async def slow_stop():
                await stop_gate.wait()

            MockProc.return_value.stop = AsyncMock(side_effect=slow_stop)
            await self._create(connector, lifecycle)

            pause_task = asyncio.create_task(lifecycle.pause_watcher(NAME))
            # Let the pause reach the gated stop: the processor is already
            # popped, so a wake sees a non-resident record and dispatches
            # _recreate — which must block on the watcher lock the pause holds.
            for _ in range(50):
                await asyncio.sleep(0)
                if NAME not in lifecycle._processors:
                    break

            wake_task = asyncio.create_task(
                self.manager.get_or_create("rc", _wake_room()))
            await asyncio.sleep(0.01)
            self.assertFalse(wake_task.done(),
                             "the wake waits on the pause's watcher lock")

            stop_gate.set()
            await pause_task
            woken = await wake_task

        self.assertIsNone(woken, "the wake re-read the record under the lock "
                                 "and declined the now-paused room")
        self.assertTrue(lifecycle.get_watcher_state(NAME).paused)
        self.assertIsNone(lifecycle.processor_named(NAME))


def _wake_room():
    from gateway.core.watcher_manager import RoomRef
    from gateway.core.watcher_rule import RoomKind

    return RoomRef(id=ROOM_ID, kind=RoomKind.CHANNEL, name="eng-backend")


class TestTheExpireVerb(unittest.IsolatedAsyncioTestCase):

    def _mgr(self, record, *, reclaimed="w1", cancelled=None):
        from tests.helpers import make_bare_session_manager

        mgr = make_bare_session_manager(
            _cancel_jobs=cancelled.append if cancelled is not None else None)
        mgr._lifecycle.get_watcher_state = MagicMock(return_value=record)
        mgr._lifecycle.reclaim_room = AsyncMock(return_value=reclaimed)
        return mgr

    async def test_expire_reclaims_and_cancels_jobs(self):
        record = make_rule_derived_record(name="w1")
        cancelled = []
        mgr = self._mgr(record, cancelled=cancelled)

        await mgr.expire_watcher("w1")

        mgr._lifecycle.reclaim_room.assert_awaited_once()
        args = mgr._lifecycle.reclaim_room.call_args
        self.assertEqual(args.args[0], "room-w1")
        self.assertIn("expire", args.kwargs["reason"])
        self.assertEqual(cancelled, ["w1"])

    async def test_expire_raises_where_the_event_handlers_swallow(self):
        """An operator watching the command must see the failure."""
        mgr = self._mgr(None)
        with self.assertRaises(RuntimeError):
            await mgr.expire_watcher("ghost")

        static = make_rule_derived_record(name="s1", config={})
        mgr = self._mgr(static)
        with self.assertRaises(RuntimeError):
            await mgr.expire_watcher("s1")

        record = make_rule_derived_record(name="w1")
        mgr = self._mgr(record, reclaimed=None)
        with self.assertRaises(RuntimeError):
            await mgr.expire_watcher("w1")

    async def test_the_dispatch_arm_exists(self):
        record = make_rule_derived_record(name="w1")
        mgr = self._mgr(record)

        result = await mgr.dispatch_command(
            {"cmd": "expire", "watcher_name": "w1"})
        self.assertTrue(result["ok"])

        result = await mgr.dispatch_command({"cmd": "expire"})
        self.assertFalse(result["ok"])


class TestControlResolvesRecordOnlyNames(unittest.TestCase):

    def test_a_record_only_name_finds_its_entry(self):
        """The control layer resolved verbs through config alone — a
        rule-derived name never reached the lifecycle at all. The record
        answers second, so the static path keeps winning while both exist."""
        from gateway.control import ControlServer

        entry = MagicMock()
        entry.session_manager.get_watcher_config = MagicMock(return_value=None)
        entry.session_manager.get_watcher_state = MagicMock(
            return_value=make_rule_derived_record(name="rc-eng"))
        server = ControlServer.__new__(ControlServer)
        server._entries = [entry]

        self.assertIs(server._find_entry_for_watcher("rc-eng"), entry)

        entry.session_manager.get_watcher_state = MagicMock(return_value=None)
        result = server._find_entry_for_watcher("ghost")
        self.assertIsInstance(result, dict)
        self.assertFalse(result["ok"])


if __name__ == "__main__":
    unittest.main()
