"""Unit tests for gateway.core.expiry_task.run_expiry_task."""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from gateway.core.expiry_task import run_expiry_task


def _make_req(request_id: str = "req-1", tool_name: str = "Bash",
              session_id: str = "sess-1", room_id: str = "room-1",
              thread_id: str | None = None):
    req = MagicMock()
    req.request_id = request_id
    req.tool_name = tool_name
    req.session_id = session_id
    req.room_id = room_id
    req.thread_id = thread_id
    return req


class TestRunExpiryTask(unittest.IsolatedAsyncioTestCase):
    def _make_sleep(self, succeed_times: int = 1):
        """Return a fake sleep that succeeds N times then raises CancelledError."""
        call_count = []

        async def fake_sleep(n):
            call_count.append(n)
            if len(call_count) > succeed_times:
                raise asyncio.CancelledError

        return fake_sleep, call_count

    async def _run_one_iteration(
        self,
        expired_reqs: list,
        notify_result: bool = True,
    ):
        """Run the expiry task for exactly one sleep-then-sweep cycle.

        Sleep #1 succeeds → sweep runs → Sleep #2 raises CancelledError.
        """
        registry = MagicMock()
        registry.expire_old = MagicMock(return_value=expired_reqs)

        notifier = MagicMock()
        notifier.notify = AsyncMock(return_value=notify_result)

        fake_sleep, sleep_calls = self._make_sleep(succeed_times=1)

        with patch("gateway.core.expiry_task.asyncio.sleep", fake_sleep):
            with self.assertRaises(asyncio.CancelledError):
                await run_expiry_task(registry, notifier)

        return registry, notifier, sleep_calls

    async def test_sleeps_before_each_sweep(self):
        registry, notifier, sleep_calls = await self._run_one_iteration([])
        # 2 sleep calls: first succeeds (triggers sweep), second cancels
        self.assertGreaterEqual(len(sleep_calls), 1)
        self.assertEqual(sleep_calls[0], 30)  # _CHECK_INTERVAL

    async def test_no_expired_requests_notifier_not_called(self):
        registry, notifier, _ = await self._run_one_iteration([])
        notifier.notify.assert_not_called()

    async def test_expired_request_calls_notifier(self):
        req = _make_req(request_id="req-abc", tool_name="Write",
                        session_id="s1", room_id="r1", thread_id="t1")
        registry, notifier, _ = await self._run_one_iteration([req])
        notifier.notify.assert_called_once_with(
            "s1", "r1", unittest.mock.ANY, thread_id="t1"
        )

    async def test_multiple_expired_requests_all_notified(self):
        reqs = [_make_req(f"req-{i}") for i in range(3)]
        registry, notifier, _ = await self._run_one_iteration(reqs)
        self.assertEqual(notifier.notify.call_count, 3)

    async def test_notify_failure_logged_but_continues(self):
        req = _make_req()
        registry, notifier, _ = await self._run_one_iteration([req], notify_result=False)
        # No exception raised, task completed (cancelled) normally
        notifier.notify.assert_called_once()

    async def test_cancelled_error_propagates(self):
        """CancelledError must not be swallowed by the exception handler."""
        registry = MagicMock()
        registry.expire_old = MagicMock(return_value=[])
        notifier = MagicMock()

        # Cancel immediately on first sleep (before any sweep)
        fake_sleep, _ = self._make_sleep(succeed_times=0)

        with patch("gateway.core.expiry_task.asyncio.sleep", fake_sleep):
            with self.assertRaises(asyncio.CancelledError):
                await run_expiry_task(registry, notifier)

    async def test_generic_exception_does_not_crash_loop(self):
        """Non-CancelledError exceptions are caught so the loop keeps running.

        Sweep 1: expire_old() raises RuntimeError → caught, loop continues.
        Sweep 2: expire_old() returns [] → normal completion.
        Sleep 3: CancelledError stops the loop.
        """
        registry = MagicMock()
        call_count = 0

        def _expire_old():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("transient failure")
            return []

        registry.expire_old = MagicMock(side_effect=_expire_old)
        notifier = MagicMock()
        notifier.notify = AsyncMock(return_value=True)

        # Let 2 sweeps complete before cancelling
        fake_sleep, _ = self._make_sleep(succeed_times=2)

        with patch("gateway.core.expiry_task.asyncio.sleep", fake_sleep):
            with self.assertRaises(asyncio.CancelledError):
                await run_expiry_task(registry, notifier)

        self.assertEqual(call_count, 2)  # ran sweep twice (first raised, second returned [])


if __name__ == "__main__":
    unittest.main()
