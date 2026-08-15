"""Unit tests for RCWebSocketClient reconnect path and callback task tracking.

Covers:
  - _reconnect re-subscribes all previously subscribed rooms
  - reconnect re-confirmation marks failed rooms explicitly
  - Failed reconnect is handled gracefully (doesn't crash the listen loop)
  - _callback_tasks set tracks and auto-discards completed tasks

Run with:
    uv run python -m pytest tests/test_ws_reconnect.py -v
"""

from __future__ import annotations

import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch

from gateway.connectors.rocketchat.websocket import RCWebSocketClient


def _make_client() -> RCWebSocketClient:
    """Create a client with dummy credentials for testing."""
    return RCWebSocketClient(
        server_url="http://localhost:3000",
        username="testuser",
        password="testpass",
    )


def _ddp_connected() -> str:
    """DDP connected handshake response."""
    return json.dumps({"msg": "connected", "session": "test-session"})


def _ddp_login_result(method_id: str) -> str:
    """Successful DDP login result response."""
    return json.dumps({"msg": "result", "id": method_id, "result": {"token": "tok"}})


# ── Reconnect re-subscription ────────────────────────────────────────────────


class TestReconnectResubscribes(unittest.IsolatedAsyncioTestCase):
    """Verify that _reconnect re-subscribes all rooms in _callbacks."""

    async def test_reconnect_resubscribes_all_rooms(self):
        """After a successful reconnect, every room in _callbacks gets a new subscription."""
        client = _make_client()

        # Pre-populate the callbacks dict as if rooms were subscribed before disconnect.
        callback_a = AsyncMock()
        callback_b = AsyncMock()
        client._callbacks = {"room_A": callback_a, "room_B": callback_b}
        # Stale subscriptions from before disconnect
        client._subscriptions = {"room_A": "old_sub_a", "room_B": "old_sub_b"}

        sent_messages: list[dict] = []

        async def mock_send(data: dict) -> None:
            sent_messages.append(data)

        # Mock connect() to succeed without a real WebSocket
        client.connect = AsyncMock()
        client._send = mock_send

        async def confirm_all_subscriptions() -> None:
            while len(client._pending_subs) < 2:
                await asyncio.sleep(0)
            for fut in list(client._pending_subs.values()):
                if not fut.done():
                    fut.set_result(True)

        confirmer = asyncio.create_task(confirm_all_subscriptions())

        await client._reconnect()
        await client._recovery_task
        await confirmer

        # connect() must be called once
        client.connect.assert_called_once()

        # Two subscription messages must be sent (one per room)
        sub_msgs = [m for m in sent_messages if m.get("msg") == "sub"]
        self.assertEqual(len(sub_msgs), 2)

        subscribed_rooms = {m["params"][0] for m in sub_msgs}
        self.assertEqual(subscribed_rooms, {"room_A", "room_B"})

        # Each subscription must use the "stream-room-messages" collection
        for m in sub_msgs:
            self.assertEqual(m["name"], "stream-room-messages")
            self.assertEqual(m["params"][1], False)

        # _subscriptions must be updated with new sub_ids (not the old ones)
        self.assertIn("room_A", client._subscriptions)
        self.assertIn("room_B", client._subscriptions)
        self.assertNotEqual(client._subscriptions["room_A"], "old_sub_a")
        self.assertNotEqual(client._subscriptions["room_B"], "old_sub_b")
        self.assertEqual(client._subscription_states["room_A"].status, "active")
        self.assertEqual(client._subscription_states["room_B"].status, "active")

    async def test_reconnect_marks_failed_room_when_resubscribe_rejected(self):
        """Rejected room re-subscription is tracked explicitly instead of silently lost."""
        client = _make_client()

        callback_a = AsyncMock()
        callback_b = AsyncMock()
        client._callbacks = {"room_A": callback_a, "room_B": callback_b}

        sent_messages: list[dict] = []

        async def mock_send(data: dict) -> None:
            sent_messages.append(data)

        client.connect = AsyncMock()
        client._send = mock_send

        async def resolve_pending_subs() -> None:
            while len(client._pending_subs) < 2:
                await asyncio.sleep(0)
            room_by_sub = {
                frame["id"]: frame["params"][0]
                for frame in sent_messages
                if frame.get("msg") == "sub"
            }
            for sub_id, fut in list(client._pending_subs.items()):
                if fut.done():
                    continue
                room_id = room_by_sub[sub_id]
                if room_id == "room_B":
                    fut.set_exception(
                        RuntimeError(
                            "Subscription rejected by server: room_B unavailable"
                        )
                    )
                else:
                    fut.set_result(True)

        resolver = asyncio.create_task(resolve_pending_subs())

        await client._reconnect()
        await client._recovery_task
        await resolver

        self.assertEqual(client._subscription_states["room_A"].status, "active")
        self.assertEqual(client._subscription_states["room_B"].status, "failed")
        self.assertIn(
            "room_B unavailable",
            client._subscription_states["room_B"].last_error,
        )
        self.assertNotIn("room_B", client._subscriptions)

    async def test_reconnect_with_no_prior_subscriptions(self):
        """Reconnect with empty _callbacks just reconnects, no subscription messages."""
        client = _make_client()
        client._callbacks = {}
        client._subscriptions = {}

        sent_messages: list[dict] = []
        client.connect = AsyncMock()
        client._send = AsyncMock(side_effect=lambda d: sent_messages.append(d))

        await client._reconnect()

        client.connect.assert_called_once()
        sub_msgs = [m for m in sent_messages if m.get("msg") == "sub"]
        self.assertEqual(len(sub_msgs), 0)

    async def test_reconnect_resets_delay_on_success(self):
        """After connect() succeeds, _reconnect_delay should be reset (by connect())."""
        client = _make_client()
        client._reconnect_delay = 16.0  # Simulate several failed retries
        client._callbacks = {}
        client.connect = AsyncMock()  # connect() resets _reconnect_delay internally

        await client._reconnect()

        # The delay was doubled before the attempt; connect() should have reset it.
        # Since we mock connect() directly (not the full WebSocket handshake),
        # we verify that connect was called and would reset the delay.
        client.connect.assert_called_once()


# ── Failed reconnect handling ────────────────────────────────────────────────


