"""Tests for gateway/core/permission.py.

Covers:
  - PermissionRegistry: get(), expire_old(), pending_for_session(), cancel_session()
  - _format_request_msg: long-params truncation
  - _format_timeout_msg: basic content
  - ConnectorPermissionNotifier: no connector, success, retry-then-succeed, all-fail
  - PermissionBroker.request_permission: approved, denied, notification failure, timeout
"""

from __future__ import annotations

import asyncio
import time
import unittest
from unittest.mock import AsyncMock, MagicMock

from gateway.core.permission import (
    ConnectorPermissionNotifier,
    PermissionBroker,
    PermissionNotificationError,
    PermissionRegistry,
    PermissionRequest,
    _format_request_msg,
    _format_timeout_msg,
)

# ── Helpers ────────────────────────────────────────────────────────────────────

class _StubBroker(PermissionBroker):
    """Minimal concrete PermissionBroker for testing the shared request_permission logic."""

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass


def _make_req(
    request_id: str = "ab12",
    session_id: str = "ses_1",
    timeout_seconds: int = 300,
) -> PermissionRequest:
    return PermissionRequest(
        request_id=request_id,
        tool_name="bash",
        tool_input={"command": "ls"},
        room_id="room_1",
        session_id=session_id,
        timeout_seconds=timeout_seconds,
    )


# ── PermissionRegistry ─────────────────────────────────────────────────────────

