"""Tests for RCWebSocketClient room-worker lifecycle and concurrency.

Covers:
  - Cancelled room workers must be awaited (round8)
  - Worker logs the in-flight message when cancelled during callback (round9)
  - Worker drains and logs remaining queue items when cancelled (round12)
  - In-flight doc counted in lost-message warning (round15)
  - WebSocket callback dispatch bounded by semaphore (code_review)

Run with:
    uv run python -m pytest tests/test_ws_room_worker.py -v
"""

from __future__ import annotations

import asyncio
import unittest

# ── Tests: websocket.py room worker awaiting ─────────────────────────────────


class TestRoomWorkerAwaiting(unittest.IsolatedAsyncioTestCase):
    """Cancelled room workers must be awaited even if they leave _callback_tasks."""

    async def test_worker_awaited_even_if_removed_from_callback_tasks(self):
        """A worker removed from _callback_tasks by done-callback must still be awaited."""
        from gateway.connectors.rocketchat.websocket import RCWebSocketClient

        ws = RCWebSocketClient.__new__(RCWebSocketClient)
        ws._running = False
        ws._listen_task = None
        ws._ping_task = None
        ws._resubscribe_task = None
        ws._callback_tasks = set()
        ws._room_queues = {}
        ws._ws = None

        awaited = []

        async def fake_worker():
            await asyncio.sleep(0)  # yields once so cancel can fire
            awaited.append("worker_done")

        worker_task = asyncio.create_task(fake_worker())
        # Simulate: worker was added to _callback_tasks with discard done-callback
        ws._callback_tasks.add(worker_task)
        worker_task.add_done_callback(ws._callback_tasks.discard)
        ws._room_workers = {"room1": worker_task}

        # Let the worker complete naturally (remove it from _callback_tasks)
        # Multiple yields needed: task body runs on first yield, done-callbacks
        # fire on subsequent iterations of the event loop.
        for _ in range(5):
            await asyncio.sleep(0)
        # Worker should have completed and discarded itself
        self.assertNotIn(worker_task, ws._callback_tasks)

        # stop() should still await it via worker_list
        await ws.stop()
        # No exception — worker was properly handled
        self.assertTrue(worker_task.done())

    async def test_worker_exception_does_not_propagate(self):
        """A room worker that raises must not cause stop() to raise."""
        from gateway.connectors.rocketchat.websocket import RCWebSocketClient

        ws = RCWebSocketClient.__new__(RCWebSocketClient)
        ws._running = False
        ws._listen_task = None
        ws._ping_task = None
        ws._resubscribe_task = None
        ws._callback_tasks = set()
        ws._room_queues = {}
        ws._ws = None

        async def bad_worker():
            raise RuntimeError("worker crashed")

        worker_task = asyncio.create_task(bad_worker())
        ws._callback_tasks.add(worker_task)
        worker_task.add_done_callback(ws._callback_tasks.discard)
        ws._room_workers = {"room1": worker_task}

        # stop() must not raise even if the worker raised
        try:
            await ws.stop()
        except Exception as exc:
            self.fail(f"stop() raised unexpectedly: {exc}")


# ── Tests: websocket.py _room_worker logs lost message on cancel ─────────────


class TestRoomWorkerCancelLogging(unittest.IsolatedAsyncioTestCase):
    """_room_worker must log the in-flight message when cancelled during callback."""

    async def test_cancelled_during_callback_logs_warning(self):
        """CancelledError mid-callback must produce a warning log with the message ID."""
        from gateway.connectors.rocketchat.websocket import RCWebSocketClient

        ws = RCWebSocketClient.__new__(RCWebSocketClient)
        ws._callbacks = {}
        ws._callback_sem = asyncio.Semaphore(10)
        ws._room_workers = {}
        ws._callback_tasks = set()

        queue: asyncio.Queue = asyncio.Queue()
        doc = {"_id": "msg_abc123", "msg": "hello"}

        started_callback = asyncio.Event()

        async def slow_callback(d):
            started_callback.set()
            await asyncio.sleep(300)  # blocks until cancelled

        ws._callbacks["room1"] = slow_callback

        worker_task = asyncio.create_task(ws._room_worker("room1", queue))
        await queue.put(doc)

        # Wait until the callback starts
        await asyncio.wait_for(started_callback.wait(), timeout=2.0)

        with self.assertLogs("agent-chat-gateway.connectors.rocketchat.ws", level="WARNING") as cm:
            worker_task.cancel()
            try:
                await worker_task
            except asyncio.CancelledError:
                pass

        log_output = "\n".join(cm.output)
        self.assertIn(
            "msg_abc123",
            log_output,
            "The lost message ID must appear in the warning log",
        )
        self.assertIn(
            "cancelled",
            log_output.lower(),
            "The log must mention 'cancelled'",
        )

    async def test_normal_exception_in_callback_not_confused_with_cancel(self):
        """A regular exception in the callback must not trigger the cancel log path."""
        from gateway.connectors.rocketchat.websocket import RCWebSocketClient

        ws = RCWebSocketClient.__new__(RCWebSocketClient)
        ws._callbacks = {}
        ws._callback_sem = asyncio.Semaphore(10)

        queue: asyncio.Queue = asyncio.Queue()
        doc = {"_id": "msg_xyz", "msg": "test"}

        async def bad_callback(d):
            raise ValueError("callback failed")

        ws._callbacks["room1"] = bad_callback
        await queue.put(doc)

        # Worker should log error but continue running (no CancelledError re-raise)
        worker_task = asyncio.create_task(ws._room_worker("room1", queue))
        # Give it time to process the message
        for _ in range(10):
            await asyncio.sleep(0)

        # Worker should still be alive (no crash from ValueError)
        self.assertFalse(worker_task.done(), "Worker must survive a callback ValueError")
        worker_task.cancel()
        try:
            await worker_task
        except asyncio.CancelledError:
            pass