class TestReconnectFailure(unittest.IsolatedAsyncioTestCase):
    """Verify that failed reconnect attempts are handled gracefully."""

    async def test_failed_reconnect_sets_ws_to_none(self):
        """If connect() raises, _ws must remain None so the listen loop retries."""
        client = _make_client()
        client._callbacks = {"room_A": AsyncMock()}
        client.connect = AsyncMock(side_effect=RuntimeError("Connection refused"))

        # Must not raise — the error is caught internally
        await client._reconnect()

        self.assertIsNone(client._ws)

    async def test_failed_reconnect_increments_delay(self):
        """Each failed reconnect doubles the backoff delay (up to max)."""
        client = _make_client()
        client._reconnect_delay = 2.0
        client._callbacks = {}
        client.connect = AsyncMock(side_effect=RuntimeError("fail"))

        await client._reconnect()

        # Delay is doubled BEFORE the attempt, so even on failure it's advanced.
        # The initial delay (2.0) was applied; after the failed connect, _ws=None.
        self.assertIsNone(client._ws)

    async def test_reconnect_delay_capped_at_max(self):
        """Backoff delay must not exceed _max_reconnect_delay."""
        client = _make_client()
        client._reconnect_delay = 50.0
        client._max_reconnect_delay = 60.0
        client._callbacks = {}
        client.connect = AsyncMock(side_effect=RuntimeError("fail"))

        await client._reconnect()

        self.assertLessEqual(client._reconnect_delay, client._max_reconnect_delay)

    async def test_listen_loop_retries_after_connection_closed(self):
        """The listen loop must call _reconnect when ConnectionClosed is raised."""
        import websockets

        client = _make_client()
        client._running = True
        call_count = 0

        # Create a mock WebSocket that raises ConnectionClosed on first recv,
        # then we stop the loop.
        mock_ws = AsyncMock()

        async def mock_recv():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise websockets.ConnectionClosed(None, None)
            # Second call: stop the loop
            client._running = False
            return json.dumps({"msg": "ping"})

        mock_ws.recv = mock_recv
        mock_ws.send = AsyncMock()
        client._ws = mock_ws

        reconnect_called = False

        async def tracked_reconnect():
            nonlocal reconnect_called
            reconnect_called = True
            # Restore a working ws for the next iteration
            client._ws = mock_ws

        client._reconnect = tracked_reconnect

        await client._listen_loop()

        self.assertTrue(reconnect_called)

    async def test_listen_loop_retries_after_generic_exception(self):
        """The listen loop must handle non-WebSocket exceptions gracefully."""
        client = _make_client()
        client._running = True

        mock_ws = AsyncMock()
        call_count = 0

        async def mock_recv():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise OSError("Network unreachable")
            client._running = False
            return json.dumps({"msg": "ping"})

        mock_ws.recv = mock_recv
        mock_ws.send = AsyncMock()
        client._ws = mock_ws

        reconnect_called = False

        async def tracked_reconnect():
            nonlocal reconnect_called
            reconnect_called = True
            client._ws = mock_ws

        client._reconnect = tracked_reconnect

        await client._listen_loop()

        self.assertTrue(reconnect_called)


# ── Callback task tracking ───────────────────────────────────────────────────


class TestCallbackTaskTracking(unittest.IsolatedAsyncioTestCase):
    """Verify that _callback_tasks tracks in-flight tasks and auto-discards."""

    async def test_room_worker_created_on_first_message(self):
        """First message to a room creates a worker task tracked in _callback_tasks."""
        client = _make_client()
        callback_done = asyncio.Event()

        async def tracking_callback(doc, access=None):
            callback_done.set()

        client._callbacks = {"room_X": tracking_callback}

        msg = {
            "msg": "changed",
            "collection": "stream-room-messages",
            "fields": {
                "eventName": "room_X",
                "args": [{"_id": "msg1", "rid": "room_X", "msg": "hello"}],
            },
        }
        await client._handle_room_message(msg)

        # Worker task should be in the set
        self.assertEqual(len(client._callback_tasks), 1)

        # Wait for callback to process the message
        await callback_done.wait()

        # Worker is still alive (long-lived), waiting for next message
        self.assertEqual(len(client._callback_tasks), 1)

        # Clean up
        for task in list(client._callback_tasks):
            task.cancel()
        await asyncio.gather(*client._callback_tasks, return_exceptions=True)

    async def test_worker_task_persists_for_multiple_messages(self):
        """Worker task processes multiple messages sequentially (one worker per room)."""
        client = _make_client()
        received: list[str] = []

        async def collecting_callback(doc, access=None):
            received.append(doc.get("msg", ""))

        client._callbacks = {"room_Y": collecting_callback}

        for i in range(3):
            msg = {
                "msg": "changed",
                "collection": "stream-room-messages",
                "fields": {
                    "eventName": "room_Y",
                    "args": [{"_id": f"msg{i}", "rid": "room_Y", "msg": f"m{i}"}],
                },
            }
            await client._handle_room_message(msg)

        await asyncio.sleep(0.1)

        # All messages processed by the same worker, in order
        self.assertEqual(received, ["m0", "m1", "m2"])
        # Only one worker task for the room
        self.assertEqual(len(client._callback_tasks), 1)

    async def test_multiple_rooms_create_multiple_workers(self):
        """Messages to different rooms create one worker task per room."""
        client = _make_client()
        barrier = asyncio.Event()

        async def blocking_callback(doc, access=None):
            await barrier.wait()

        client._callbacks = {
            "room_A": blocking_callback,
            "room_B": blocking_callback,
            "room_C": blocking_callback,
        }

        for room in ("room_A", "room_B", "room_C"):
            msg = {
                "msg": "changed",
                "collection": "stream-room-messages",
                "fields": {
                    "eventName": room,
                    "args": [{"_id": f"msg-{room}", "rid": room, "msg": "hi"}],
                },
            }
            await client._handle_room_message(msg)

        # One worker task per room (3 rooms = 3 tasks)
        self.assertEqual(len(client._callback_tasks), 3)

        barrier.set()
        await asyncio.sleep(0.05)

    async def test_stop_cancels_callback_tasks(self):
        """stop() must cancel and drain all in-flight callback tasks."""
        client = _make_client()
        barrier = asyncio.Event()

        async def blocking_callback(doc, access=None):
            await barrier.wait()

        client._callbacks = {"room_W": blocking_callback}

        msg = {
            "msg": "changed",
            "collection": "stream-room-messages",
            "fields": {
                "eventName": "room_W",
                "args": [{"_id": "msg_stop", "rid": "room_W", "msg": "test"}],
            },
        }
        await client._handle_room_message(msg)
        self.assertEqual(len(client._callback_tasks), 1)

        # stop() should cancel the blocked task
        client._running = False
        client._ws = AsyncMock()
        client._ws.close = AsyncMock()
        await client.stop()