class TestPermissionRegistry(unittest.IsolatedAsyncioTestCase):

    async def test_get_returns_registered_request(self):
        registry = PermissionRegistry()
        req = _make_req("abc1")
        registry.register(req)
        self.assertIs(registry.get("abc1"), req)

    async def test_get_returns_none_for_unknown_id(self):
        registry = PermissionRegistry()
        self.assertIsNone(registry.get("xxxx"))

    async def test_resolve_sets_future_result_and_removes_entry(self):
        registry = PermissionRegistry()
        req = _make_req("abc2")
        registry.register(req)
        result = registry.resolve("abc2", True)
        self.assertTrue(result)
        self.assertTrue(req.future.result())
        # Entry removed after resolve
        self.assertIsNone(registry.get("abc2"))

    async def test_resolve_returns_false_for_unknown_id(self):
        registry = PermissionRegistry()
        self.assertFalse(registry.resolve("nope", True))

    async def test_resolve_returns_false_if_already_resolved(self):
        registry = PermissionRegistry()
        req = _make_req("abc3")
        registry.register(req)
        registry.resolve("abc3", True)
        # Second resolve on same ID must return False
        self.assertFalse(registry.resolve("abc3", False))

    async def test_expire_old_auto_denies_timed_out_requests(self):
        registry = PermissionRegistry()
        req = _make_req("old1", timeout_seconds=1)
        req.created_at = time.monotonic() - 10  # backdate to force expiry
        registry.register(req)

        expired = registry.expire_old()

        self.assertEqual(len(expired), 1)
        self.assertIs(expired[0], req)
        self.assertFalse(req.future.result())  # auto-denied

    async def test_expire_old_does_not_expire_fresh_requests(self):
        registry = PermissionRegistry()
        req = _make_req("new1", timeout_seconds=300)
        registry.register(req)

        expired = registry.expire_old()

        self.assertEqual(len(expired), 0)
        self.assertFalse(req.future.done())

    async def test_expire_old_skips_already_resolved_requests(self):
        """A request already resolved by the owner is not counted as expired."""
        registry = PermissionRegistry()
        req = _make_req("res1", timeout_seconds=1)
        req.created_at = time.monotonic() - 10
        registry.register(req)
        registry.resolve("res1", True)  # owner responded before expiry check

        expired = registry.expire_old()

        self.assertEqual(len(expired), 0)

    async def test_pending_for_session_returns_only_matching(self):
        registry = PermissionRegistry()
        r1 = _make_req("r1", session_id="ses_A")
        r2 = _make_req("r2", session_id="ses_B")
        r3 = _make_req("r3", session_id="ses_A")
        for r in [r1, r2, r3]:
            registry.register(r)

        pending = registry.pending_for_session("ses_A")

        self.assertEqual(len(pending), 2)
        self.assertIn(r1, pending)
        self.assertIn(r3, pending)
        self.assertNotIn(r2, pending)

    async def test_cancel_session_auto_denies_all_pending(self):
        registry = PermissionRegistry()
        r1 = _make_req("r4", session_id="ses_C")
        r2 = _make_req("r5", session_id="ses_C")
        for r in [r1, r2]:
            registry.register(r)

        registry.cancel_session("ses_C")

        self.assertFalse(r1.future.result())
        self.assertFalse(r2.future.result())

    async def test_cancel_session_does_not_affect_other_sessions(self):
        registry = PermissionRegistry()
        r_other = _make_req("r6", session_id="ses_D")
        registry.register(r_other)

        registry.cancel_session("ses_C")  # different session

        self.assertFalse(r_other.future.done())

    async def test_resolve_with_matching_room_id_succeeds(self):
        """resolve() with from_room_id matching request.room_id resolves normally."""
        registry = PermissionRegistry()
        req = PermissionRequest(
            request_id="rm01",
            tool_name="Bash",
            tool_input={},
            room_id="room_A",
            session_id="ses_1",
        )
        registry.register(req)
        result = registry.resolve("rm01", True, from_room_id="room_A")
        self.assertTrue(result)
        self.assertTrue(req.future.result())
        self.assertIsNone(registry.get("rm01"))  # removed after resolve

    async def test_resolve_with_mismatched_room_id_rejected(self):
        """resolve() with from_room_id != request.room_id leaves request pending."""
        registry = PermissionRegistry()
        req = PermissionRequest(
            request_id="rm02",
            tool_name="Bash",
            tool_input={},
            room_id="room_A",
            session_id="ses_1",
        )
        registry.register(req)
        result = registry.resolve("rm02", True, from_room_id="room_B")
        self.assertFalse(result)
        self.assertFalse(req.future.done(), "Request must remain pending after cross-room rejection")
        self.assertIs(registry.get("rm02"), req, "Request must stay in registry")

    async def test_resolve_without_from_room_id_skips_room_check(self):
        """resolve() without from_room_id (legacy callers) still works."""
        registry = PermissionRegistry()
        req = PermissionRequest(
            request_id="rm03",
            tool_name="Bash",
            tool_input={},
            room_id="room_A",
            session_id="ses_1",
        )
        registry.register(req)
        result = registry.resolve("rm03", False)
        self.assertTrue(result)
        self.assertFalse(req.future.result())

    async def test_resolve_matching_thread_id_succeeds(self):
        """resolve() with matching from_thread_id resolves normally."""
        registry = PermissionRegistry()
        req = PermissionRequest(
            request_id="td01",
            tool_name="Bash",
            tool_input={},
            room_id="room_A",
            session_id="ses_1",
            thread_id="thread_1",
        )
        registry.register(req)
        result = registry.resolve("td01", True, from_room_id="room_A", from_thread_id="thread_1")
        self.assertTrue(result)
        self.assertTrue(req.future.result())
        self.assertIsNone(registry.get("td01"))

    async def test_resolve_mismatched_thread_id_rejected(self):
        """resolve() with from_thread_id != request.thread_id leaves request pending."""
        registry = PermissionRegistry()
        req = PermissionRequest(
            request_id="td02",
            tool_name="Bash",
            tool_input={},
            room_id="room_A",
            session_id="ses_1",
            thread_id="thread_1",
        )
        registry.register(req)
        result = registry.resolve("td02", True, from_room_id="room_A", from_thread_id="thread_2")
        self.assertFalse(result)
        self.assertFalse(req.future.done(), "Request must remain pending after cross-thread rejection")
        self.assertIs(registry.get("td02"), req, "Request must stay in registry")

    async def test_resolve_room_level_approve_allowed_for_threaded_request(self):
        """A non-threaded approve (from_thread_id=None) can resolve a threaded request.

        Owners may use the room-level input box even for requests that originated
        in a thread — blocking this would make approvals unnecessarily hard.
        """
        registry = PermissionRegistry()
        req = PermissionRequest(
            request_id="td03",
            tool_name="Bash",
            tool_input={},
            room_id="room_A",
            session_id="ses_1",
            thread_id="thread_1",
        )
        registry.register(req)
        # from_thread_id=None → room-level approve, must succeed
        result = registry.resolve("td03", True, from_room_id="room_A", from_thread_id=None)
        self.assertTrue(result)
        self.assertTrue(req.future.result())


