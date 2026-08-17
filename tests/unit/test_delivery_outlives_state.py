"""#115: a delivery in flight across a watcher restart, and the MM worker leak.

Three of #115's five, pinned as behaviour:

* A watcher stop→start replaces the connector's per-room state object while a
  delivery is mid-handler. Commits written to the detached object vanish with
  it — the accepted message is re-delivered by the next replay, and a
  handed-back one is never recovered. The fence commits to the room's LIVE
  object instead; the room is the identity, not the object.
* Mattermost's `unregister_channel` used to discard only a bookkeeping set
  (write-only in production), leaking a worker task parked on `queue.get()`
  plus its queue per unsubscribed channel, forever.
* The RC routing path's `roomParticipant: False` early-return now records the
  removal for a tracked room, exactly as the tracked path does — a dropped
  removal left the stale watermark in place, and a later re-add replayed the
  whole non-member interval.
"""

import asyncio
import unittest
from unittest.mock import AsyncMock

from tests.unit.test_connector import (
    TestWatermarkAdvancement as _WatermarkSuite,
)


class TestADeliveryOutlivesItsSubscription(unittest.IsolatedAsyncioTestCase):
    """RC: the commit follows the room, not the object (#115)."""

    _make_connector_and_sub = _WatermarkSuite._make_connector_and_sub

    def _doc(self, mid="m1", ts="200"):
        return {
            "_id": mid,
            "u": {"username": "alice", "_id": "uid-alice"},
            "msg": "@bot hello",
            "ts": {"$date": ts},
            "mentions": [{"username": "bot"}],
        }

    async def test_an_accepted_message_commits_to_the_live_subscription(self):
        from gateway.connectors.rocketchat.connector import _RoomSubscription
        from gateway.core.connector import Room

        connector, old_sub = self._make_connector_and_sub()
        fresh = _RoomSubscription(
            room=Room(id="room-1", name="general", type="channel"),
            last_processed_ts="150",
        )
        gate = asyncio.Event()

        async def handler(msg):
            # The watcher restart, mid-delivery: the old object is popped and
            # a fresh one installed while the handler runs.
            connector._rooms["room-1"] = fresh
            gate.set()
            return True

        connector._handler = handler

        await connector._on_raw_ddp_message("room-1", self._doc("m1", "200"))

        self.assertTrue(gate.is_set())
        self.assertEqual(fresh.last_processed_ts, "200",
                         "the watermark landed on the live object")
        self.assertIn("m1", fresh.seen_ids_set,
                      "the dedup id landed on the live object — the next "
                      "replay must not re-deliver an accepted message")

    async def test_a_hand_back_claims_the_window_on_the_live_subscription(self):
        from gateway.connectors.rocketchat.connector import _RoomSubscription
        from gateway.core.connector import Room

        connector, old_sub = self._make_connector_and_sub()
        fresh = _RoomSubscription(
            room=Room(id="room-1", name="general", type="channel"),
            last_processed_ts="150",
        )

        async def handler(msg):
            connector._rooms["room-1"] = fresh
            return False  # queue full — the hand-back path

        connector._handler = handler

        await connector._on_raw_ddp_message("room-1", self._doc("m2", "200"))

        self.assertIsNotNone(fresh.replay_boundary,
                              "the outage window opened on the live object — "
                              "the hand-back is recoverable by the next replay")

    async def test_a_reclaimed_room_commits_nowhere(self):
        connector, old_sub = self._make_connector_and_sub()

        async def handler(msg):
            connector._rooms.pop("room-1")
            return True

        connector._handler = handler

        await connector._on_raw_ddp_message("room-1", self._doc("m3", "200"))

        self.assertEqual(old_sub.last_processed_ts, "100",
                         "nothing was committed to the detached object")


