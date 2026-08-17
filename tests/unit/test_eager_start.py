"""Eager creation for connectors with no unsolicited inbound (§2.6, step 7b).

Script's messages arrive by injection and Voice's rooms as HTTP path
segments — nothing ever offers a room, so lazy creation can never fire.
Their rules name literal rooms and every named room starts at boot through
`get_or_create`. This is the cutover's replacement for the static
`watchers:` list: same eager model, driven by rules — sticky binding, the
paused refusal and the frozen record fields all come with the path.
"""

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from gateway.core.room_pattern import RoomPattern
from gateway.core.watcher_rule import RoomMatcher, WatcherRule
from tests.helpers import make_manager


def _rule(name="ops", include=("ops-room",), connector="default"):
    return WatcherRule(
        name=name,
        connector=connector,
        agent="default",
        rooms=RoomMatcher(include=tuple(RoomPattern(p) for p in include)),
    )


def _eager_manager(rules=None):
    return make_manager(watcher_rules=rules if rules is not None else [_rule()])


class TestEagerStart(unittest.IsolatedAsyncioTestCase):

    async def test_a_literal_rule_room_starts_at_boot(self):
        mgr = _eager_manager()
        with patch("gateway.core.watcher_lifecycle.MessageProcessor") as MockProc:
            MockProc.return_value.start = MagicMock()

            errors = await mgr.sync_only()

        self.assertEqual(errors, [])
        record = mgr._lifecycle.record_for_room("ops-room")
        self.assertIsNotNone(record, "the eager loop created the watcher")
        self.assertEqual(record.rule_name, "ops", "created from the rule")
        self.assertTrue(dict(record.config), "the config snapshot is frozen")
        self.assertTrue(record.session_id)
        self.assertIsNotNone(
            mgr._lifecycle.processor_named(record.watcher_name),
            "eager means started, not registered idle")
        self.assertIsNone(mgr._sweep,
                          "an eager connector is never idle-eligible (§2.6)")

    async def test_the_second_boot_resumes_the_same_session(self):
        """Sticky binding through get_or_create: the record is the recreation
        source, so a restart resumes rather than re-mints."""
        mgr = _eager_manager()
        with patch("gateway.core.watcher_lifecycle.MessageProcessor") as MockProc:
            MockProc.return_value.start = MagicMock()
            await mgr.sync_only()
            record = mgr._lifecycle.record_for_room("ops-room")
            session_id = record.session_id
            name = record.watcher_name

            # The restart, in miniature: the processor and its dispatcher
            # claim are gone, the record survives, and the eager loop runs
            # again.
            proc = mgr._lifecycle._processors.pop(name)
            mgr._dispatcher.remove_processor("ops-room", proc)
            errors: list[str] = []
            await mgr._eager_start_rule_rooms(errors)

        self.assertEqual(errors, [])
        woken = mgr._lifecycle.record_for_room("ops-room")
        self.assertEqual(woken.session_id, session_id, "the same session")
        self.assertEqual(woken.rule_name, "ops", "the frozen rule survived")

    async def test_a_paused_room_is_not_started_and_not_an_error(self):
        """§4.4 through the same door as everywhere else: get_or_create
        refuses a paused record, and the eager loop reads that refusal as
        the operator's instruction, not a startup failure."""
        mgr = _eager_manager()
        with patch("gateway.core.watcher_lifecycle.MessageProcessor") as MockProc:
            MockProc.return_value.start = MagicMock()
            MockProc.return_value.stop = AsyncMock()
            await mgr.sync_only()
            record = mgr._lifecycle.record_for_room("ops-room")
            await mgr._lifecycle.pause_watcher(record.watcher_name)

            errors: list[str] = []
            await mgr._eager_start_rule_rooms(errors)

        self.assertEqual(errors, [])
        self.assertTrue(mgr._lifecycle.record_for_room("ops-room").paused)
        self.assertIsNone(mgr._lifecycle.processor_named(record.watcher_name))

    async def test_one_rooms_failure_does_not_stop_the_boot(self):
        mgr = _eager_manager(rules=[_rule(include=("bad-room", "good-room"))])
        real_resolve = mgr._connector.resolve_room

        async def resolve(name):
            if name == "bad-room":
                raise RuntimeError("room service down")
            return await real_resolve(name)

        mgr._connector.resolve_room = resolve
        with patch("gateway.core.watcher_lifecycle.MessageProcessor") as MockProc:
            MockProc.return_value.start = MagicMock()

            errors = await mgr.sync_only()

        self.assertEqual(len(errors), 1)
        self.assertIn("bad-room", errors[0])
        self.assertIsNotNone(mgr._lifecycle.record_for_room("good-room"),
                             "the healthy room still started")

    async def test_an_inbound_connector_is_never_walked(self):
        """Their rooms arrive; walking the rules would double-create."""
        from tests.helpers import make_bare_session_manager

        mgr = make_bare_session_manager(
            _watcher_manager=MagicMock(),
            _watcher_rules=[_rule()],
        )
        mgr._connector.supports_unsolicited_inbound = MagicMock(return_value=True)
        mgr._connector.resolve_room = AsyncMock()

        await mgr._eager_start_rule_rooms([])

        mgr._connector.resolve_room.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