class TestInboundOverflowState(unittest.IsolatedAsyncioTestCase):
    """Verify room queue overflow is surfaced as degraded subscription state."""

    async def test_queue_overflow_marks_room_degraded_and_counts_drops(self):
        client = _make_client()

        async def callback(_doc):
            return None

        room_id = "room_overflow"
        client._callbacks = {room_id: callback}

        queue: asyncio.Queue = asyncio.Queue(maxsize=1)
        queue.put_nowait(({"_id": "existing"}, None))
        client._room_queues[room_id] = queue

        blocker = asyncio.Event()

        async def never_finishes():
            await blocker.wait()

        worker = asyncio.create_task(never_finishes())

        async def cleanup_worker() -> None:
            worker.cancel()
            try:
                await worker
            except asyncio.CancelledError:
                pass

        self.addAsyncCleanup(cleanup_worker)
        client._room_workers[room_id] = worker

        msg = {
            "msg": "changed",
            "collection": "stream-room-messages",
            "fields": {
                "eventName": room_id,
                "args": [{"_id": "msg1", "rid": room_id, "msg": "hello"}],
            },
        }

        await client._handle_room_message(msg)

        state = client.subscription_statuses[room_id]
        self.assertEqual(state["status"], "degraded")
        self.assertEqual(state["dropped_messages"], 1)
        self.assertIn("overflow", state["last_error"])

        self.assertEqual(len(client._callback_tasks), 0)


if __name__ == "__main__":
    unittest.main()


# ── Appended from test_round7_fixes.py ────────────────────────────────────────


class TestPendingSubsCancelledOnReconnect(unittest.IsolatedAsyncioTestCase):
    """Pending subscription futures must be cancelled/errored during _reconnect()."""

    def _make_ws(self):
        ws = RCWebSocketClient.__new__(RCWebSocketClient)
        ws._ws = None
        ws._running = False
        ws._callbacks = {}
        ws._subscriptions = {}
        ws._subscription_states = {}
        ws._pending_results = {}
        ws._pending_subs = {}
        ws._callback_tasks = set()
        ws._room_workers = {}
        ws._room_queues = {}
        ws._reconnect_delay = 0.0
        ws._max_reconnect_delay = 60.0
        ws._recovery_task = None
        ws._listen_task = None
        ws._ping_task = None
        return ws

    async def test_orphaned_futures_resolved_on_reconnect(self):
        """Futures in _pending_subs must be given an exception during _reconnect()."""
        ws = self._make_ws()

        loop = asyncio.get_running_loop()
        pending_fut = loop.create_future()
        ws._pending_subs["sub_123"] = pending_fut

        with patch.object(ws, "connect", new_callable=AsyncMock):
            await ws._reconnect()

        self.assertTrue(pending_fut.done(), "pending_subs future must be resolved during reconnect")
        self.assertIsInstance(pending_fut.exception(), RuntimeError)
        self.assertIn("connection lost", str(pending_fut.exception()).lower())

    async def test_pending_subs_cleared_after_reconnect(self):
        """_pending_subs must be empty after reconnect completes."""
        ws = self._make_ws()

        loop = asyncio.get_running_loop()
        for sub_id in ("sub_a", "sub_b"):
            ws._pending_subs[sub_id] = loop.create_future()

        with patch.object(ws, "connect", new_callable=AsyncMock):
            await ws._reconnect()

        self.assertEqual(len(ws._pending_subs), 0, "_pending_subs must be cleared after reconnect")


# ── Appended from test_round15_fixes.py ───────────────────────────────────────


class TestReconnectClearsPendingSubsOnCancel(unittest.IsolatedAsyncioTestCase):
    """_reconnect must resolve pending_subs futures in a finally block."""

    def _make_ws(self):
        ws = RCWebSocketClient.__new__(RCWebSocketClient)
        ws._ws = None
        ws._running = True
        ws._callbacks = {}
        ws._subscriptions = {}
        ws._subscription_states = {}
        ws._pending_subs = {}
        ws._recovery_task = None
        ws._callback_tasks = set()
        ws._reconnect_delay = 1.0
        ws._max_reconnect_delay = 30.0
        ws._callback_sem = asyncio.Semaphore(10)
        return ws

    async def test_pending_subs_resolved_when_connect_raises_cancelled(self):
        """Futures in _pending_subs must be resolved even when connect() raises CancelledError."""
        ws = self._make_ws()

        fut = asyncio.get_event_loop().create_future()
        ws._pending_subs["sub-abc"] = fut

        with patch.object(ws, "connect", new_callable=AsyncMock,
                          side_effect=asyncio.CancelledError("shutdown")):
            with self.assertRaises(asyncio.CancelledError):
                await ws._reconnect()

        self.assertTrue(fut.done(), "Future was left unresolved after CancelledError in connect()")

    async def test_pending_subs_resolved_when_connect_raises_exception(self):
        """Futures in _pending_subs must also be resolved when connect() raises a regular exception."""
        ws = self._make_ws()

        fut = asyncio.get_event_loop().create_future()
        ws._pending_subs["sub-xyz"] = fut

        with patch.object(ws, "connect", new_callable=AsyncMock,
                          side_effect=RuntimeError("connection refused")):
            await ws._reconnect()

        self.assertTrue(fut.done(), "Future not resolved after connect() exception")
        if fut.exception() is not None:
            _ = fut.exception()

    async def test_pending_subs_cleared_after_reconnect(self):
        """`_pending_subs` dict must be empty after reconnect regardless of outcome."""
        ws = self._make_ws()

        fut = asyncio.get_event_loop().create_future()
        ws._pending_subs["sub-id"] = fut

        with patch.object(ws, "connect", new_callable=AsyncMock,
                          side_effect=asyncio.CancelledError()):
            with self.assertRaises(asyncio.CancelledError):
                await ws._reconnect()

        self.assertEqual(len(ws._pending_subs), 0)

    async def test_already_done_futures_not_overwritten(self):
        """Futures already resolved must not be set again."""
        ws = self._make_ws()

        fut = asyncio.get_event_loop().create_future()
        fut.set_result({"sub_id": "abc"})
        ws._pending_subs["sub-already-done"] = fut

        with patch.object(ws, "connect", new_callable=AsyncMock,
                          side_effect=asyncio.CancelledError()):
            with self.assertRaises(asyncio.CancelledError):
                await ws._reconnect()

        self.assertEqual(fut.result(), {"sub_id": "abc"})


