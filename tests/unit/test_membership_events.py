"""Membership events (§2.7): a removal reclaims the record, a join registers one.

The removal half pins what makes it different from expiry, which shares its
reclamation body: a remove stops a resident processor (expiry bails on
residency), overrides pause with an audit line (expiry never touches paused),
and runs under no TTL. And what makes it the same: everything the record
points at is reclaimed, record popped last, idempotently — a removal
discovered twice reaches the same end state.
"""

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

from gateway.core.room_pattern import RoomPattern
from gateway.core.state import StateFilter, lifecycle_state, past_expire_ttl
from gateway.core.watcher_manager import RoomRef, WatcherManager, config_from_record
from gateway.core.watcher_rule import RoomKind, RoomMatcher, WatcherRule
from tests.helpers import (
    MockAgentBackend,
    make_core_config,
    make_lifecycle,
    make_rule_derived_record,
)


def _harness(records, *, processors=None):
    """A real lifecycle holding real records, its collaborators doubled —
    the idle-sweep suite's shape, without the sweep."""
    connector = MagicMock()
    connector.get_last_processed_ts = MagicMock(return_value=None)
    connector.unsubscribe_room = AsyncMock()
    lifecycle = make_lifecycle(
        connector=connector,
        agents={"default": MockAgentBackend()},
        config=make_core_config(),
    )
    lifecycle._attachment_workspace = MagicMock()
    for r in records:
        lifecycle._states[r.watcher_name] = r
    for name, proc in (processors or {}).items():
        lifecycle._processors[name] = proc
    return lifecycle, connector


def _resident_processor():
    processor = MagicMock()
    processor.has_work_in_flight = False
    processor.stop = AsyncMock()
    return processor


class TestARemovalReclaimsTheRecord(unittest.IsolatedAsyncioTestCase):

    async def test_an_idle_record_is_reclaimed_entirely(self):
        record = make_rule_derived_record(dropped_at="2026-08-01T00:00:00+00:00")
        lifecycle, connector = _harness([record])

        name = await lifecycle.reclaim_room("room-w1", reason="removed from the room")

        self.assertEqual(name, "w1")
        self.assertIsNone(lifecycle.get_watcher_state("w1"), "the record is gone")
        connector.unsubscribe_room.assert_awaited_once_with("room-w1", watcher_id="w1")
        lifecycle._maps.remove_session.assert_called_with("sess-1")
        lifecycle._attachment_workspace.reclaim.assert_called_once()
        lifecycle._state_store.save.assert_called()

    async def test_a_resident_watcher_is_stopped_first_then_reclaimed(self):
        """Expiry bails on residency; a remove cannot — the bot can be kicked
        from a live room mid-conversation, and leaving the processor running
        against a popped record is the defect the stop exists to prevent."""
        record = make_rule_derived_record()
        proc = _resident_processor()
        lifecycle, connector = _harness([record], processors={"w1": proc})

        name = await lifecycle.reclaim_room("room-w1", reason="kicked")

        self.assertEqual(name, "w1")
        proc.stop.assert_awaited()
        self.assertNotIn("w1", lifecycle._processors)
        self.assertIsNone(lifecycle.get_watcher_state("w1"))

    async def test_pause_is_overridden_and_audited(self):
        """§2.7: removal is not an inference from inactivity — it is the
        platform stating the room is gone, so it overrides the one setting
        every timer honours. Never silently: the audit line is a requirement."""
        record = make_rule_derived_record(paused=True)
        lifecycle, _ = _harness([record])

        with self.assertLogs(
            "agent-chat-gateway.core.watcher_lifecycle", level="WARNING"
        ) as captured:
            name = await lifecycle.reclaim_room("room-w1", reason="removed")

        self.assertEqual(name, "w1")
        self.assertIsNone(lifecycle.get_watcher_state("w1"))
        audit = [line for line in captured.output if "AUDIT" in line]
        self.assertEqual(len(audit), 1, "the pause override is audited, once")
        self.assertIn("room-w1", audit[0], "the audit names the room")
        self.assertIn("pause", audit[0].lower())

    async def test_no_record_is_an_idempotent_no_op(self):
        """A missed event discovered twice — the live event and a later REST
        failure, or the reconciliation — must reach the same end state."""
        record = make_rule_derived_record()
        lifecycle, connector = _harness([record])

        self.assertEqual(await lifecycle.reclaim_room("room-w1", reason="removed"), "w1")
        self.assertIsNone(await lifecycle.reclaim_room("room-w1", reason="removed"))
        self.assertIsNone(await lifecycle.reclaim_room("room-unknown", reason="removed"))
        connector.unsubscribe_room.assert_awaited_once()

    async def test_a_static_record_is_config_yamls_not_the_events(self):
        """A record with no materialized config is recreated from config.yaml
        at every boot regardless of records — reclaiming it here would delete
        a watermark while leaving the watcher's owner intent in place."""
        record = make_rule_derived_record(config={})
        lifecycle, connector = _harness([record])

        self.assertIsNone(await lifecycle.reclaim_room("room-w1", reason="removed"))
        self.assertIsNotNone(lifecycle.get_watcher_state("w1"), "the record is kept")
        connector.unsubscribe_room.assert_not_awaited()

    async def test_a_failed_stop_does_not_refuse_the_reclaim(self):
        """The room is gone whatever the teardown hit — a remove that refused
        to reclaim on a stop error would keep a session for a room that can
        never receive another message."""
        record = make_rule_derived_record()
        proc = _resident_processor()
        proc.stop = AsyncMock(side_effect=RuntimeError("network died"))
        lifecycle, _ = _harness([record], processors={"w1": proc})

        name = await lifecycle.reclaim_room("room-w1", reason="removed")

        self.assertEqual(name, "w1")
        self.assertIsNone(lifecycle.get_watcher_state("w1"))