# ── Message formatting ─────────────────────────────────────────────────────────

class TestFormatMessages(unittest.IsolatedAsyncioTestCase):

    async def test_format_request_msg_contains_request_id_and_tool_name(self):
        req = _make_req("ab12")
        msg = _format_request_msg(req)
        self.assertIn("ab12", msg)
        self.assertIn("bash", msg)

    async def test_format_request_msg_empty_tool_input_shows_none(self):
        req = PermissionRequest(
            request_id="ab12",
            tool_name="bash",
            tool_input={},
            room_id="room_1",
            session_id="ses_1",
        )
        msg = _format_request_msg(req)
        self.assertIn("(none)", msg)

    async def test_format_request_msg_long_params_truncated(self):
        """Params string longer than 200 chars must be truncated with '...'."""
        # Each entry is ~65 chars after repr[:60]; 4 entries * 65 + separators > 200
        req = PermissionRequest(
            request_id="cd34",
            tool_name="bash",
            tool_input={f"key{i:02d}": "x" * 55 for i in range(4)},
            room_id="room_1",
            session_id="ses_1",
        )
        msg = _format_request_msg(req)
        self.assertIn("...", msg)

    async def test_format_timeout_msg_contains_request_id_and_auto_denied(self):
        req = _make_req("ab12")
        msg = _format_timeout_msg(req)
        self.assertIn("ab12", msg)
        self.assertIn("auto-denied", msg)


# ── ConnectorPermissionNotifier ────────────────────────────────────────────────