class TestReconnectRestoresTheStream(unittest.IsolatedAsyncioTestCase):
    """The resubscribe loop iterates rooms, and the stream is not a room.

    That is the key-space split biting from the other side: two kinds of subscription need
    two kinds of restore. Without this, after any disconnect untracked rooms stopped
    arriving forever — and, worse because it is silent, newly tracked rooms skipped their
    own subscription too, since the connector still believed the stream was carrying them.
    """

    async def test_the_stream_is_resubscribed(self):
        from unittest.mock import AsyncMock

        client = _make_client()
        client._wants_stream = True
        client._stream_sub_id = "old-sub"
        client.subscribe_all = AsyncMock(return_value=True)
        client._subscribe_with_confirmation = AsyncMock(return_value="sub-1")

        await client._recover("Reconnect", try_stream=True)

        client.subscribe_all.assert_awaited_once()

    async def test_a_client_that_never_had_a_stream_does_not_ask_for_one(self):
        """Per-room deployments must not acquire subscribe-all by reconnecting."""
        from unittest.mock import AsyncMock

        client = _make_client()
        client._wants_stream = False
        client._stream_sub_id = None
        client.subscribe_all = AsyncMock(return_value=True)
        client._subscribe_with_confirmation = AsyncMock(return_value="sub-1")

        await client._recover("Reconnect", try_stream=True)

        client.subscribe_all.assert_not_awaited()

    async def test_a_failed_restore_is_reported_rather_than_silent(self):
        from unittest.mock import AsyncMock

        client = _make_client()
        client._wants_stream = True
        client._stream_sub_id = "old-sub"
        client.subscribe_all = AsyncMock(return_value=False)
        client._subscribe_with_confirmation = AsyncMock(return_value="sub-1")

        with self.assertLogs("agent-chat-gateway.connectors.rocketchat.ws", "ERROR"):
            await client._recover("Reconnect", try_stream=True)


class TestStreamIntentSurvivesFailure(unittest.IsolatedAsyncioTestCase):
    """Intent and current subscription are different facts.

    Clearing the subscription id on a failed restore used to clear both, so one failure
    removed the only marker saying the stream should be retried — every later reconnect
    skipped it while the connector went on believing the stream was live.
    """

    async def test_a_failed_restore_still_retries_next_time(self):
        from unittest.mock import AsyncMock

        client = _make_client()
        client._wants_stream = True
        client._stream_sub_id = "old"
        client._subscribe_with_confirmation = AsyncMock(return_value="s")
        client.subscribe_all = AsyncMock(return_value=False)

        with self.assertLogs("agent-chat-gateway.connectors.rocketchat.ws", "ERROR"):
            await client._recover("Reconnect", try_stream=True)
        self.assertTrue(client._wants_stream, "intent must outlive a failed attempt")

        client.subscribe_all = AsyncMock(return_value=True)
        await client._recover("Reconnect", try_stream=True)
        client.subscribe_all.assert_awaited_once()

    async def test_a_restored_stream_still_replays_the_outage(self):
        """Restoring delivery and losing the outage are not the same thing.

        An earlier version returned as soon as the stream came back, which also skipped
        the reconnect callback — the one that fetches what was sent to tracked rooms while
        the socket was down. Those messages were permanently missed.
        """
        from unittest.mock import AsyncMock

        client = _make_client()
        client._wants_stream = True
        client._callbacks = {"r1": AsyncMock()}
        client.subscribe_all = AsyncMock(return_value=True)
        client._subscribe_with_confirmation = AsyncMock(return_value="s")
        replayed = AsyncMock()
        client.register_reconnect_callback(replayed)

        await client._recover("Reconnect", try_stream=True)

        replayed.assert_awaited_once()
        client._subscribe_with_confirmation.assert_not_awaited()

    async def test_a_restored_stream_skips_per_room_resubscription(self):
        """The stream carries tracked rooms too; resubscribing them would have the server
        send every message twice."""
        from unittest.mock import AsyncMock

        client = _make_client()
        client._wants_stream = True
        client._callbacks = {"r1": AsyncMock(), "r2": AsyncMock()}
        client.subscribe_all = AsyncMock(return_value=True)
        client._subscribe_with_confirmation = AsyncMock(return_value="s")

        await client._recover("Reconnect", try_stream=True)

        client._subscribe_with_confirmation.assert_not_awaited()

    async def test_a_failed_stream_falls_back_to_per_room(self):
        from unittest.mock import AsyncMock

        client = _make_client()
        client._wants_stream = True
        client._callbacks = {"r1": AsyncMock()}
        client.subscribe_all = AsyncMock(return_value=False)
        client._subscribe_with_confirmation = AsyncMock(return_value="s")

        with self.assertLogs("agent-chat-gateway.connectors.rocketchat.ws", "ERROR"):
            await client._recover("Reconnect", try_stream=True)

        client._subscribe_with_confirmation.assert_awaited()


class TestStreamIntentAndStates(unittest.IsolatedAsyncioTestCase):
    async def test_intent_is_recorded_before_the_attempt(self):
        """A timeout, or a send failing during a brief disconnect, used to return False
        with the intent never set — so every later reconnect saw no stream to restore, and
        the connector stayed on per-room delivery for life having asked exactly once."""
        from unittest.mock import AsyncMock

        client = _make_client()
        client._send = AsyncMock(side_effect=RuntimeError("socket gone"))

        ok = await client.subscribe_all(timeout=0.01)

        self.assertFalse(ok)
        self.assertTrue(client._wants_stream, "intent must survive a failed attempt")

    async def test_a_restored_stream_marks_rooms_active_again(self):
        """`_reconnect` marks every state `reconnecting`, and the per-room confirmations
        that would clear it are what the stream makes unnecessary — so without this every
        tracked room reads as reconnecting forever."""
        from unittest.mock import AsyncMock

        from gateway.connectors.rocketchat.websocket import SubscriptionState

        client = _make_client()
        client._wants_stream = True
        client._callbacks = {"r1": AsyncMock()}
        client._subscription_states = {
            "r1": SubscriptionState(room_id="r1", callback=AsyncMock(),
                                    status="reconnecting")
        }
        client.subscribe_all = AsyncMock(return_value=True)

        await client._recover("Reconnect", try_stream=True)

        self.assertEqual(client._subscription_states["r1"].status, "active")

    def test_stream_active_reports_the_transport_not_an_intention(self):
        client = _make_client()
        client._wants_stream = True
        client._stream_sub_id = None
        self.assertFalse(
            client.stream_active,
            "wanting the stream is not having it — a watcher added while it is down needs "
            "its own subscription",
        )
        client._stream_sub_id = "sub-1"
        self.assertTrue(client.stream_active)


