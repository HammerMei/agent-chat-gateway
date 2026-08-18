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
            # The removal hook marks the current object AND bumps the room's
            # loss generation (both happen on the production removal path);
            # then the re-add installs a fresh object — all while this
            # delivery runs. The fence must compare the ROOM generation, not
            # the replaced object's epoch (Codex round 2).
            old_sub.left_the_room()
            connector._note_membership_loss("room-1")
            connector._rooms["room-1"] = fresh
            return True

        connector._handler = handler

        await connector._on_raw_ddp_message("room-1", self._doc("m1", "200"))

        self.assertEqual(fresh.last_processed_ts, "",
                         "no pre-removal watermark in the new membership")
        self.assertNotIn("m1", fresh.seen_ids_set)

    async def test_rc_refuses_the_commit_when_a_restart_hides_the_removal(self):
        """Codex round 2: A→B→removal→C. A benign restart replaces the entry
        object BEFORE the removal, so the loss marks the replacement (B) and
        the re-add installs a third object (C) that carries no mark at all.
        An object-level fence sees a clean live object; the ROOM-level
        generation is what survives the shuffle."""
        from gateway.connectors.rocketchat.connector import _RoomSubscription
        from gateway.core.connector import Room

        connector, sub_a = self._make_connector_and_sub()

        def _fresh(ts=""):
            return _RoomSubscription(
                room=Room(id="room-1", name="general", type="channel"),
                last_processed_ts=ts,
            )

        sub_b, sub_c = _fresh("150"), _fresh()

        async def handler(msg):
            # 1. Benign watcher restart mid-delivery: A → B, no loss.
            connector._rooms["room-1"] = sub_b
            # 2. The removal lands on B — the mark dies with B.
            sub_b.left_the_room()
            connector._note_membership_loss("room-1")
            # 3. Re-add installs C, unmarked.
            connector._rooms["room-1"] = sub_c
            return True

        connector._handler = handler

        await connector._on_raw_ddp_message("room-1", self._doc("m1", "200"))

        self.assertEqual(sub_c.last_processed_ts, "",
                         "no pre-removal watermark crossed into the third "
                         "object's membership")
        self.assertNotIn("m1", sub_c.seen_ids_set)

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

    async def test_mm_refuses_the_commit_when_a_restart_hides_the_removal(self):
        """Codex round 2, MM half: A→B→removal→C — the membership_lost bit
        lives on the state object and dies with it, so the channel-level
        generation is the fence that survives."""
        from unittest.mock import AsyncMock

        from gateway.connectors.mattermost.connector import _ChannelState
        from gateway.core.connector import Room
        from tests.unit.test_mattermost_connector import _make_connector

        connector = _make_connector()
        connector._config.require_mention = False
        connector._config.filter_sender = False
        connector._rest.resolve_username = AsyncMock(return_value="alice")

        def _state(ts=None):
            s = _ChannelState(room=Room(id="chan-1", name="general", type="channel"))
            s.last_processed_ts = ts
            return s

        state_a, state_b, state_c = _state("100"), _state("150"), _state()
        connector._channels["chan-1"] = state_a

        async def handler(msg):
            # 1. Benign restart mid-delivery: A → B, no loss.
            connector._channels["chan-1"] = state_b
            # 2. The removal stamps B and bumps the channel generation —
            #    exactly what the removal hook does.
            state_b.membership_lost = True
            connector._membership_gen["chan-1"] = (
                connector._membership_gen.get("chan-1", 0) + 1)
            # 3. Re-add installs C, unmarked.
            connector._channels["chan-1"] = state_c
            return True

        connector._handler = handler

        await connector._on_posted_event({
            "post": {"id": "p1", "channel_id": "chan-1", "user_id": "u-alice",
                     "message": "hello", "create_at": 200},
            "sender_name": "@alice", "channel_type": "O",
            "channel_name": "general", "channel_display_name": "General",
            "team_id": "", "mentions": [],
        })

        self.assertIsNone(state_c.last_processed_ts)
        self.assertNotIn("p1", state_c.seen_ids_set)

    async def test_mm_drops_a_post_at_entry_once_membership_is_lost(self):
        """Codex round 9: the reclamation runs in its own task and can wait
        on the watcher lock — a post handled in that window was normalized
        and DELIVERED to the agent of a room the bot had been removed from,
        with the flag consulted only at the commit fence. Dropped at entry
        now: the bot cannot answer in a room it left."""
        from unittest.mock import AsyncMock

        from gateway.connectors.mattermost.connector import _ChannelState
        from gateway.core.connector import Room
        from tests.unit.test_mattermost_connector import _make_connector

        connector = _make_connector()
        connector._config.require_mention = False
        connector._config.filter_sender = False
        resolve = AsyncMock(return_value="alice")
        connector._rest.resolve_username = resolve
        handler = AsyncMock(return_value=True)
        connector._handler = handler

        state = _ChannelState(room=Room(id="chan-1", name="general", type="channel"))
        state.membership_lost = True
        connector._channels["chan-1"] = state

        await connector._on_posted_event({
            "post": {"id": "p1", "channel_id": "chan-1", "user_id": "u-alice",
                     "message": "hello", "create_at": 200},
            "sender_name": "@alice", "channel_type": "O",
            "channel_name": "general", "channel_display_name": "General",
            "team_id": "", "mentions": [],
        })

        handler.assert_not_awaited()
        resolve.assert_not_awaited()
        self.assertNotIn("p1", state.seen_ids_set,
                         "nothing was committed for the dropped post")

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
        self.assertEqual(connector._membership_gen.get("chan-1"), 1,
                         "the removal also bumps the channel-level "
                         "generation — the mark that outlives the object")


