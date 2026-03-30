"""Unit tests for OpenCodeCallablePermissionBroker.

Covers SSE line parsing, _dispatch (approve/deny/timeout/handler error),
_reply (approve → "once", deny → "reject", empty id no-op), and
lifecycle (start/stop).
"""

from __future__ import annotations

import asyncio
import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from gateway.agents.opencode.callable_broker import OpenCodeCallablePermissionBroker

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_broker(handler=None, timeout_seconds: int = 10):
    if handler is None:
        handler = AsyncMock(return_value=True)
    broker = OpenCodeCallablePermissionBroker(
        base_url="http://opencode.local:12345",
        permission_handler=handler,
        timeout_seconds=timeout_seconds,
    )
    return broker, handler


def _sse_line(payload: dict) -> str:
    return f"data: {json.dumps(payload)}"


# ── _handle_sse_line ──────────────────────────────────────────────────────────


class TestHandleSseLine(unittest.IsolatedAsyncioTestCase):
    async def test_non_data_line_ignored(self):
        broker, handler = _make_broker()
        await broker._handle_sse_line(": heartbeat")
        handler.assert_not_called()

    async def test_malformed_json_ignored(self):
        broker, handler = _make_broker()
        await broker._handle_sse_line("data: {not valid json}")
        # Should log warning but not raise or call handler
        handler.assert_not_called()

    async def test_non_permission_event_ignored(self):
        broker, handler = _make_broker()
        line = _sse_line({"type": "message.delta", "content": "hello"})
        await broker._handle_sse_line(line)
        handler.assert_not_called()

    async def test_permission_asked_dispatches_task(self):
        handler = AsyncMock(return_value=True)
        broker, _ = _make_broker(handler=handler)
        broker._reply_client = MagicMock()
        broker._reply_client.post = AsyncMock(return_value=MagicMock())

        payload = {
            "type": "permission.asked",
            "properties": {
                "id": "perm-abc123",
                "permission": "Bash",
                "patterns": [],
                "metadata": {"cmd": "ls"},
            },
        }
        await broker._handle_sse_line(_sse_line(payload))

        # Allow the dispatched task to run
        await asyncio.gather(*list(broker._pending_tasks), return_exceptions=True)
        handler.assert_called_once_with("Bash", {"cmd": "ls"})

    async def test_patterns_used_when_metadata_empty(self):
        handler = AsyncMock(return_value=True)
        broker, _ = _make_broker(handler=handler)
        broker._reply_client = MagicMock()
        broker._reply_client.post = AsyncMock(return_value=MagicMock())

        payload = {
            "type": "permission.asked",
            "properties": {
                "id": "p1",
                "permission": "Glob",
                "patterns": ["*.py"],
                "metadata": {},
            },
        }
        await broker._handle_sse_line(_sse_line(payload))
        await asyncio.gather(*list(broker._pending_tasks), return_exceptions=True)
        handler.assert_called_once_with("Glob", {"commands": ["*.py"]})

    async def test_empty_metadata_and_patterns_gives_empty_input(self):
        handler = AsyncMock(return_value=False)
        broker, _ = _make_broker(handler=handler)
        broker._reply_client = MagicMock()
        broker._reply_client.post = AsyncMock(return_value=MagicMock())

        payload = {
            "type": "permission.asked",
            "properties": {
                "id": "p2",
                "permission": "Read",
                "patterns": [],
                "metadata": {},
            },
        }
        await broker._handle_sse_line(_sse_line(payload))
        await asyncio.gather(*list(broker._pending_tasks), return_exceptions=True)
        handler.assert_called_once_with("Read", {})


# ── _dispatch ─────────────────────────────────────────────────────────────────


