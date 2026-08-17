"""Startup replay over persisted records, and what it stands on (§2.2, §2.4).

Three layers, tested in order:

1. `sync_watchers` must not prune rule-derived records and must hydrate them —
   the prune line `persisted - config_names` *is* the static model, and left
   alone it would delete cross-restart sticky binding, the paused-record drop,
   and the replay's iteration source in one stroke.
2. `_replay_persisted_records` probes each record's gap, recreates only rooms
   with one, and replays through the connector's shared per-room half.
3. The eligibility gates: paused, static, watermarkless and resident records
   are all left alone.
"""

import unittest
from unittest.mock import AsyncMock, MagicMock

from gateway.core.state import WatcherState
from tests.helpers import make_lifecycle


def _window(mgr):
    """The down-window as `sync_only` would have frozen it — before anything
    the tests then simulate arriving live."""
    return mgr._snapshot_watermarks()


def _dynamic_record(name="rc-eng-backend", room_id="r1", **overrides):
    fields = dict(
        watcher_name=name,
        session_id="sess-1",
        room_id=room_id,
        room_type="channel",
        room_name="eng-backend",
        room_kind="channel",
        last_processed_ts="1786874400000",
        connector="rc",
        agent="claude",
        rule_name="eng",
        rule={"name": "eng"},
        config={"name": name, "connector": "rc", "room": "eng-backend",
                "agent": "claude"},
    )
    fields.update(overrides)
    return WatcherState(**fields)


class TestRuleDerivedRecordsSurviveBoot(unittest.IsolatedAsyncioTestCase):
    """The prune exemption and the hydration — the replay's prerequisites."""

    async def test_a_dynamic_record_is_not_pruned_and_is_hydrated(self):
        record = _dynamic_record()
        store = MagicMock()
        store.load = MagicMock(return_value={record.watcher_name: record})
        store.save = MagicMock()
        lifecycle = make_lifecycle(state_store=store, watcher_configs=[])

        await lifecycle.sync_watchers()

        prune = store.save.call_args.kwargs.get("prune")
        self.assertNotIn(record.watcher_name, prune or set(),
                         "a rule-derived record is not an orphan of the config")
        # Hydrated: sticky binding answers for it from boot.
        self.assertIs(lifecycle.record_for_room("r1"), record)

    async def test_a_static_orphan_is_still_pruned(self):
        """The exemption must not disable deliberate removal: a record with no
        rule_name whose config entry is gone was deleted by the operator."""
        record = _dynamic_record(rule_name="", rule={}, config={})
        store = MagicMock()
        store.load = MagicMock(return_value={record.watcher_name: record})
        store.save = MagicMock()
        lifecycle = make_lifecycle(state_store=store, watcher_configs=[])

        await lifecycle.sync_watchers()

        prune = store.save.call_args.kwargs.get("prune")
        self.assertIn(record.watcher_name, prune)

    async def test_a_hydrated_paused_record_still_answers_paused(self):
        record = _dynamic_record(paused=True)
        store = MagicMock()
        store.load = MagicMock(return_value={record.watcher_name: record})
        store.save = MagicMock()
        lifecycle = make_lifecycle(state_store=store, watcher_configs=[])

        await lifecycle.sync_watchers()

        found = lifecycle.record_for_room("r1")
        self.assertTrue(found.paused, "the pause survives the restart")