def _rule(name="eng", include=("eng-*",), **kwargs):
    return WatcherRule(
        name=name,
        connector="rc",
        agent="default",
        rooms=RoomMatcher(
            include=tuple(RoomPattern(p) for p in include),
            except_for=(),
            direct=False,
            group_direct=False,
        ),
        **kwargs,
    )


def _room(id="r1", kind=RoomKind.CHANNEL, name="eng-backend", participants=()):
    return RoomRef(id=id, kind=kind, name=name, participants=participants)


def _add_harness(rules=None):
    """A real manager over a real lifecycle — the add path writes a record
    and starts nothing, so the whole stack can be real except the stores."""
    lifecycle, connector = _harness([])
    manager = WatcherManager(
        "rc", connector, lifecycle, rules if rules is not None else [_rule()])
    return manager, lifecycle, connector


class TestAJoinRegistersAnIdleRecord(unittest.IsolatedAsyncioTestCase):

    async def test_the_record_is_idle_addressable_and_recreatable(self):
        manager, lifecycle, connector = _add_harness()

        name = await manager.register_on_join(_room())

        self.assertEqual(name, "rc-eng-backend")
        record = lifecycle.get_watcher_state(name)
        self.assertIsNotNone(record, "the record is persisted")
        # Idle, not active and not failed: dropped_at is the join stamp.
        self.assertTrue(record.dropped_at)
        self.assertEqual(
            lifecycle_state(record, resident=False), StateFilter.IDLE)
        # The rule was snapshotted at join (§2.4) and the config materialized —
        # without the frozen config the first-message wake declines the record
        # as static and the room is permanently deaf.
        self.assertIsNotNone(config_from_record(record))
        self.assertEqual(record.rule_name, "eng")
        self.assertTrue(record.rule)
        self.assertEqual(record.room_kind, "channel")
        lifecycle._state_store.save.assert_called()

    async def test_nothing_is_started(self):
        manager, lifecycle, connector = _add_harness()

        name = await manager.register_on_join(_room())

        record = lifecycle.get_watcher_state(name)
        self.assertEqual(record.session_id, "", "no session is provisioned")
        self.assertNotIn(name, lifecycle._processors, "no processor runs")
        connector.subscribe_room.assert_not_called()
        lifecycle._dispatcher.add_processor.assert_not_called()

    async def test_a_duplicate_add_never_restamps_the_clocks(self):
        """A duplicate add event — or an add for a room whose record already
        exists — must not reset dropped_at (pushing expiry out) or re-snapshot
        the rule (breaking sticky binding)."""
        manager, lifecycle, _ = _add_harness()

        name = await manager.register_on_join(_room())
        record = lifecycle.get_watcher_state(name)
        stamped = record.dropped_at

        self.assertIsNone(await manager.register_on_join(_room()))
        self.assertIs(lifecycle.get_watcher_state(name), record)
        self.assertEqual(record.dropped_at, stamped)

    async def test_no_matching_rule_registers_nothing(self):
        manager, lifecycle, _ = _add_harness(rules=[_rule(include=("ops-*",))])

        self.assertIsNone(await manager.register_on_join(_room()))
        self.assertIsNone(lifecycle.get_watcher_state("rc-eng-backend"))

    async def test_a_disarmed_manager_registers_nothing(self):
        manager, lifecycle, _ = _add_harness()
        manager.disarm()

        self.assertIsNone(await manager.register_on_join(_room()))
        self.assertIsNone(lifecycle.get_watcher_state("rc-eng-backend"))

    async def test_a_never_spoken_room_expires_from_the_join_stamp(self):
        """Deliberate (§2.7): the registered record reuses the idle state
        rather than inventing a fourth one, so it inherits idle's full
        semantics — including expiry `session_expire_days` after the join."""
        manager, lifecycle, _ = _add_harness()

        name = await manager.register_on_join(_room())
        record = lifecycle.get_watcher_state(name)

        joined = datetime.fromisoformat(record.dropped_at)
        if joined.tzinfo is None:
            joined = joined.astimezone(timezone.utc)
        self.assertFalse(past_expire_ttl(record, joined + timedelta(days=14)))
        self.assertTrue(past_expire_ttl(record, joined + timedelta(days=16)))