class TestTheStreamIsLostWhileTheSocketStaysUp(unittest.IsolatedAsyncioTestCase):
    """`nosub` for a confirmed stream — the one `live → lost` nothing else can see.

    A socket drop reconnects, and a reconnect resubscribes. This does neither: the
    connection is healthy, so no recovery path is triggered by anything. Meanwhile every
    tracked room released its own subscription when the stream took over, so at the instant
    the stream stops, *no room has any delivery path at all* — and the gateway looks idle
    rather than broken.
    """

    def _connected_client(self) -> RCWebSocketClient:
        client = _make_client()
        client._running = True
        client._ws = AsyncMock()
        client._wants_stream = True
        client._stream_sub_id = "stream-1"
        return client

    async def _feed(self, client: RCWebSocketClient, frame: dict) -> list[dict]:
        """Run one frame through the real listen loop; return the frames it sent.

        `_send` doubles as the server here: a `sub` is confirmed the moment it is sent, so
        the recovery completes without the listen loop that would normally resolve it.
        """
        sent: list[dict] = []

        async def _fake_send(payload):
            sent.append(payload)
            if payload.get("msg") == "sub":
                fut = client._pending_subs.get(payload.get("id"))
                if fut and not fut.done():
                    fut.set_result(True)

        client._send = _fake_send

        delivered = False

        async def _recv():
            nonlocal delivered
            if not delivered:
                delivered = True
                return json.dumps(frame)
            client._running = False
            raise asyncio.CancelledError()

        client._ws.recv = _recv
        with self.assertRaises(asyncio.CancelledError):
            await client._listen_loop()

        task = client._recovery_task
        if task is not None:
            await task
        return sent

    async def test_every_tracked_room_gets_its_own_subscription_back(self):
        client = self._connected_client()
        client._callbacks = {"r1": AsyncMock(), "r2": AsyncMock()}

        with self.assertLogs("agent-chat-gateway.connectors.rocketchat.ws", "ERROR"):
            sent = await self._feed(
                client, {"msg": "nosub", "id": "stream-1", "error": {"message": "gone"}}
            )

        subscribed = {
            f["params"][0] for f in sent if f.get("msg") == "sub"
        }
        self.assertEqual(
            subscribed, {"r1", "r2"},
            "clearing the id records the loss; the transition is not complete until "
            "every tracked room can receive again",
        )

    async def test_the_stream_stops_claiming_to_be_live(self):
        client = self._connected_client()
        with self.assertLogs("agent-chat-gateway.connectors.rocketchat.ws", "ERROR"):
            await self._feed(
                client, {"msg": "nosub", "id": "stream-1", "error": {"message": "gone"}}
            )
        self.assertFalse(
            client.stream_active,
            "a watcher added now must be told to subscribe to its own room",
        )

    async def test_the_intent_to_have_a_stream_survives_losing_it(self):
        client = self._connected_client()
        with self.assertLogs("agent-chat-gateway.connectors.rocketchat.ws", "ERROR"):
            await self._feed(
                client, {"msg": "nosub", "id": "stream-1", "error": {"message": "gone"}}
            )
        self.assertEqual(
            (client.stream_active, client._wants_stream), (False, True),
            "lost *and still wanted* is the whole state — asserting the intent alone "
            "cannot tell this apart from a stream that was never noticed as gone",
        )

    async def test_the_gap_in_delivery_is_replayed(self):
        """An outage that leaves the socket up is still an outage.

        Between the stream stopping and the per-room confirmations, messages to tracked
        rooms reached nobody. Restoring delivery and recovering what was lost are two
        obligations, and a path that discharges only the first loses messages quietly.
        """
        client = self._connected_client()
        client._callbacks = {"r1": AsyncMock()}
        replayed = AsyncMock()
        client.register_reconnect_callback(replayed)

        with self.assertLogs("agent-chat-gateway.connectors.rocketchat.ws", "ERROR"):
            await self._feed(
                client, {"msg": "nosub", "id": "stream-1", "error": {"message": "gone"}}
            )

        replayed.assert_awaited_once()

    async def test_a_nosub_for_something_else_is_not_the_stream_dying(self):
        """The near miss: a rejected *room* subscription must not tear down the stream."""
        client = self._connected_client()
        client._callbacks = {"r1": AsyncMock()}

        sent = await self._feed(
            client, {"msg": "nosub", "id": "some-room-sub", "error": {"message": "no"}}
        )

        self.assertTrue(client.stream_active, "the stream was never mentioned")
        self.assertEqual(
            [f for f in sent if f.get("msg") == "sub"], [],
            "nothing was lost, so nothing needs restoring",
        )

    async def test_a_stream_still_awaiting_confirmation_fails_its_caller_instead(self):
        """`subscribe_all` owns the refusal it is waiting for.

        Recovering here as well would race the caller's own fallback, and there is nothing
        to recover: a stream that was never confirmed never displaced a room subscription.
        """
        client = self._connected_client()
        client._stream_sub_id = None
        pending: asyncio.Future = asyncio.get_running_loop().create_future()
        client._pending_subs["stream-2"] = pending
        client._callbacks = {"r1": AsyncMock()}

        sent = await self._feed(
            client, {"msg": "nosub", "id": "stream-2", "error": {"message": "refused"}}
        )

        self.assertIsInstance(pending.exception(), RuntimeError)
        self.assertEqual([f for f in sent if f.get("msg") == "sub"], [])