class TestConnectorPermissionNotifier(unittest.IsolatedAsyncioTestCase):

    async def test_no_connector_returns_false(self):
        """Missing session→connector mapping returns False immediately."""
        notifier = ConnectorPermissionNotifier({})
        result = await notifier.notify("ses_1", "room_1", "hello")
        self.assertFalse(result)

    async def test_success_on_first_attempt_returns_true(self):
        connector = MagicMock()
        connector.send_text = AsyncMock()
        notifier = ConnectorPermissionNotifier({"ses_1": connector})

        result = await notifier.notify("ses_1", "room_1", "hello")

        self.assertTrue(result)
        connector.send_text.assert_called_once()

    async def test_retry_on_transient_error_then_succeed(self):
        """Fails on first attempt, succeeds on second — retry logic kicks in."""
        connector = MagicMock()
        call_count = 0

        async def flaky_send(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise ConnectionError("temporary")

        connector.send_text = flaky_send
        notifier = ConnectorPermissionNotifier({"ses_1": connector})

        result = await notifier.notify(
            "ses_1", "room_1", "hello", max_attempts=3, retry_delay=0
        )

        self.assertTrue(result)
        self.assertEqual(call_count, 2)

    async def test_all_attempts_fail_returns_false(self):
        """Exhausting all retries returns False."""
        connector = MagicMock()
        connector.send_text = AsyncMock(side_effect=ConnectionError("down"))
        notifier = ConnectorPermissionNotifier({"ses_1": connector})

        result = await notifier.notify(
            "ses_1", "room_1", "hello", max_attempts=3, retry_delay=0
        )

        self.assertFalse(result)
        self.assertEqual(connector.send_text.call_count, 3)

    async def test_passes_thread_id_to_send_text(self):
        """thread_id is forwarded to the underlying send_text call."""
        connector = MagicMock()
        connector.send_text = AsyncMock()
        notifier = ConnectorPermissionNotifier({"ses_1": connector})

        await notifier.notify("ses_1", "room_1", "msg", thread_id="thread_42")

        _, kwargs = connector.send_text.call_args
        self.assertEqual(kwargs.get("thread_id"), "thread_42")


# ── PermissionBroker.request_permission ────────────────────────────────────────

class TestPermissionBrokerRequestPermission(unittest.IsolatedAsyncioTestCase):
    """Tests for the shared request_permission() logic in PermissionBroker ABC."""

    def _make_broker(self, notify_result: bool = True, timeout_seconds: int = 60):
        registry = PermissionRegistry()
        notifier = MagicMock()
        notifier.notify = AsyncMock(return_value=notify_result)
        broker = _StubBroker(registry, notifier, timeout_seconds=timeout_seconds)
        return broker, registry, notifier

    async def test_approved_returns_true(self):
        broker, registry, _ = self._make_broker()

        async def approve_later():
            await asyncio.sleep(0)
            for req in list(registry._requests.values()):
                registry.resolve(req.request_id, True)

        task = asyncio.create_task(approve_later())
        result = await broker.request_permission("bash", {"cmd": "ls"}, "ses_1", "room_1")
        await task

        self.assertTrue(result)

    async def test_denied_returns_false(self):
        broker, registry, _ = self._make_broker()

        async def deny_later():
            await asyncio.sleep(0)
            for req in list(registry._requests.values()):
                registry.resolve(req.request_id, False)

        task = asyncio.create_task(deny_later())
        result = await broker.request_permission("bash", {"cmd": "ls"}, "ses_1", "room_1")
        await task

        self.assertFalse(result)

    async def test_notification_failure_raises_permission_notification_error(self):
        """When the notifier cannot deliver the message, raise PermissionNotificationError."""
        broker, _, _ = self._make_broker(notify_result=False)

        with self.assertRaises(PermissionNotificationError):
            await broker.request_permission("bash", {}, "ses_1", "room_1")

    async def test_notification_failure_does_not_leave_request_in_registry(self):
        """Failed notification must clean up the registry entry (resolve to False)."""
        broker, registry, _ = self._make_broker(notify_result=False)

        try:
            await broker.request_permission("bash", {}, "ses_1", "room_1")
        except PermissionNotificationError:
            pass

        self.assertEqual(len(registry._requests), 0)

    async def test_timeout_auto_denies_and_returns_false(self):
        """If the owner never responds within timeout, the request is auto-denied."""
        broker, _, notifier = self._make_broker(timeout_seconds=1, notify_result=True)
        broker._timeout_seconds = 0.05  # 50ms — fast for tests

        # Don't resolve the future — let it time out
        result = await broker.request_permission("bash", {}, "ses_1", "room_1")

        self.assertFalse(result)

    async def test_timeout_sends_timeout_notification(self):
        """A timeout posts a secondary 'auto-denied' notice to the room."""
        broker, _, notifier = self._make_broker(notify_result=True)
        broker._timeout_seconds = 0.05

        await broker.request_permission("bash", {}, "ses_1", "room_1")

        # First call: initial permission request; second call: timeout notice
        self.assertEqual(notifier.notify.call_count, 2)

    async def test_request_removed_from_registry_after_approval(self):
        """The registry entry is popped after the future resolves."""
        broker, registry, _ = self._make_broker()

        async def approve_later():
            await asyncio.sleep(0)
            for req in list(registry._requests.values()):
                registry.resolve(req.request_id, True)

        task = asyncio.create_task(approve_later())
        await broker.request_permission("bash", {}, "ses_1", "room_1")
        await task

        self.assertEqual(len(registry._requests), 0)

    async def test_thread_id_forwarded_to_notifier(self):
        """thread_id is passed through to the notifier for both request and timeout notices."""
        broker, _, notifier = self._make_broker(notify_result=True)
        broker._timeout_seconds = 0.05

        await broker.request_permission(
            "bash", {}, "ses_1", "room_1", thread_id="tid_42"
        )

        first_call_kwargs = notifier.notify.call_args_list[0].kwargs
        self.assertEqual(first_call_kwargs.get("thread_id"), "tid_42")


if __name__ == "__main__":
    unittest.main()