class TestStartupReplay(unittest.IsolatedAsyncioTestCase):
    """The replay itself, against a mocked lifecycle and connector."""

    def _manager(self, records):
        from tests.helpers import make_bare_session_manager

        mgr = make_bare_session_manager(_connector_name="rc")
        mgr._connector.probe_missed_since = AsyncMock(return_value=False)
        mgr._connector.replay_room_since = AsyncMock()
        mgr._lifecycle.states = MagicMock(
            return_value={r.watcher_name: r for r in records})
        mgr._lifecycle.processor_named = MagicMock(return_value=None)
        mgr._watcher_manager = MagicMock()
        mgr._watcher_manager.get_or_create = AsyncMock(return_value="proc")
        return mgr

    async def test_an_empty_gap_leaves_the_room_idle(self):
        """The lazy model working: no messages missed, nothing recreated.

        The probe is the connector's judgment, not a raw history read: the
        agent's own last reply always sits above the watermark (which only
        advances on accepted *inbound*), so a probe that counted it would
        report a gap for nearly every room at every boot."""
        mgr = self._manager([_dynamic_record()])

        await mgr._replay_persisted_records(_window(mgr))

        mgr._watcher_manager.get_or_create.assert_not_awaited()
        mgr._connector.replay_room_since.assert_not_awaited()
        # The probe asked from the stored watermark.
        self.assertEqual(
            mgr._connector.probe_missed_since.await_args.args[1], "1786874400000")

    async def test_a_gap_recreates_from_the_record_and_replays(self):
        record = _dynamic_record(room_kind="group_dm",
                                 participants=["alice", "bob"], room_name="")
        mgr = self._manager([record])
        mgr._connector.probe_missed_since = AsyncMock(return_value=True)

        await mgr._replay_persisted_records(_window(mgr))

        # The probe was asked about the room typed by its *kind*, so a group DM
        # reaches the direct-room history endpoint rather than a channel one.
        self.assertEqual(
            mgr._connector.probe_missed_since.await_args.args[0].type, "group_dm")
        args = mgr._watcher_manager.get_or_create.await_args
        self.assertEqual(args.args[0], "rc")
        room = args.args[1]
        self.assertEqual(room.id, "r1")
        self.assertEqual(room.kind.value, "group_dm",
                         "the RoomRef is rebuilt from the record, not re-resolved")
        self.assertEqual(room.participants, ("alice", "bob"))
        # The recreation replays the record's own window itself, so this loop
        # does not replay again — two passes over one interval are not free:
        # replay hands the filter the same boundary both times, so only the id
        # window stands between them.
        mgr._connector.replay_room_since.assert_not_awaited()

    async def test_ineligible_records_are_left_alone(self):
        records = [
            _dynamic_record(name="w-paused", room_id="r2", paused=True),
            _dynamic_record(name="w-static", room_id="r3", rule_name="", config={}),
            _dynamic_record(name="w-fresh", room_id="r4", last_processed_ts=""),
        ]
        mgr = self._manager(records)
        mgr._connector.probe_missed_since = AsyncMock(return_value=True)

        await mgr._replay_persisted_records(_window(mgr))

        mgr._connector.probe_missed_since.assert_not_awaited()
        mgr._watcher_manager.get_or_create.assert_not_awaited()

    async def test_a_room_a_live_message_already_recreated_is_left_alone(self):
        """Inverted again — the fourth deliberate inversion in this step, and
        the first one a *model* overturned rather than a review finding.

        Writing down replay ownership (§2.2) settled it: a recreation owns the
        replay for the room it recreates, and this loop owns no interval at
        all. A resident room with a record must have come through a recreation
        — its record rules out `_create`, and a rule-derived record is absent
        from `watchers:` so the static path never starts one — so its window is
        already replayed, and replaying here would be a second pass over it.

        The probe still reads the frozen snapshot rather than the record: a
        live message may have advanced the watermark past the whole
        down-window, and reading it would report no gap at all.
        """
        record = _dynamic_record()
        mgr = self._manager([record])
        window = _window(mgr)
        mgr._connector.probe_missed_since = AsyncMock(return_value=True)
        # What the live path did between start_inbound and this loop.
        mgr._lifecycle.processor_named = MagicMock(return_value="already-running")
        record.last_processed_ts = "9999999999999"

        await mgr._replay_persisted_records(window)

        self.assertEqual(
            mgr._connector.probe_missed_since.await_args.args[1], "1786874400000",
            "the probe asks about the frozen down-window, not the advanced watermark",
        )
        mgr._watcher_manager.get_or_create.assert_not_awaited()
        mgr._connector.replay_room_since.assert_not_awaited()

    async def test_one_bad_room_does_not_kill_boot(self):
        """Best-effort per record: the probe failing, or the recreation
        raising (which the manager now does on a failed start), logs and moves
        to the next record — the room recovers on its next live message."""
        bad_probe = _dynamic_record(name="w-bad", room_id="r-bad")
        bad_create = _dynamic_record(name="w-worse", room_id="r-worse")
        good = _dynamic_record(name="w-good", room_id="r-good")
        mgr = self._manager([bad_probe, bad_create, good])

        async def probe(room, after_ts):
            if room.id == "r-bad":
                raise RuntimeError("rest down")
            return True

        mgr._connector.probe_missed_since = AsyncMock(side_effect=probe)
        mgr._watcher_manager.get_or_create = AsyncMock(
            side_effect=[RuntimeError("backend down"), "proc"])

        await mgr._replay_persisted_records(_window(mgr))

        # Both failures were survived and the good room was still recovered —
        # by its recreation, which owns the replay.
        self.assertEqual(
            [c.args[1].id for c in mgr._watcher_manager.get_or_create.await_args_list],
            ["r-worse", "r-good"],
        )

    async def test_without_a_manager_the_replay_is_a_no_op(self):
        mgr = self._manager([_dynamic_record()])
        mgr._watcher_manager = None
        await mgr._replay_persisted_records(_window(mgr))
        mgr._connector.probe_missed_since.assert_not_awaited()


class TestTheDownWindowSnapshot(unittest.IsolatedAsyncioTestCase):
    """The snapshot's *placement* is the defence: taken after hydration (which
    is what puts the records in memory) and before the stream opens."""

    def test_only_rule_derived_records_with_a_watermark_are_snapshotted(self):
        from tests.helpers import make_bare_session_manager

        mgr = make_bare_session_manager()
        mgr._lifecycle.states = MagicMock(return_value={
            "keep": _dynamic_record(name="keep"),
            "static": _dynamic_record(name="static", rule_name=""),
            "fresh": _dynamic_record(name="fresh", last_processed_ts=""),
        })

        window = mgr._snapshot_watermarks()

        self.assertEqual(window, {"keep": "1786874400000"})

    async def test_the_snapshot_is_taken_before_the_stream_opens(self):
        """After start_inbound a live message can advance a watermark past the
        whole down-window, so a snapshot taken later describes nothing."""
        from tests.helpers import make_bare_session_manager

        mgr = make_bare_session_manager()
        order = []
        mgr._connector.start_inbound = AsyncMock(
            side_effect=lambda: order.append("inbound"))
        mgr._lifecycle.sync_watchers = AsyncMock(
            side_effect=lambda **kw: order.append("sync") or [])
        mgr._snapshot_watermarks = MagicMock(
            side_effect=lambda: order.append("snapshot") or {})
        mgr._replay_persisted_records = AsyncMock(
            side_effect=lambda w: order.append("replay"))

        await mgr.sync_only()

        self.assertEqual(order, ["sync", "snapshot", "inbound", "replay"])


if __name__ == "__main__":
    unittest.main()