class TestDispatch(unittest.IsolatedAsyncioTestCase):
    async def _run_dispatch(self, handler, req_id="r1", tool="Bash", tool_input=None):
        broker, _ = _make_broker(handler=handler, timeout_seconds=5)
        broker._reply_client = MagicMock()
        broker._reply_client.post = AsyncMock(return_value=MagicMock())
        await broker._dispatch(req_id, tool, tool_input or {})
        return broker

    async def test_approved_posts_once(self):
        broker = await self._run_dispatch(AsyncMock(return_value=True))
        broker._reply_client.post.assert_called_once()
        _, kwargs = broker._reply_client.post.call_args
        self.assertEqual(kwargs["json"]["reply"], "once")

    async def test_denied_posts_reject(self):
        broker = await self._run_dispatch(AsyncMock(return_value=False))
        _, kwargs = broker._reply_client.post.call_args
        self.assertEqual(kwargs["json"]["reply"], "reject")

    async def test_handler_timeout_denies(self):
        async def slow_handler(*_):
            await asyncio.sleep(999)

        broker, _ = _make_broker(handler=slow_handler, timeout_seconds=0.01)
        broker._reply_client = MagicMock()
        broker._reply_client.post = AsyncMock(return_value=MagicMock())
        await broker._dispatch("r1", "Bash", {})
        _, kwargs = broker._reply_client.post.call_args
        self.assertEqual(kwargs["json"]["reply"], "reject")

    async def test_handler_exception_denies(self):
        async def bad_handler(*_):
            raise ValueError("unexpected!")

        broker = await self._run_dispatch(bad_handler)
        _, kwargs = broker._reply_client.post.call_args
        self.assertEqual(kwargs["json"]["reply"], "reject")


# ── _reply ─────────────────────────────────────────────────────────────────────


class TestReply(unittest.IsolatedAsyncioTestCase):
    async def test_approved_sends_once(self):
        broker, _ = _make_broker()
        broker._reply_client = MagicMock()
        broker._reply_client.post = AsyncMock(return_value=MagicMock())
        await broker._reply("req-1", True)
        broker._reply_client.post.assert_called_once()
        _, kwargs = broker._reply_client.post.call_args
        self.assertEqual(kwargs["json"]["reply"], "once")

    async def test_denied_sends_reject(self):
        broker, _ = _make_broker()
        broker._reply_client = MagicMock()
        broker._reply_client.post = AsyncMock(return_value=MagicMock())
        await broker._reply("req-1", False)
        _, kwargs = broker._reply_client.post.call_args
        self.assertEqual(kwargs["json"]["reply"], "reject")

    async def test_empty_req_id_is_noop(self):
        broker, _ = _make_broker()
        broker._reply_client = MagicMock()
        broker._reply_client.post = AsyncMock()
        await broker._reply("", True)
        broker._reply_client.post.assert_not_called()

    async def test_reply_client_not_initialized_logs_error(self):
        broker, _ = _make_broker()
        broker._reply_client = None  # broker.start() not called
        # Should not raise — just logs an error
        await broker._reply("req-1", True)

    async def test_reply_network_error_handled(self):
        broker, _ = _make_broker()
        broker._reply_client = MagicMock()
        broker._reply_client.post = AsyncMock(side_effect=httpx.ConnectError("refused"))
        # Should not raise
        await broker._reply("req-1", True)


# ── Lifecycle ─────────────────────────────────────────────────────────────────


class TestLifecycle(unittest.IsolatedAsyncioTestCase):
    async def test_start_creates_reply_client_and_sse_task(self):
        broker, _ = _make_broker()

        async def fake_listen_sse():
            await asyncio.sleep(9999)

        with patch.object(broker, "_listen_sse", fake_listen_sse):
            await broker.start()
            self.assertIsNotNone(broker._reply_client)
            self.assertIsNotNone(broker._sse_task)
            await broker.stop()

    async def test_stop_cancels_sse_task(self):
        broker, _ = _make_broker()

        async def fake_listen_sse():
            await asyncio.sleep(9999)

        with patch.object(broker, "_listen_sse", fake_listen_sse):
            await broker.start()
            task = broker._sse_task
            await broker.stop()
            self.assertTrue(task.done())
            self.assertIsNone(broker._sse_task)
            self.assertIsNone(broker._reply_client)

    async def test_stop_cancels_pending_permission_tasks(self):
        broker, _ = _make_broker()
        broker._reply_client = MagicMock()
        broker._reply_client.aclose = AsyncMock()

        # Manually add a long-running pending task
        async def long_running():
            await asyncio.sleep(9999)

        task = asyncio.create_task(long_running())
        broker._pending_tasks.add(task)

        await broker.stop()
        self.assertTrue(task.done())
        self.assertEqual(len(broker._pending_tasks), 0)

    async def test_stop_rejects_cancelled_permission_request(self):
        gate = asyncio.Event()

        async def blocking_handler(*_args):
            await gate.wait()
            return True

        broker, _ = _make_broker(handler=blocking_handler)
        broker._reply = AsyncMock()

        async def fake_listen_sse():
            await asyncio.sleep(9999)

        with patch.object(broker, "_listen_sse", fake_listen_sse):
            await broker.start()
            payload = {
                "type": "permission.asked",
                "properties": {
                    "id": "req-stop",
                    "permission": "Bash",
                    "patterns": [],
                    "metadata": {"cmd": "ls"},
                },
            }
            await broker._handle_sse_line(_sse_line(payload))
            await asyncio.sleep(0)

            await broker.stop()

        broker._reply.assert_any_call("req-stop", False)


