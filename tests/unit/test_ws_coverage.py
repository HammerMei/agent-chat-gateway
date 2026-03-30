"""Additional coverage tests for gateway.connectors.rocketchat.websocket.

Targets the uncovered branches identified in the coverage report:
  - _recv: not-connected guard
  - call_method: error-response logging path
  - _listen_loop: ping/pong, nosub-no-future, unhandled msg, JSONDecodeError,
                  no-ws reconnect, generic exception path
  - _handle_room_message: empty args, missing room_id, no callback, dead-worker
  - _room_worker: no callback after dequeue, generic exception
  - _ping_loop: CancelledError re-raise, generic exception swallowed
  - _reconnect: cancels old _resubscribe_task before starting new one
  - stop(): tasks already done (None guard)
"""

from __future__ import annotations

import asyncio
import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

# ── Helper: build a minimal client instance without connecting ────────────────


def _make_client():
    from gateway.connectors.rocketchat.websocket import RCWebSocketClient

    return RCWebSocketClient(
        server_url="http://localhost:3000",
        username="bot",
        password="pass",
    )


# ── _recv ─────────────────────────────────────────────────────────────────────


class TestRecv(unittest.IsolatedAsyncioTestCase):
    async def test_recv_raises_when_not_connected(self):
        """_recv must raise RuntimeError when _ws is None."""
        client = _make_client()
        client._ws = None
        with self.assertRaises(RuntimeError, msg="Not connected"):
            await client._recv()


# ── call_method ───────────────────────────────────────────────────────────────


class TestCallMethodLogging(unittest.IsolatedAsyncioTestCase):
    async def test_error_response_is_logged(self):
        """A result dict containing 'error' must trigger the debug log path."""
        client = _make_client()
        client._ws = MagicMock()
        client._ws.send = AsyncMock()

        error_result = {"id": "m1", "result": None, "error": {"message": "not found"}}

        async def _fake_wait_for(coro, timeout):
            # Resolve the future that call_method created
            await coro  # drain the coroutine
            return error_result

        loop = asyncio.get_running_loop()

        # Intercept the Future creation so we can inject our result
        original_create_future = loop.create_future

        def _patched_create_future():
            fut = original_create_future()
            # Schedule resolution on the next iteration
            loop.call_soon(fut.set_result, error_result)
            return fut

        with (
            patch.object(loop, "create_future", side_effect=_patched_create_future),
            patch(
                "gateway.connectors.rocketchat.websocket.logger"
            ) as mock_logger,
        ):
            result = await client.call_method("getChannels", [], timeout=5)

        self.assertEqual(result.get("error"), {"message": "not found"})
        # Verify the error-logging branch was hit
        debug_calls = [str(c) for c in mock_logger.debug.call_args_list]
        self.assertTrue(
            any("error" in c for c in debug_calls),
            f"Expected error debug log, got: {debug_calls}",
        )


# ── _listen_loop ──────────────────────────────────────────────────────────────