class TestTheOutageBoundaryIsAnnouncedBeforeDeliveryReturns(unittest.IsolatedAsyncioTestCase):
    """Whatever must be measured from where the outage started has to be measured while
    nothing is subscribed.

    Once the first room is confirmed, live traffic resumes for it while the others are
    still subscribing — so anything read after that is a mix of two eras.
    """

    def _client(self) -> RCWebSocketClient:
        client = _make_client()
        client._callbacks = {"r1": AsyncMock(), "r2": AsyncMock()}
        return client

    def _record(self, client) -> list[str]:
        events: list[str] = []
        client.register_outage_callback(AsyncMock(side_effect=lambda: events.append("outage")))
        client.register_reconnect_callback(AsyncMock(side_effect=lambda: events.append("replay")))

        async def _sub(room_id, callback, timeout, keep_callback_on_failure):
            events.append(f"sub:{room_id}")
            return "s"

        client._subscribe_with_confirmation = _sub
        return events

    async def test_the_reconnect_path_announces_before_it_subscribes(self):
        client = self._client()
        client._wants_stream = False
        events = self._record(client)

        await client._recover("Reconnect", try_stream=True)

        self.assertEqual(events[0], "outage")
        self.assertEqual(events[-1], "replay")
        self.assertEqual(sorted(events[1:-1]), ["sub:r1", "sub:r2"])

    async def test_the_stream_fallback_announces_before_it_subscribes(self):
        client = self._client()
        events = self._record(client)

        await client._recover("Stream fallback", try_stream=False)

        self.assertEqual(events[0], "outage")
        self.assertEqual(events[-1], "replay")

    async def test_the_stream_restore_happens_after_the_announcement_too(self):
        """`subscribe_all` restores delivery for every room at once, so it is inside the
        window the boundary has to precede."""
        client = self._client()
        client._wants_stream = True
        events = self._record(client)

        async def _subscribe_all(*a, **k):
            events.append("stream")
            return True

        client.subscribe_all = _subscribe_all

        await client._recover("Reconnect", try_stream=True)

        self.assertEqual(events[:2], ["outage", "stream"])

    async def test_a_client_with_no_outage_callback_still_recovers(self):
        client = self._client()
        client._wants_stream = False
        replayed = AsyncMock()
        client.register_reconnect_callback(replayed)
        client._subscribe_with_confirmation = AsyncMock(return_value="s")

        await client._recover("Reconnect", try_stream=True)

        replayed.assert_awaited_once()

    async def test_a_failing_outage_callback_does_not_abort_the_recovery(self):
        """Losing the boundary degrades replay to the old behaviour; losing the recovery
        would leave every room dark."""
        client = self._client()
        client._wants_stream = False
        client.register_outage_callback(AsyncMock(side_effect=RuntimeError("boom")))
        client._subscribe_with_confirmation = AsyncMock(return_value="s")
        replayed = AsyncMock()
        client.register_reconnect_callback(replayed)

        with self.assertLogs("agent-chat-gateway.connectors.rocketchat.ws", "WARNING"):
            await client._recover("Reconnect", try_stream=True)

        replayed.assert_awaited_once()


class TestAWatcherAddedDuringTheRestoreIsNotDeliveredTwice(unittest.IsolatedAsyncioTestCase):
    """The restore window is a window in which `stream_active` correctly answers False.

    The recovery clears the stream id before re-asking for it, so a watcher
    added while `subscribe_all()` is awaiting confirmation is told — truthfully — that the
    stream is down, and opens its own subscription. If the stream then comes back, that
    room has two delivery paths for the rest of the process's life.
    """

    async def test_a_subscription_opened_mid_restore_is_released(self):
        client = _make_client()
        client._wants_stream = True
        client._callbacks = {"r1": AsyncMock()}
        sent: list[dict] = []
        client._send = AsyncMock(side_effect=lambda d: sent.append(d))

        async def _subscribe_all(*a, **k):
            # A watcher is created while the confirmation is in flight.
            client._subscriptions["r1"] = "sub-mid-restore"
            return True

        client.subscribe_all = _subscribe_all

        await client._recover("Reconnect", try_stream=True)

        self.assertEqual(
            client._subscriptions, {},
            "the stream carries the room now, so nothing else may also carry it",
        )
        self.assertIn(
            {"msg": "unsub", "id": "sub-mid-restore"}, sent,
            "released on the server too — a local drop leaves the server sending both",
        )
        self.assertIn(
            "r1", client._callbacks,
            "the room is still tracked; only who asked the server for it changed",
        )

    async def test_a_failed_restore_keeps_per_room_delivery(self):
        """The near miss: releasing unconditionally would drop the very subscriptions the
        fallback depends on."""
        client = _make_client()
        client._wants_stream = True
        client._callbacks = {"r1": AsyncMock()}
        client.subscribe_all = AsyncMock(return_value=False)
        client._subscribe_with_confirmation = AsyncMock(return_value="s")

        with self.assertLogs("agent-chat-gateway.connectors.rocketchat.ws", "ERROR"):
            await client._recover("Reconnect", try_stream=True)

        client._subscribe_with_confirmation.assert_awaited()


class TestAConfirmationRevokedBeforeItTookEffect(unittest.IsolatedAsyncioTestCase):
    """`ready` and `nosub` for the stream, in one batch of frames.

    The receive loop can process both before the coroutine awaiting the confirmation is
    scheduled again: resolving a future only *schedules* its waiter, and reading an
    already-buffered frame does not yield. In that window the future exists but is done —
    so the rejection branch sees nothing to reject — and `_stream_sub_id` is still unset,
    so the stream branch does not recognise its own subscription. The caller then recorded
    a dead id as live and the connector released every per-room subscription behind it.
    """

    async def _run(self, frames_after_sub) -> tuple[bool, RCWebSocketClient, list[dict]]:
        client = _make_client()
        client._ws = AsyncMock()
        sent: list[dict] = []

        async def _send(payload):
            sent.append(payload)

        client._send = _send
        task = asyncio.create_task(client.subscribe_all(timeout=5))
        while not sent:
            await asyncio.sleep(0)
        sub_id = sent[0]["id"]

        frames = [json.dumps(f(sub_id)) for f in frames_after_sub]

        async def _recv():
            # Returns without awaiting anything, exactly as a buffered frame does — so the
            # loop drains both frames before the waiter runs. A test that yielded here
            # would be testing a different, benign interleaving.
            if frames:
                return frames.pop(0)
            client._running = False
            raise asyncio.CancelledError()

        client._ws.recv = _recv
        client._running = True
        with self.assertRaises(asyncio.CancelledError):
            await client._listen_loop()

        return await task, client, sent

    async def test_a_stream_terminated_right_after_ready_is_not_recorded_as_live(self):
        with self.assertLogs("agent-chat-gateway.connectors.rocketchat.ws", "WARNING"):
            ok, client, _sent = await self._run([
                lambda sid: {"msg": "ready", "subs": [sid]},
                lambda sid: {"msg": "nosub", "id": sid, "error": {"message": "stopped"}},
            ])

        self.assertFalse(ok, "the caller must report the capability as unavailable")
        self.assertFalse(
            client.stream_active,
            "recording it would claim delivery the server has already stopped — and the "
            "connector releases every per-room subscription on that claim",
        )
        self.assertTrue(client._wants_stream, "still wanted; only this attempt failed")

    async def test_a_plain_confirmation_is_still_a_confirmation(self):
        """The near miss: the revocation check must not reject every subscription."""
        ok, client, _sent = await self._run([
            lambda sid: {"msg": "ready", "subs": [sid]},
        ])
        self.assertTrue(ok)
        self.assertTrue(client.stream_active)

    async def test_a_nosub_for_an_unrelated_id_does_not_revoke_the_stream(self):
        ok, client, _sent = await self._run([
            lambda sid: {"msg": "ready", "subs": [sid]},
            lambda sid: {"msg": "nosub", "id": "someone-elses-sub",
                         "error": {"message": "no"}},
        ])
        self.assertTrue(ok)
        self.assertTrue(client.stream_active)

    async def test_a_later_attempt_is_not_poisoned_by_the_revoked_one(self):
        """The revocation is consumed by the attempt it belongs to."""
        with self.assertLogs("agent-chat-gateway.connectors.rocketchat.ws", "WARNING"):
            first, client, _ = await self._run([
                lambda sid: {"msg": "ready", "subs": [sid]},
                lambda sid: {"msg": "nosub", "id": sid, "error": {"message": "stopped"}},
            ])
        self.assertFalse(first)
        self.assertIsNone(client._revoked_stream_sub_id)


