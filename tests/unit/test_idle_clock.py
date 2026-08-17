"""The idle clock (§2.5): `last_activity_at` moves on accepted work only.

One write site — `processor.enqueue` on success — because inbound and
scheduled injection both funnel through it, so the two cannot drift. The
advance is in memory only, persisted at the existing save points: a
per-message disk write is the cost §2.2 explicitly rejects, and a crash that
loses the advance idles a room *early*, which is the safe direction.

What must NOT advance the clock is the half that erodes silently: a refused
message is not activity, and counting it would keep a room the gateway is
dropping messages for alive forever.
"""

import unittest

from gateway.core.connector import IncomingMessage, Room, User, UserRole
from gateway.core.state import WatcherState
from tests.helpers import make_core_config, make_processor


def _msg(mid="m1"):
    return IncomingMessage(
        id=mid, timestamp="1700000000000",
        room=Room(id="room_1", name="test-room"),
        sender=User(id="u1", username="alice", display_name="alice"),
        role=UserRole.OWNER, text="hi",
    )


def _record(**kw):
    defaults = dict(
        watcher_name="test-watcher", session_id="ses_001",
        room_id="room_1", room_type="channel",
    )
    defaults.update(kw)
    return WatcherState(**defaults)


class TestTheIdleClockMovesOnAcceptedWorkOnly(unittest.IsolatedAsyncioTestCase):

    async def test_an_accepted_message_advances_the_clock(self):
        ws = _record(last_activity_at="2026-01-01T00:00:00-08:00")
        processor = make_processor(watcher_state=ws)

        accepted = await processor.enqueue(_msg())

        self.assertTrue(accepted)
        self.assertNotEqual(ws.last_activity_at, "2026-01-01T00:00:00-08:00")
        self.assertTrue(ws.last_activity_at)

    async def test_a_refused_message_is_not_activity(self):
        """Queue full → the message is dropped and owed a retry — counting it
        as activity would keep a room the gateway is refusing alive forever."""
        ws = _record()
        processor = make_processor(
            watcher_state=ws,
            config=make_core_config(max_queue_depth=1),
        )

        self.assertTrue(await processor.enqueue(_msg("m1")))
        stamped = ws.last_activity_at
        self.assertTrue(stamped)

        self.assertFalse(await processor.enqueue(_msg("m2")))
        self.assertEqual(ws.last_activity_at, stamped,
                         "a dropped message must not advance the idle clock")

    async def test_a_stopping_processor_does_not_advance_the_clock(self):
        ws = _record()
        processor = make_processor(watcher_state=ws)
        processor._state = "draining"

        self.assertFalse(await processor.enqueue(_msg()))
        self.assertFalse(ws.last_activity_at)

    async def test_a_processor_with_no_record_still_accepts(self):
        """The static path builds processors without a record; the clock is a
        dynamic-model field and its absence must not cost delivery."""
        processor = make_processor(watcher_state=None)

        self.assertTrue(await processor.enqueue(_msg()))


if __name__ == "__main__":
    unittest.main()