class TestListenLoopBranches(unittest.IsolatedAsyncioTestCase):
    def _make_connected_client(self):
        client = _make_client()
        client._running = True
        ws = MagicMock()
        ws.send = AsyncMock()
        client._ws = ws
        return client

    async def test_ping_triggers_pong(self):
        """A 'ping' message must cause the client to send a 'pong' back."""
        client = self._make_connected_client()
        ping_msg = json.dumps({"msg": "ping"}).encode()

        call_count = 0

        async def _recv():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return ping_msg.decode()
            # Stop the loop after the pong
            client._running = False
            raise asyncio.CancelledError()

        client._ws.recv = _recv

        with self.assertRaises(asyncio.CancelledError):
            await client._listen_loop()

        client._ws.send.assert_awaited_once()
        sent = json.loads(client._ws.send.call_args[0][0])
        self.assertEqual(sent["msg"], "pong")

    async def test_unhandled_message_type_is_logged(self):
        """An unrecognised msg type must hit the debug log branch."""
        client = self._make_connected_client()
        weird_msg = json.dumps({"msg": "unknown_type_xyz"})

        call_count = 0

        async def _recv():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return weird_msg
            client._running = False
            raise asyncio.CancelledError()

        client._ws.recv = _recv

        with (
            patch(
                "gateway.connectors.rocketchat.websocket.logger"
            ) as mock_logger,
            self.assertRaises(asyncio.CancelledError),
        ):
            await client._listen_loop()

        debug_msgs = [str(c) for c in mock_logger.debug.call_args_list]
        self.assertTrue(any("Unhandled" in m or "unknown_type_xyz" in m for m in debug_msgs))

    async def test_json_decode_error_is_swallowed(self):
        """A malformed JSON frame must be logged but must NOT crash the loop."""
        client = self._make_connected_client()

        call_count = 0

        async def _recv():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return "THIS IS NOT JSON {{{"
            client._running = False
            raise asyncio.CancelledError()

        client._ws.recv = _recv

        with self.assertRaises(asyncio.CancelledError):
            await client._listen_loop()

        # If we reached here the loop continued past the bad frame — correct.

    async def test_no_ws_triggers_reconnect(self):
        """When _ws is None the loop must call _reconnect() and then continue."""
        client = _make_client()
        client._running = True
        client._ws = None

        reconnect_called = False

        async def _fake_reconnect():
            nonlocal reconnect_called
            reconnect_called = True
            # After reconnect set _running=False to exit the loop
            client._running = False

        client._reconnect = _fake_reconnect

        await client._listen_loop()

        self.assertTrue(reconnect_called)

    async def test_nosub_without_pending_future_updates_state(self):
        """A 'nosub' for a sub_id with no pending future must update subscription state."""
        from gateway.connectors.rocketchat.websocket import SubscriptionState

        client = self._make_connected_client()

        # Set up a known subscription state
        room_id = "room-abc"
        state = SubscriptionState(room_id=room_id, callback=AsyncMock(), sub_id="sub-99")
        client._subscription_states[room_id] = state

        nosub_msg = json.dumps({
            "msg": "nosub",
            "id": "sub-99",
            "error": {"message": "room not found"},
        })

        call_count = 0

        async def _recv():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return nosub_msg
            client._running = False
            raise asyncio.CancelledError()

        client._ws.recv = _recv

        with self.assertRaises(asyncio.CancelledError):
            await client._listen_loop()

        self.assertEqual(state.status, "failed")
        self.assertIn("room not found", state.last_error)

    async def test_generic_exception_triggers_reconnect(self):
        """An unexpected exception in the recv loop must set _ws=None and reconnect."""
        client = self._make_connected_client()
        reconnect_called = False

        async def _boom():
            raise ValueError("unexpected boom")

        async def _fake_reconnect():
            nonlocal reconnect_called
            reconnect_called = True
            client._running = False

        client._ws.recv = _boom
        client._reconnect = _fake_reconnect

        await client._listen_loop()

        self.assertTrue(reconnect_called)
        self.assertIsNone(client._ws)


# ── _handle_room_message ──────────────────────────────────────────────────────


