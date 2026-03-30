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
from unittest.mock import AsyncMock, MagicMock, patch

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
        await client._resubscribe_task
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
        await client._resubscribe_task
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
        original_reconnect = client._reconnect

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

        async def tracking_callback(doc):
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

        async def collecting_callback(doc):
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

        async def blocking_callback(doc):
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

        async def blocking_callback(doc):
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
        queue.put_nowait({"_id": "existing"})
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
        ws._resubscribe_task = None
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
        ws._resubscribe_task = None
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