class TestADeliveryCrossingAMembershipRemoval(unittest.IsolatedAsyncioTestCase):
    """Codex review of #121: the redirect fence must tell a benign watcher
    restart from a membership replacement. A pre-removal frame committing
    into a re-added room's fresh state would point the next replay below the
    removal — delivering the whole non-member interval."""

    _make_connector_and_sub = _WatermarkSuite._make_connector_and_sub

    def _doc(self, mid="m1", ts="200"):
        return {
            "_id": mid,
            "u": {"username": "alice", "_id": "uid-alice"},
            "msg": "@bot hello",
            "ts": {"$date": ts},
            "mentions": [{"username": "bot"}],
        }

    async def test_rc_refuses_the_commit_across_a_removal(self):
        from gateway.connectors.rocketchat.connector import _RoomSubscription
        from gateway.core.connector import Room

        connector, old_sub = self._make_connector_and_sub()
        fresh = _RoomSubscription(
            room=Room(id="room-1", name="general", type="channel"),
            last_processed_ts="",
        )

        async def handler(msg):
            # The removal hook marks the CURRENT object, then the re-add
            # installs a fresh one — all while this delivery runs.
            old_sub.left_the_room()
            connector._rooms["room-1"] = fresh
            return True

        connector._handler = handler

        await connector._on_raw_ddp_message("room-1", self._doc("m1", "200"))

        self.assertEqual(fresh.last_processed_ts, "",
                         "no pre-removal watermark in the new membership")
        self.assertNotIn("m1", fresh.seen_ids_set)

    async def test_mm_refuses_the_commit_across_a_removal(self):
        from unittest.mock import AsyncMock

        from gateway.connectors.mattermost.connector import _ChannelState
        from gateway.core.connector import Room
        from tests.unit.test_mattermost_connector import _make_connector

        connector = _make_connector()
        connector._config.require_mention = False
        connector._config.filter_sender = False
        connector._rest.resolve_username = AsyncMock(return_value="alice")

        old = _ChannelState(room=Room(id="chan-1", name="general", type="channel"))
        old.last_processed_ts = "100"
        connector._channels["chan-1"] = old
        fresh = _ChannelState(room=Room(id="chan-1", name="general", type="channel"))

        async def handler(msg):
            old.membership_lost = True  # what the removal hook stamps
            connector._channels["chan-1"] = fresh
            return True

        connector._handler = handler

        await connector._on_posted_event({
            "post": {"id": "p1", "channel_id": "chan-1", "user_id": "u-alice",
                     "message": "hello", "create_at": 200},
            "sender_name": "@alice", "channel_type": "O",
            "channel_name": "general", "channel_display_name": "General",
            "team_id": "", "mentions": [],
        })

        self.assertIsNone(fresh.last_processed_ts)
        self.assertNotIn("p1", fresh.seen_ids_set)

    async def test_the_mm_removal_hook_stamps_the_current_state(self):
        from unittest.mock import AsyncMock

        from gateway.connectors.mattermost.connector import _ChannelState
        from gateway.core.connector import MembershipHook, Room
        from tests.unit.test_mattermost_connector import _make_connector

        connector = _make_connector()
        state = _ChannelState(room=Room(id="chan-1", name="g", type="channel"))
        connector._channels["chan-1"] = state
        connector.register_membership_hook(
            MembershipHook(added=AsyncMock(), removed=AsyncMock()))

        await connector._on_membership_event({
            "event": "user_removed",
            "data": {"channel_id": "chan-1", "remover_id": "admin"},
            "broadcast": {"user_id": "bot-id-1"},
        })
        if connector._routing_tasks:
            import asyncio as _a
            await _a.gather(*connector._routing_tasks)

        self.assertTrue(state.membership_lost)


class TestAFailedWakeReplayKeepsTheWindow(unittest.IsolatedAsyncioTestCase):
    """Codex review of #121: a wake replay reads an EXTERNAL window (the
    record's mark) against a fresh subscription — a failure that simply
    returned left nothing pointing at the interval, and the triggering
    message's commit sealed it away permanently. Whoever fails to replay owns
    keeping the window reachable."""

    _make_connector_and_sub = _WatermarkSuite._make_connector_and_sub

    async def test_rc_claims_the_external_window_on_fetch_failure(self):
        from unittest.mock import AsyncMock

        connector, sub = self._make_connector_and_sub()
        connector._rest.is_room_member = AsyncMock(return_value=True)
        connector._rest.get_room_history_page = AsyncMock(
            side_effect=RuntimeError("REST hiccup"))

        await connector.replay_room_since("room-1", after_ts="50")

        self.assertEqual(sub.replay_boundary, "50",
                         "the unread window is claimed for the next reconnect")

    async def test_rc_claims_the_external_window_on_membership_unknown(self):
        from unittest.mock import AsyncMock

        connector, sub = self._make_connector_and_sub()
        connector._rest.is_room_member = AsyncMock(return_value=None)

        await connector.replay_room_since("room-1", after_ts="50")

        self.assertEqual(sub.replay_boundary, "50")

    async def test_mm_claims_the_external_window_on_fetch_failure(self):
        from unittest.mock import AsyncMock

        from gateway.connectors.mattermost.connector import _ChannelState
        from gateway.core.connector import Room
        from tests.unit.test_mattermost_connector import _make_connector

        connector = _make_connector()
        state = _ChannelState(room=Room(id="chan-1", name="g", type="channel"))
        state.last_processed_ts = "100"
        connector._channels["chan-1"] = state
        connector._rest.get_room_history_page = AsyncMock(
            side_effect=RuntimeError("REST hiccup"))

        await connector.replay_room_since("chan-1", after_ts="50")

        self.assertEqual(state.replay_boundary, "50")