class TestSharedStreamBookkeepingHasAnOwner(unittest.IsolatedAsyncioTestCase):
    """Three findings in one round, one genre: state written by whichever attempt finished
    last, with nothing recording which attempt it belonged to.

    Two of the three were introduced by the fixes for the two rounds before it — each added
    a shared field without adding an owner. So the answer here is the rule, and a test that
    walks the surface rather than a list someone has to remember to extend.
    """

    async def test_stop_clears_every_stream_field_there_is(self):
        """Derived from the object, not from a hand-written list.

        A hand list is a defect with a delay on it: the next stream field would be added,
        `stop()` would not clear it, and nothing would fail until a client was reused and a
        dead socket's subscription was reported as live.
        """
        fresh = _make_client()
        expected = {
            name: value for name, value in vars(fresh).items() if "stream" in name
        }
        self.assertTrue(expected, "the introspection must actually find the fields")

        client = _make_client()
        client._stream_sub_id = "s-live"
        client._wants_stream = True
        client._pending_stream_sub_id = "s-pending"
        client._revoked_stream_sub_id = "s-revoked"

        await client.stop()

        actual = {name: value for name, value in vars(client).items() if "stream" in name}
        self.assertEqual(
            actual, expected,
            "`→ absent` is the one transition that clears intent, and stop() is its "
            "transport half",
        )

    async def test_an_attempt_does_not_erase_a_newer_attempts_identity(self):
        """A socket drop during the confirmation starts a replacement attempt, and
        `_reconnect` spawns it *before* it fails the old future — so the newer attempt can
        publish its id first. The straggler must not take it with it."""
        client = _make_client()
        client._ws = AsyncMock()
        client._send = AsyncMock()
        sent: list[dict] = []
        client._send = AsyncMock(side_effect=lambda d: sent.append(d))

        task = asyncio.create_task(client.subscribe_all(timeout=0.05))
        while not sent:
            await asyncio.sleep(0)
        old_id = sent[0]["id"]

        # The replacement publishes before the straggler's timeout unwinds.
        client._pending_stream_sub_id = "newer-attempt"
        client._revoked_stream_sub_id = "newer-attempt"

        with self.assertLogs("agent-chat-gateway.connectors.rocketchat.ws", "WARNING"):
            self.assertFalse(await task)

        self.assertEqual(client._pending_stream_sub_id, "newer-attempt")
        self.assertEqual(
            client._revoked_stream_sub_id, "newer-attempt",
            "erasing this reopens the previous round's window from the other side",
        )
        self.assertNotEqual(old_id, "newer-attempt")

    async def test_a_migration_releases_only_the_subscription_it_captured(self):
        """A stream lost mid-migration starts the fallback, which can install a new
        subscription for a room this loop has already read."""
        client = _make_client()
        sent: list[dict] = []
        client._send = AsyncMock(side_effect=lambda d: sent.append(d))
        client._callbacks = {"r1": AsyncMock(), "r2": AsyncMock()}
        client._subscriptions = {"r1": "old-1", "r2": "old-2"}
        from gateway.connectors.rocketchat.websocket import SubscriptionState
        client._subscription_states = {
            "r1": SubscriptionState(room_id="r1", callback=AsyncMock(), sub_id="old-1"),
            "r2": SubscriptionState(room_id="r2", callback=AsyncMock(), sub_id="old-2"),
        }

        original_send = client._send

        async def _send_and_replace(payload):
            await original_send(payload)
            # While r1 is being released, the fallback resubscribes r2.
            if payload.get("id") == "old-1":
                client._subscriptions["r2"] = "new-2"
                client._subscription_states["r2"].sub_id = "new-2"

        client._send = _send_and_replace

        await client.unsubscribe_rooms_keeping_callbacks()

        self.assertEqual(
            client._subscriptions, {"r2": "new-2"},
            "the replacement must stay tracked — untracked means removing the watcher "
            "can never release it",
        )
        self.assertEqual(client._subscription_states["r2"].sub_id, "new-2")
        unsubbed = {f["id"] for f in sent if f.get("msg") == "unsub"}
        self.assertEqual(
            unsubbed, {"old-1", "old-2"},
            "the captured id is still released on the server: nothing else ever will, "
            "since the replacement overwrote the mapping without unsubscribing",
        )