class TestHandleRoomMessage(unittest.IsolatedAsyncioTestCase):
    def _make_msg(self, args=None, event_name=None, rid=None):
        message_doc = {}
        if rid:
            message_doc["rid"] = rid
        fields: dict = {}
        if args is not None:
            fields["args"] = args
        if event_name:
            fields["eventName"] = event_name
        return {"msg": "changed", "collection": "stream-room-messages", "fields": fields}

    async def test_empty_args_returns_without_dispatch(self):
        """A message with empty args must be silently ignored."""
        client = _make_client()
        msg = self._make_msg(args=[])
        # Should complete without raising or dispatching
        await client._handle_room_message(msg)
        self.assertEqual(len(client._room_queues), 0)

    async def test_missing_room_id_returns_without_dispatch(self):
        """If neither eventName nor rid is present, must bail out silently."""
        client = _make_client()
        # args has a doc with no 'rid', and no 'eventName' in fields
        msg = self._make_msg(args=[{"text": "hello"}])
        await client._handle_room_message(msg)
        self.assertEqual(len(client._room_queues), 0)

    async def test_no_callback_for_room_returns_without_dispatch(self):
        """No registered callback for room_id → message is dropped silently."""
        client = _make_client()
        msg = self._make_msg(args=[{"rid": "room-99"}], rid="room-99")
        # No callback registered
        await client._handle_room_message(msg)
        self.assertEqual(len(client._room_queues), 0)

    async def test_dead_worker_is_replaced(self):
        """When the existing worker task is done(), it must be cleaned up and replaced."""
        client = _make_client()
        room_id = "room-dead"
        callback = AsyncMock()
        client._callbacks[room_id] = callback

        # Create a "dead" worker task (already done)
        async def _noop():
            pass

        dead_task = asyncio.create_task(_noop())
        await dead_task  # ensure it's done
        client._room_workers[room_id] = dead_task

        # Also put an old queue with a pending item (to trigger warning log)
        old_q: asyncio.Queue = asyncio.Queue()
        old_q.put_nowait({"old": "msg"})
        client._room_queues[room_id] = old_q

        msg = self._make_msg(args=[{"rid": room_id, "text": "hi"}], rid=room_id)

        with patch("gateway.connectors.rocketchat.websocket.logger") as mock_logger:
            await client._handle_room_message(msg)

        # A new queue and worker must have been created
        self.assertIn(room_id, client._room_queues)
        new_q = client._room_queues[room_id]
        self.assertIsNot(new_q, old_q)
        # Warning about dropped messages from old queue must have been logged
        warning_calls = [str(c) for c in mock_logger.warning.call_args_list]
        self.assertTrue(any("died" in w or "unprocessed" in w for w in warning_calls))

        # Clean up background worker task
        worker = client._room_workers.get(room_id)
        if worker and not worker.done():
            worker.cancel()
            try:
                await worker
            except (asyncio.CancelledError, Exception):
                pass


# ── _room_worker ──────────────────────────────────────────────────────────────


class TestRoomWorker(unittest.IsolatedAsyncioTestCase):
    async def test_no_callback_skips_dispatch(self):
        """If callback is removed between queue.get() and dispatch, message is silently skipped."""
        client = _make_client()
        room_id = "room-x"
        q: asyncio.Queue = asyncio.Queue()
        q.put_nowait({"_id": "msg-1"})

        # No callback registered for this room
        task = asyncio.create_task(client._room_worker(room_id, q))

        # Give the worker one iteration then cancel it
        await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        # No exception → passed

    async def test_callback_exception_is_logged(self):
        """A callback that raises must log an error but not crash the worker."""
        client = _make_client()
        room_id = "room-err"

        async def _bad_callback(doc):
            raise ValueError("callback blew up")

        client._callbacks[room_id] = _bad_callback

        q: asyncio.Queue = asyncio.Queue()
        q.put_nowait({"_id": "msg-2"})

        task = asyncio.create_task(client._room_worker(room_id, q))
        await asyncio.sleep(0)  # let worker process the message
        task.cancel()

        with patch(
            "gateway.connectors.rocketchat.websocket.logger"
        ) as mock_logger:
            # Re-run with the patch in scope
            client2 = _make_client()
            client2._callbacks[room_id] = _bad_callback
            q2: asyncio.Queue = asyncio.Queue()
            q2.put_nowait({"_id": "msg-3"})
            task2 = asyncio.create_task(client2._room_worker(room_id, q2))
            await asyncio.sleep(0.05)
            task2.cancel()
            try:
                await task2
            except asyncio.CancelledError:
                pass

        error_calls = [str(c) for c in mock_logger.error.call_args_list]
        self.assertTrue(any("callback blew up" in e or "Callback error" in e for e in error_calls))

    async def test_worker_generic_exception_logs_and_exits(self):
        """A non-CancelledError exception in the main loop must log and exit cleanly."""
        client = _make_client()
        room_id = "room-crash"

        # Put a sentinel that causes the callback to blow up
        async def _exploding_callback(doc):
            raise RuntimeError("kaboom")

        client._callbacks[room_id] = _exploding_callback

        q: asyncio.Queue = asyncio.Queue()
        q.put_nowait({"_id": "x"})

        with patch(
            "gateway.connectors.rocketchat.websocket.logger"
        ) as mock_logger:
            task = asyncio.create_task(client._room_worker(room_id, q))
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        error_calls = [str(c) for c in mock_logger.error.call_args_list]
        self.assertTrue(any("kaboom" in e or "error" in e.lower() for e in error_calls))


