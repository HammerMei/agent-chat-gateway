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
        from gateway.core.session_manager import SessionManager

        mgr = SessionManager.__new__(SessionManager)
        mgr._connector = MagicMock()
        mgr._connector.probe_missed_since = AsyncMock(return_value=False)
        mgr._connector.replay_room_since = AsyncMock()
        mgr._connector_name = "rc"
        mgr._lifecycle = MagicMock()
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

        await mgr._replay_persisted_records()

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

        await mgr._replay_persisted_records()

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
        mgr._connector.replay_room_since.assert_awaited_once_with("r1")

    async def test_ineligible_records_are_left_alone(self):
        records = [
            _dynamic_record(name="w-paused", room_id="r2", paused=True),
            _dynamic_record(name="w-static", room_id="r3", rule_name="", config={}),
            _dynamic_record(name="w-fresh", room_id="r4", last_processed_ts=""),
        ]
        mgr = self._manager(records)
        mgr._connector.probe_missed_since = AsyncMock(return_value=True)

        await mgr._replay_persisted_records()

        mgr._connector.probe_missed_since.assert_not_awaited()
        mgr._watcher_manager.get_or_create.assert_not_awaited()

    async def test_a_resident_room_is_not_probed(self):
        mgr = self._manager([_dynamic_record()])
        mgr._lifecycle.processor_named = MagicMock(return_value="already-running")

        await mgr._replay_persisted_records()

        mgr._connector.probe_missed_since.assert_not_awaited()

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

        await mgr._replay_persisted_records()

        # Both failures were survived and the good room still replayed.
        mgr._connector.replay_room_since.assert_awaited_once_with("r-good")

    async def test_without_a_manager_the_replay_is_a_no_op(self):
        mgr = self._manager([_dynamic_record()])
        mgr._watcher_manager = None
        await mgr._replay_persisted_records()
        mgr._connector.probe_missed_since.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