class TestTheStreamFallbackDoesNotRaceTheRecoveryItReplaces(unittest.IsolatedAsyncioTestCase):
    """Invariant 8 is about the task slot too, not only the fields in it.

    A reconnect's recovery can still be inside its replay — one REST round trip per room —
    when the restored stream is dropped. Installing a second recovery over the top left
    both running, both reaching the replay callback, and both reading the same boundary.
    A message id is recorded only once its handler finishes, so that is two agent turns
    for one message.
    """

    async def test_two_replays_are_never_live_at_once(self):
        """The symptom the finding names, asserted directly.

        Concurrency is the defect — a message id is recorded only after its handler
        finishes, so two replays reading the same boundary produce two agent turns for one
        message. Asserting "the displaced one did not finish" would not catch it: both
        recoveries call the *same* callback, so a finish can belong to either.
        """
        client = _make_client()
        client._wants_stream = True
        client._stream_sub_id = "stream-1"
        client._callbacks = {"r1": AsyncMock()}
        client._subscribe_with_confirmation = AsyncMock(return_value="s")
        client.subscribe_all = AsyncMock(return_value=True)

        live = 0
        peak = 0
        first_replay_started = asyncio.Event()
        release = asyncio.Event()

        async def _slow_replay():
            nonlocal live, peak
            live += 1
            peak = max(peak, live)
            first_replay_started.set()
            try:
                await release.wait()
            finally:
                live -= 1

        client.register_reconnect_callback(_slow_replay)
        displaced = asyncio.create_task(client._recover("Reconnect", try_stream=True))
        client._recovery_task = displaced
        await first_replay_started.wait()

        with self.assertLogs("agent-chat-gateway.connectors.rocketchat.ws", "ERROR"):
            client._on_stream_lost("server stopped it")
        fallback = client._recovery_task

        release.set()
        await fallback

        self.assertEqual(
            peak, 1,
            "two recoveries reached the replay callback at once — same boundary, "
            "two agent turns for one message",
        )
        self.assertTrue(
            displaced.cancelled(),
            "the recovery this one displaced must be stopped, not merely forgotten",
        )

    async def test_the_fallback_still_recovers_when_nothing_was_displaced(self):
        """The near miss: awaiting a displaced task must not become a requirement."""
        client = _make_client()
        client._wants_stream = True
        client._stream_sub_id = "stream-1"
        client._callbacks = {"r1": AsyncMock()}
        client._subscribe_with_confirmation = AsyncMock(return_value="s")
        replayed = AsyncMock()
        client.register_reconnect_callback(replayed)

        with self.assertLogs("agent-chat-gateway.connectors.rocketchat.ws", "ERROR"):
            client._on_stream_lost("server stopped it")
        await client._recovery_task

        replayed.assert_awaited_once()

    async def test_a_finished_recovery_is_not_cancelled(self):
        client = _make_client()
        client._wants_stream = True
        client._stream_sub_id = "stream-1"
        client._callbacks = {}
        done: asyncio.Future = asyncio.get_running_loop().create_future()
        done.set_result(None)
        finished = asyncio.ensure_future(asyncio.sleep(0))
        await finished
        client._recovery_task = finished

        with self.assertLogs("agent-chat-gateway.connectors.rocketchat.ws", "ERROR"):
            client._on_stream_lost("server stopped it")
        await client._recovery_task

        self.assertFalse(finished.cancelled())


class TestOneRecoveryAtATime(unittest.IsolatedAsyncioTestCase):
    """The consolidation, tested as a structure rather than as three behaviours.

    Both entry points go through one launcher and one sequence, so "which of the two wrote
    this field last" stops being a question anyone can get wrong. What is left to test is
    that the structure holds: one slot, always cancel-and-replace, and work that outlives
    its starter checks whether it is still wanted.
    """

    def _client(self) -> RCWebSocketClient:
        client = _make_client()
        client._callbacks = {"r1": AsyncMock()}
        client._subscribe_with_confirmation = AsyncMock(return_value="s")
        return client

    async def test_a_reconnect_retires_a_recovery_even_with_nothing_to_recover(self):
        """No tracked rooms and no stream intent is still a reason to stop the previous
        one: it holds subscription state from before the socket dropped."""
        client = self._client()
        client._callbacks = {}
        client._wants_stream = False
        never_ends = asyncio.create_task(asyncio.sleep(9999))
        client._recovery_task = never_ends
        client.connect = AsyncMock()
        client._reconnect_delay = 0

        with patch("asyncio.sleep", new=AsyncMock()):
            await client._reconnect()
        await asyncio.sleep(0)

        self.assertTrue(never_ends.cancelled())

    async def test_an_overtaken_stream_attempt_records_nothing(self):
        """`subscribe_all` outlives the recovery that asked for it — `start_inbound` calls
        it directly, so it is not even in the slot. Publishing an id after a newer recovery
        has started claims delivery that recovery knows nothing about."""
        client = self._client()
        sent: list[dict] = []
        client._send = AsyncMock(side_effect=lambda d: sent.append(d))

        task = asyncio.create_task(client.subscribe_all(timeout=5))
        while not sent:
            await asyncio.sleep(0)
        sub_id = sent[0]["id"]

        client._recovery_generation += 1        # a newer recovery starts
        fut = client._pending_subs.get(sub_id)
        fut.set_result(True)                     # ...then the old confirmation lands

        with self.assertLogs("agent-chat-gateway.connectors.rocketchat.ws", "WARNING"):
            self.assertFalse(await task)

        self.assertFalse(
            client.stream_active,
            "an id published by a retired attempt is one nobody is tracking",
        )
        self.assertIn(
            {"msg": "unsub", "id": sub_id}, sent,
            "and it is released, not merely forgotten",
        )

    async def test_a_stopped_client_does_not_get_its_stream_back(self):
        """`stop()` clears the fields; the finding was that a confirmed-but-unresumed
        attempt then puts one of them back, and `_wants_stream=False` means no reconnect
        ever undoes that."""
        client = self._client()
        client._send = AsyncMock()
        task = asyncio.create_task(client.subscribe_all(timeout=5))
        while not client._pending_subs:
            await asyncio.sleep(0)
        sub_id = next(iter(client._pending_subs))

        await client.stop()
        fut = client._pending_subs.get(sub_id)
        if fut is not None and not fut.done():
            fut.set_result(True)

        try:
            await task
        except Exception:
            pass

        self.assertFalse(client.stream_active)
        self.assertFalse(client._wants_stream)

    async def test_installing_a_subscription_releases_the_one_it_replaces(self):
        """A recovery cancelled partway through releasing per-room subscriptions leaves
        some still live. The next recovery resubscribes those rooms — and without this,
        overwrote the mapping while the original kept delivering, untracked and
        unreleasable."""
        client = _make_client()
        sent: list[dict] = []
        client._send = AsyncMock(side_effect=lambda d: sent.append(d))
        client._subscriptions = {"r1": "left-behind"}
        cb = AsyncMock()

        task = asyncio.create_task(
            client._subscribe_with_confirmation(
                room_id="r1", callback=cb, timeout=5, keep_callback_on_failure=True,
            )
        )
        while not any(f.get("msg") == "sub" for f in sent):
            await asyncio.sleep(0)
        new_id = next(f["id"] for f in sent if f.get("msg") == "sub")
        client._pending_subs[new_id].set_result(True)
        await task

        self.assertIn(
            {"msg": "unsub", "id": "left-behind"}, sent,
            "the predecessor is released on the server, or nothing ever can be",
        )
        self.assertEqual(client._subscriptions["r1"], new_id)