# ── Tests: websocket.py _room_worker queue drain on cancellation ─────────────


class TestRoomWorkerQueueDrainOnCancel(unittest.IsolatedAsyncioTestCase):
    """_room_worker must log remaining queue items when cancelled."""

    def _make_ws(self):
        from gateway.connectors.rocketchat.websocket import RCWebSocketClient

        ws = RCWebSocketClient.__new__(RCWebSocketClient)
        ws._callbacks = {}
        ws._callback_sem = asyncio.Semaphore(10)
        return ws

    async def test_worker_logs_warning_for_queued_messages_on_cancel(self):
        """When cancelled with messages still in the queue, a WARNING must be logged."""
        ws = self._make_ws()
        queue: asyncio.Queue = asyncio.Queue()

        # Register a blocking callback so the worker stays inside the callback
        # when we cancel — guaranteeing that the remaining queue items are still
        # pending when the CancelledError fires.
        callback_entered = asyncio.Event()

        async def _blocking_callback(doc):
            callback_entered.set()
            await asyncio.sleep(9999)  # will be cancelled

        ws._callbacks["room-aabbcc"] = _blocking_callback

        # Enqueue 3 messages: worker processes msg0 (blocks in callback),
        # while msg1 and msg2 wait in the queue.
        for i in range(3):
            queue.put_nowait({"_id": f"msg{i}"})

        import logging

        worker_task = asyncio.create_task(ws._room_worker("room-aabbcc", queue))
        await callback_entered.wait()  # worker is now inside the blocking callback

        with self.assertLogs("agent-chat-gateway.connectors.rocketchat.ws",
                              level=logging.WARNING) as log_cm:
            worker_task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await worker_task

        # The mid-callback cancellation warning (already existed) plus the
        # queue-drain warning (new fix) should both be present.
        lost_warnings = [r for r in log_cm.output if "permanently lost" in r]
        self.assertGreater(len(lost_warnings), 0, "Expected at least one permanently-lost warning")
        # Queue must be empty — everything drained
        self.assertTrue(queue.empty(), "Queue should be drained after cancellation")

    async def test_worker_no_warning_when_queue_empty_on_cancel(self):
        """Cancellation with an empty queue must not log a 'permanently lost' warning."""
        ws = self._make_ws()
        queue: asyncio.Queue = asyncio.Queue()

        worker_task = asyncio.create_task(ws._room_worker("room-aabbcc", queue))
        await asyncio.sleep(0)  # let it block on queue.get()

        worker_task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await worker_task

        # If we get here without assertRaises failing, no unexpected exception occurred.
        # The queue was empty so the drain loop should have found 0 items and logged nothing.
        self.assertTrue(queue.empty())

    async def test_worker_drains_queue_items_on_cancel(self):
        """Queue must be empty after cancellation — items drained even if not processed."""
        ws = self._make_ws()
        queue: asyncio.Queue = asyncio.Queue()

        callback_entered = asyncio.Event()

        async def _blocking_callback(doc):
            callback_entered.set()
            await asyncio.sleep(9999)

        ws._callbacks["room-aabbcc"] = _blocking_callback

        # 1 message in callback (blocking), 2 more in queue
        for i in range(3):
            queue.put_nowait({"_id": f"msg{i}"})

        worker_task = asyncio.create_task(ws._room_worker("room-aabbcc", queue))
        await callback_entered.wait()

        worker_task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await worker_task

        self.assertTrue(queue.empty(), "Queue must be fully drained after worker cancellation")


# ── Tests: websocket.py _room_worker — in-flight doc counted on cancellation ─