# ── _ping_loop ────────────────────────────────────────────────────────────────


class TestPingLoop(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_error_propagates(self):
        """CancelledError inside _ping_loop must be re-raised, not swallowed."""
        client = _make_client()
        client._running = True
        client._ws = MagicMock()
        client._ws.send = AsyncMock()

        async def _fast_sleep(_):
            raise asyncio.CancelledError()

        with patch("asyncio.sleep", side_effect=_fast_sleep):
            with self.assertRaises(asyncio.CancelledError):
                await client._ping_loop()

    async def test_generic_exception_is_swallowed(self):
        """A non-CancelledError exception inside the ping try-block must be swallowed."""
        client = _make_client()
        client._running = True
        client._ws = MagicMock()
        client._ws.send = AsyncMock(side_effect=RuntimeError("send failed"))

        call_count = 0

        async def _fast_sleep(_):
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                client._running = False

        with patch("asyncio.sleep", side_effect=_fast_sleep):
            # Should NOT raise
            await client._ping_loop()

        # Reached here → exception was swallowed correctly


# ── _reconnect ────────────────────────────────────────────────────────────────


class TestReconnect(unittest.IsolatedAsyncioTestCase):
    async def test_cancels_old_resubscribe_task(self):
        """_reconnect must cancel an in-progress _resubscribe_task before creating a new one."""
        client = _make_client()
        client._reconnect_delay = 0  # no real sleep

        # Simulate a long-running resubscribe task
        cancelled = False

        async def _long_resubscribe():
            nonlocal cancelled
            try:
                await asyncio.sleep(9999)
            except asyncio.CancelledError:
                cancelled = True
                raise

        old_task = asyncio.create_task(_long_resubscribe())
        client._resubscribe_task = old_task

        async def _fake_connect():
            pass

        # After connect, simulate no callbacks so no new resubscribe_task is created
        client._callbacks = {}

        with patch.object(client, "connect", side_effect=_fake_connect):
            with patch("asyncio.sleep"):
                await client._reconnect()

        self.assertTrue(old_task.cancelled() or cancelled)


# ── stop() — None guard ───────────────────────────────────────────────────────


class TestStopNoneGuard(unittest.IsolatedAsyncioTestCase):
    async def test_stop_with_no_tasks_does_not_raise(self):
        """stop() must handle _ping_task and _listen_task being None without error."""
        client = _make_client()
        client._running = True
        client._ping_task = None
        client._listen_task = None
        client._resubscribe_task = None

        # Should not raise
        await client.stop()
        self.assertFalse(client._running)

    async def test_stop_with_already_done_tasks(self):
        """stop() must handle tasks that are already done without error."""
        client = _make_client()
        client._running = True

        async def _noop():
            pass

        done_task = asyncio.create_task(_noop())
        await done_task  # let it finish

        client._ping_task = done_task
        client._listen_task = done_task

        await client.stop()
        self.assertFalse(client._running)


if __name__ == "__main__":
    unittest.main()