def _bare_manager_with_membership(**attrs):
    from tests.helpers import make_bare_session_manager

    mgr = make_bare_session_manager(**attrs)
    if mgr._watcher_manager is None:
        mgr._watcher_manager = MagicMock()
        mgr._watcher_manager.disarmed = False
        mgr._watcher_manager.register_on_join = AsyncMock(return_value="w1")
    mgr._lifecycle.reclaim_room = AsyncMock(return_value="w1")
    return mgr


class TestTheMembershipHandlers(unittest.IsolatedAsyncioTestCase):
    """The SessionManager half: events reach the manager and the lifecycle,
    jobs are cancelled for a reclaimed name, and nothing ever raises toward
    the connector — a dropped add is re-discovered by the room's first
    message, a dropped remove by the reconciliation."""

    async def test_an_add_registers_through_the_manager(self):
        mgr = _bare_manager_with_membership()
        room = _room()

        await mgr._on_membership_added(room)

        mgr._watcher_manager.register_on_join.assert_awaited_once_with(room)

    async def test_a_remove_reclaims_and_cancels_the_jobs(self):
        cancelled = []
        mgr = _bare_manager_with_membership(_cancel_jobs=cancelled.append)

        await mgr._on_membership_removed("room-w1")

        mgr._lifecycle.reclaim_room.assert_awaited_once()
        self.assertEqual(
            mgr._lifecycle.reclaim_room.call_args.args[0], "room-w1")
        self.assertEqual(cancelled, ["w1"])

    async def test_a_no_op_reclaim_cancels_nothing(self):
        """reclaim_room answering None means no record was reclaimed — a
        static record, or no record at all — and the jobs of a watcher that
        still exists must not be cancelled."""
        cancelled = []
        mgr = _bare_manager_with_membership(_cancel_jobs=cancelled.append)
        mgr._lifecycle.reclaim_room = AsyncMock(return_value=None)

        await mgr._on_membership_removed("room-w1")

        self.assertEqual(cancelled, [])

    async def test_a_disarmed_manager_ignores_both(self):
        """Mid-shutdown, an add must not register after the final save and a
        remove must not race stop_all — both are re-discovered later."""
        mgr = _bare_manager_with_membership()
        mgr._watcher_manager.disarmed = True

        await mgr._on_membership_added(_room())
        await mgr._on_membership_removed("room-w1")

        mgr._watcher_manager.register_on_join.assert_not_awaited()
        mgr._lifecycle.reclaim_room.assert_not_awaited()

    async def test_no_manager_means_no_membership_handling(self):
        """Static-only deployments keep their exact behaviour (§2.7)."""
        mgr = _bare_manager_with_membership()
        mgr._watcher_manager = None

        await mgr._on_membership_added(_room())
        await mgr._on_membership_removed("room-w1")

        mgr._lifecycle.reclaim_room.assert_not_awaited()

    async def test_handler_failures_never_reach_the_connector(self):
        mgr = _bare_manager_with_membership()
        mgr._watcher_manager.register_on_join = AsyncMock(
            side_effect=RuntimeError("boom"))
        mgr._lifecycle.reclaim_room = AsyncMock(
            side_effect=RuntimeError("boom"))

        await mgr._on_membership_added(_room())      # must not raise
        await mgr._on_membership_removed("room-w1")  # must not raise

    async def test_a_failed_job_cancel_does_not_undo_the_reclaim(self):
        def explode(name):
            raise RuntimeError("store is corrupt")

        mgr = _bare_manager_with_membership(_cancel_jobs=explode)

        await mgr._on_membership_removed("room-w1")  # must not raise

        mgr._lifecycle.reclaim_room.assert_awaited_once()

    async def test_connect_only_registers_the_hook_with_the_router(self):
        """The hook is gated exactly like the router: rules exist, or neither
        is registered and a static-only deployment keeps its behaviour."""
        mgr = _bare_manager_with_membership()
        await mgr.connect_only()
        mgr._connector.register_membership_hook.assert_called_once()
        hook = mgr._connector.register_membership_hook.call_args.args[0]
        self.assertEqual(hook.added, mgr._on_membership_added)
        self.assertEqual(hook.removed, mgr._on_membership_removed)

        static = _bare_manager_with_membership()
        static._watcher_manager = None
        await static.connect_only()
        static._connector.register_membership_hook.assert_not_called()


if __name__ == "__main__":
    unittest.main()