class TestMMDeliveryOutlivesItsState(unittest.IsolatedAsyncioTestCase):
    """Mattermost's twin of the same fence."""

    def _connector(self):
        from tests.unit.test_mattermost_connector import _make_connector

        connector = _make_connector()
        connector._config.require_mention = False
        connector._config.filter_sender = False
        return connector

    def _decoded(self, mid="p1", ts=200):
        return {
            "post": {"id": mid, "channel_id": "chan-1", "user_id": "u-alice",
                     "message": "hello", "create_at": ts},
            "sender_name": "@alice",
            "channel_type": "O",
            "channel_name": "general",
            "channel_display_name": "General",
            "team_id": "",
            "mentions": [],
        }

    def _state(self, connector, ts="150"):
        from gateway.connectors.mattermost.connector import _ChannelState
        from gateway.core.connector import Room

        state = _ChannelState(room=Room(id="chan-1", name="general", type="channel"))
        state.last_processed_ts = ts
        connector._channels["chan-1"] = state
        return state

    async def test_an_accepted_post_commits_to_the_live_state(self):
        connector = self._connector()
        old = self._state(connector, ts="100")
        connector._rest.resolve_username = AsyncMock(return_value="alice")

        fresh = None

        async def handler(msg):
            nonlocal fresh
            fresh = self._state(connector, ts="150")
            return True

        connector._handler = handler

        await connector._on_posted_event(self._decoded("p1", 200))

        self.assertEqual(fresh.last_processed_ts, "200",
                         "the watermark landed on the live state")
        self.assertIn("p1", fresh.seen_ids_set)
        self.assertEqual(old.last_processed_ts, "100")

    async def test_a_hand_back_claims_the_window_on_the_live_state(self):
        connector = self._connector()
        self._state(connector, ts="100")
        connector._rest.resolve_username = AsyncMock(return_value="alice")

        fresh = None

        async def handler(msg):
            nonlocal fresh
            fresh = self._state(connector, ts="150")
            return False

        connector._handler = handler

        await connector._on_posted_event(self._decoded("p2", 200))

        self.assertIsNotNone(fresh.replay_boundary,
                              "the hand-back is recoverable through the live state")


class TestMMUnregisterReleasesTheWorker(unittest.IsolatedAsyncioTestCase):
    """#115: `unregister_channel` releases the queue and cancels the worker —
    it used to discard a bookkeeping set nothing in production reads."""

    async def test_the_worker_and_queue_are_released(self):
        from gateway.connectors.mattermost.websocket import MattermostWebSocketClient

        ws = MattermostWebSocketClient("http://x", token_provider=lambda: "t")
        ws.register_handler(AsyncMock())
        await ws._dispatch({"post": {"channel_id": "chan-1"}})
        worker = ws._channel_workers["chan-1"]
        self.assertFalse(worker.done())

        ws.unregister_channel("chan-1")
        await asyncio.sleep(0)  # let the cancellation land

        self.assertNotIn("chan-1", ws._channel_queues)
        self.assertNotIn("chan-1", ws._channel_workers)
        self.assertTrue(worker.cancelled() or worker.done(),
                        "the worker task was released, not leaked")

    async def test_a_returning_channel_gets_a_fresh_pair(self):
        from gateway.connectors.mattermost.websocket import MattermostWebSocketClient

        ws = MattermostWebSocketClient("http://x", token_provider=lambda: "t")
        ws.register_handler(AsyncMock())
        await ws._dispatch({"post": {"channel_id": "chan-1"}})
        ws.unregister_channel("chan-1")

        await ws._dispatch({"post": {"channel_id": "chan-1"}})
        self.assertIn("chan-1", ws._channel_workers)
        ws.unregister_channel("chan-1")


class TestUnroutedParticipantFalseRecordsTheRemoval(unittest.IsolatedAsyncioTestCase):
    """#115: the routing path's early return on `roomParticipant: False` now
    records the removal for a tracked room — the same `left_the_room()` the
    tracked path calls — instead of leaving the stale watermark for a later
    re-add to replay the non-member interval from."""

    _make_connector_and_sub = _WatermarkSuite._make_connector_and_sub

    def _doc(self):
        return {"_id": "m1", "rid": "room-1", "msg": "hi",
                "u": {"username": "alice", "_id": "uid-alice"},
                "ts": {"$date": "200"}}

    async def test_an_explicit_false_clears_the_tracked_rooms_marks(self):
        connector, sub = self._make_connector_and_sub()
        connector._router = AsyncMock()
        epoch = sub.membership_epoch

        await connector._on_unrouted_message(
            self._doc(), {"roomParticipant": False})

        self.assertEqual(sub.last_processed_ts, "",
                         "the removal cleared the watermark")
        self.assertEqual(sub.membership_epoch, epoch + 1,
                         "in-flight work finds out through the epoch")

    async def test_absence_is_not_a_removal(self):
        """"Nobody said" is not "not a participant" — an access object with
        no answer must not clear anything."""
        connector, sub = self._make_connector_and_sub()
        connector._router = AsyncMock()

        await connector._on_unrouted_message(self._doc(), None)
        await connector._on_unrouted_message(self._doc(), {})

        self.assertEqual(sub.last_processed_ts, "100", "marks untouched")


if __name__ == "__main__":
    unittest.main()