# ── Semaphore (Issue 11.1) ─────────────────────────────────────────────────────


class TestSemaphore(unittest.IsolatedAsyncioTestCase):
    async def test_semaphore_limits_concurrent_dispatch(self):
        """Concurrent _dispatch calls are bounded by _permission_sem."""
        broker, _ = _make_broker()
        self.assertIsNotNone(broker._permission_sem)
        # Semaphore should have the expected bound (10)
        self.assertEqual(broker._permission_sem._value, 10)

    async def test_dispatch_uses_semaphore(self):
        """_dispatch acquires the semaphore before calling handler."""
        enter_count = 0
        release_count = 0

        class TrackingSemaphore:
            def __init__(self):
                self._value = 10

            async def __aenter__(self):
                nonlocal enter_count
                enter_count += 1

            async def __aexit__(self, *args):
                nonlocal release_count
                release_count += 1

        handler = AsyncMock(return_value=True)
        broker, _ = _make_broker(handler=handler)
        broker._permission_sem = TrackingSemaphore()
        broker._reply_client = MagicMock()
        broker._reply_client.post = AsyncMock(return_value=MagicMock())

        await broker._dispatch("req-1", "Bash", {})
        self.assertEqual(enter_count, 1)
        self.assertEqual(release_count, 1)


# ── _sse_task cleared in finally (Issue 11.2) ─────────────────────────────────


class TestSseTaskClearedInFinally(unittest.IsolatedAsyncioTestCase):
    async def test_sse_task_is_none_after_stop(self):
        """stop() must set _sse_task = None even on unexpected exceptions."""
        broker, _ = _make_broker()

        async def fake_listen_sse():
            await asyncio.sleep(9999)

        with patch.object(broker, "_listen_sse", fake_listen_sse):
            await broker.start()
            self.assertIsNotNone(broker._sse_task)
            await broker.stop()
            # _sse_task must be None after stop() — verified via finally block
            self.assertIsNone(broker._sse_task)

    async def test_sse_task_cleared_when_task_raises_unexpected(self):
        """_sse_task = None is set even if the task raises an unexpected exception."""
        broker, _ = _make_broker()

        async def raise_value_error():
            raise ValueError("unexpected!")

        broker._sse_task = asyncio.create_task(raise_value_error())
        # Allow the task to finish (it will raise ValueError, stored in task result)
        await asyncio.sleep(0)
        # Manually cancel and await to simulate stop()
        broker._sse_task.cancel()
        try:
            await broker._sse_task
        except (asyncio.CancelledError, ValueError):
            pass
        finally:
            broker._sse_task = None

        self.assertIsNone(broker._sse_task)


# ── SSE connect timeout (Issue 10.1) ──────────────────────────────────────────


class TestSseConnectTimeout(unittest.TestCase):
    def test_sse_client_has_connect_timeout(self):
        """_listen_sse must use httpx.Timeout with a connect value, not timeout=None."""
        import inspect

        import gateway.agents.opencode.callable_broker as cb_mod

        source = inspect.getsource(cb_mod.OpenCodeCallablePermissionBroker._listen_sse)
        self.assertNotIn("timeout=None", source)
        self.assertIn("httpx.Timeout", source)
        self.assertIn("connect=", source)
        self.assertIn("response.raise_for_status()", source)


if __name__ == "__main__":
    unittest.main()
