"""Membership events (§2.7): a removal reclaims the record, a join registers one.

The removal half pins what makes it different from expiry, which shares its
reclamation body: a remove stops a resident processor (expiry bails on
residency), overrides pause with an audit line (expiry never touches paused),
and runs under no TTL. And what makes it the same: everything the record
points at is reclaimed, record popped last, idempotently — a removal
discovered twice reaches the same end state.
"""

import unittest
from unittest.mock import AsyncMock, MagicMock

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


if __name__ == "__main__":
    unittest.main()