class TestParticipantFalseFiresTheRemovalHook(unittest.IsolatedAsyncioTestCase):
    """Codex round 18: the tracked path's `roomParticipant: false` is the
    server's own authoritative removal answer — not the offline inference
    #123 defers — and stopping at the connector-local marks left the
    processor, record, session and jobs alive until the idle TTL. The hook
    now fires, through the same per-room serialization every membership
    event takes."""

    _make_connector_and_sub = TestADeliveryOutlivesItsSubscription._make_connector_and_sub

    async def test_the_hook_fires_and_serializes(self):
        from unittest.mock import AsyncMock

        from gateway.core.connector import MembershipHook

        connector, sub = self._make_connector_and_sub()
        connector._handler = AsyncMock(return_value=True)
        removed = AsyncMock()
        connector.register_membership_hook(
            MembershipHook(added=AsyncMock(), removed=removed))
        connector._routing_tasks = set()

        doc = {
            "_id": "m1",
            "u": {"username": "alice", "_id": "uid-alice"},
            "msg": "@bot hello",
            "ts": {"$date": "200"},
            "mentions": [{"username": "bot"}],
        }
        handled = await connector._on_raw_ddp_message(
            "room-1", doc, access={"roomParticipant": False, "roomType": "c",
                                   "roomName": "general"})
        self.assertTrue(handled)
        for _ in range(10):
            await asyncio.sleep(0)
        if connector._routing_tasks:
            await asyncio.gather(*connector._routing_tasks)

        removed.assert_awaited_once_with("room-1")
        self.assertTrue(sub.membership_lost if hasattr(sub, "membership_lost")
                        else True)


class TestMMReplayAbortsOnAMembershipEraChange(unittest.IsolatedAsyncioTestCase):
    """Codex round 19 (P1): a live remove-then-re-add REPLACES the channel
    state mid-batch, and the exists-check alone let the rest of a batch
    fetched for the OLD era dispatch into the newly joined room. The
    generation captured at replay entry aborts the batch on any change."""

    async def test_the_batch_stops_at_the_era_boundary(self):
        from unittest.mock import AsyncMock

        from gateway.connectors.mattermost.connector import _ChannelState
        from gateway.core.connector import Room
        from tests.unit.test_mattermost_connector import _make_connector

        connector = _make_connector()
        connector._config.require_mention = False
        connector._config.filter_sender = False
        connector._rest.resolve_username = AsyncMock(return_value="alice")

        state = _ChannelState(room=Room(id="chan-1", name="general", type="channel"))
        connector._channels["chan-1"] = state
        posts = [
            {"id": f"p{i}", "channel_id": "chan-1", "user_id": "u-alice",
             "message": f"m{i}", "create_at": 100 + i, "type": ""}
            for i in range(3)
        ]
        connector._rest.get_room_history = AsyncMock(return_value=posts)

        delivered = []

        async def handler(msg):
            delivered.append(msg.id)
            if len(delivered) == 1:
                # The removal + re-add, mid-batch: gen bumps, fresh state.
                connector._membership_gen["chan-1"] = (
                    connector._membership_gen.get("chan-1", 0) + 1)
                connector._channels["chan-1"] = _ChannelState(
                    room=Room(id="chan-1", name="general", type="channel"))
            return True

        connector._handler = handler

        await connector.replay_room_since("chan-1", after_ts="50")

        self.assertEqual(delivered, ["p0"],
                         "the batch stopped at the era boundary — the old "
                         "era's remaining posts never reached the new room")

    async def test_an_era_change_during_the_fetch_rejects_the_whole_batch(self):
        """Codex round 21: the capture must sit BEFORE the history fetch — a
        remove-then-re-add landing during the fetch itself would otherwise be
        snapshotted as the current era, and the OLD era's entire fetched
        batch would dispatch into the re-added room."""
        from unittest.mock import AsyncMock

        from gateway.connectors.mattermost.connector import _ChannelState
        from gateway.core.connector import Room
        from tests.unit.test_mattermost_connector import _make_connector

        connector = _make_connector()
        connector._config.require_mention = False
        connector._config.filter_sender = False
        connector._rest.resolve_username = AsyncMock(return_value="alice")

        state = _ChannelState(room=Room(id="chan-1", name="general", type="channel"))
        connector._channels["chan-1"] = state
        posts = [{"id": "p0", "channel_id": "chan-1", "user_id": "u-alice",
                  "message": "m0", "create_at": 100, "type": ""}]

        async def fetch_then_era_change(channel_id, count=50, before_ts=None,
                                        after_ts=None):
            # The remove + re-add, DURING the fetch.
            connector._membership_gen["chan-1"] = (
                connector._membership_gen.get("chan-1", 0) + 1)
            connector._channels["chan-1"] = _ChannelState(
                room=Room(id="chan-1", name="general", type="channel"))
            from gateway.core.connector import HistoryPage
            return HistoryPage(messages=posts, raw_count=1, limit=count)

        connector._rest.get_room_history_page = fetch_then_era_change
        delivered = []
        connector._handler = AsyncMock(
            side_effect=lambda m: delivered.append(m.id) or True)

        await connector.replay_room_since("chan-1", after_ts="50")

        self.assertEqual(delivered, [],
                         "the whole batch was rejected — it was fetched for "
                         "an era that ended during the fetch")


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