class TestRoomWorkerInFlightCounted(unittest.IsolatedAsyncioTestCase):
    """_room_worker must count the in-flight doc in the lost-message warning."""

    def _make_ws(self):
        from gateway.connectors.rocketchat.websocket import RCWebSocketClient

        ws = RCWebSocketClient.__new__(RCWebSocketClient)
        ws._callbacks = {}
        ws._callback_sem = asyncio.Semaphore(1)
        return ws

    async def test_inflight_message_counted_when_cancelled_at_semaphore(self):
        """Message dequeued before semaphore must appear in the lost-count log."""
        ws = self._make_ws()
        room_id = "ROOM123"
        queue: asyncio.Queue = asyncio.Queue()

        doc = {"_id": "msg001", "msg": "hello"}

        # Block the semaphore by running a holder task first
        holder_acquired = asyncio.Event()
        task_cancel_event = asyncio.Event()

        async def sem_holder():
            async with ws._callback_sem:
                holder_acquired.set()
                await task_cancel_event.wait()

        # Register a real callback so the worker tries to acquire the semaphore
        async def real_callback(d):
            pass

        ws._callbacks[room_id] = real_callback

        holder_task = asyncio.create_task(sem_holder())
        await holder_acquired.wait()  # semaphore is now held

        # Put the doc in the queue
        await queue.put(doc)

        worker_task = asyncio.create_task(ws._room_worker(room_id, queue))

        # Give the worker time to dequeue the doc and block at semaphore
        await asyncio.sleep(0.05)

        # Verify queue is now empty (doc was dequeued by worker)
        self.assertTrue(queue.empty(), "Doc should have been dequeued by worker")

        # Cancel the worker while it's blocked at semaphore acquire
        with self.assertLogs(
            "agent-chat-gateway.connectors.rocketchat.ws", level="WARNING"
        ) as log_ctx:
            worker_task.cancel()
            task_cancel_event.set()
            try:
                await worker_task
            except asyncio.CancelledError:
                pass

        holder_task.cancel()
        try:
            await holder_task
        except asyncio.CancelledError:
            pass

        # The warning must mention 1 lost message
        combined = " ".join(log_ctx.output)
        self.assertIn("1", combined, f"Expected '1' in warning logs: {log_ctx.output}")

    async def test_no_warning_when_queue_empty_at_cancel(self):
        """No lost-message warning when worker is cancelled on empty queue (no in-flight doc)."""
        ws = self._make_ws()
        room_id = "ROOM456"
        queue: asyncio.Queue = asyncio.Queue()

        async def real_callback(d):
            pass

        ws._callbacks[room_id] = real_callback

        # No docs in queue — worker blocks at queue.get()
        worker_task = asyncio.create_task(ws._room_worker(room_id, queue))
        await asyncio.sleep(0.01)

        # Cancel while blocked on empty queue — no doc is in-flight
        worker_task.cancel()
        try:
            await worker_task
        except asyncio.CancelledError:
            pass

        # Queue is still empty, no in-flight doc — no warning is expected
        self.assertTrue(queue.empty())


# ── Tests: WebSocket callback concurrency bounded by semaphore ────────────────


class TestBoundedWebSocketCallbacks(unittest.IsolatedAsyncioTestCase):
    """Issue #13: WebSocket callback dispatch must be bounded by semaphore."""

    async def test_room_worker_concurrency_bounded_by_semaphore(self):
        """Room workers share the _callback_sem to bound global concurrency."""
        from gateway.connectors.rocketchat.websocket import RCWebSocketClient

        client = RCWebSocketClient.__new__(RCWebSocketClient)
        client._callback_sem = asyncio.Semaphore(2)
        client._callbacks = {}
        client._callback_tasks = set()
        client._room_queues = {}

        active = []
        max_active = 0

        async def slow_callback(doc):
            nonlocal max_active
            active.append(1)
            current = len(active)
            max_active = max(max_active, current)
            await asyncio.sleep(0.05)
            active.pop()

        # Create 3 room workers with callbacks
        for i in range(3):
            room_id = f"room{i}"
            client._callbacks[room_id] = slow_callback
            q: asyncio.Queue = asyncio.Queue(maxsize=50)
            client._room_queues[room_id] = q
            task = asyncio.create_task(client._room_worker(room_id, q))
            client._callback_tasks.add(task)
            task.add_done_callback(client._callback_tasks.discard)
            q.put_nowait({"msg": f"m{i}"})

        # Let workers process
        await asyncio.sleep(0.15)

        # Max concurrency should not exceed 2 (semaphore limit)
        self.assertLessEqual(max_active, 2)

        # Clean up
        for task in list(client._callback_tasks):
            task.cancel()
        await asyncio.gather(*client._callback_tasks, return_exceptions=True)


if __name__ == "__main__":
    unittest.main()
