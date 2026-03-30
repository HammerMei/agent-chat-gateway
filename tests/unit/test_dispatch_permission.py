"""Tests for MessageDispatcher permission command interception edge cases.

Covers:
  - approve/deny commands: 4-char ID → success reply
  - ID too short (< 4 chars) → error reply with "Invalid ID" message
  - ID too long (> 4 chars) → error reply
  - Unknown ID (4-char but not in registry) → "No pending request" reply
  - Uppercase APPROVE / DENY → correctly normalised by IGNORECASE regex
  - Non-owner messages: permission regex not evaluated (fan-out instead)
  - dispatch() returns True for permission commands (no watermark hold)
  - dispatch() watermark semantics: any() vs all() for multi-processor rooms

Run with:
    uv run python -m pytest tests/test_dispatch_permission.py -v
"""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock

from gateway.core.connector import IncomingMessage, Room, User, UserRole
from gateway.core.dispatch import MessageDispatcher
from gateway.core.permission import PermissionRegistry, PermissionRequest

# ── Helpers ────────────────────────────────────────────────────────────────────


def _make_msg(
    text: str,
    room_id: str = "room_1",
    role: UserRole = UserRole.OWNER,
) -> IncomingMessage:
    room = Room(id=room_id, name="general", type="dm")
    sender = User(id="u1", username="owner", display_name="Owner")
    return IncomingMessage(
        id="msg_1",
        timestamp="1",
        room=room,
        sender=sender,
        role=role,
        text=text,
    )


def _make_dispatcher(registry: PermissionRegistry | None = None) -> tuple[MessageDispatcher, MagicMock]:
    connector = MagicMock()
    connector.send_text = AsyncMock()
    return MessageDispatcher(connector, registry or PermissionRegistry()), connector


# ── Permission command interception ───────────────────────────────────────────


class TestPermissionCommandInterception(unittest.IsolatedAsyncioTestCase):
    """approve/deny <id> commands from owners are intercepted before fan-out."""

    async def test_approve_4char_id_resolves_request(self):
        """approve <4-char-id> resolves the pending request as approved."""
        registry = PermissionRegistry()
        dispatcher, connector = _make_dispatcher(registry)

        req = PermissionRequest(
            request_id="a3k9",
            tool_name="Bash",
            tool_input={},
            room_id="room_1",
            session_id="ses_1",
        )
        registry.register(req)

        msg = _make_msg("approve a3k9")
        result = await dispatcher.dispatch(msg)

        self.assertTrue(result, "Permission commands always return True")
        self.assertTrue(req.future.done())
        self.assertTrue(req.future.result(), "Request should be approved")
        connector.send_text.assert_awaited_once()
        reply_text = connector.send_text.call_args[0][1].text
        self.assertIn("approved", reply_text)
        self.assertIn("a3k9", reply_text)

    async def test_deny_4char_id_resolves_request_as_denied(self):
        """deny <4-char-id> resolves the pending request as denied."""
        registry = PermissionRegistry()
        dispatcher, connector = _make_dispatcher(registry)

        req = PermissionRequest(
            request_id="b7z2",
            tool_name="Write",
            tool_input={},
            room_id="room_1",
            session_id="ses_1",
        )
        registry.register(req)

        msg = _make_msg("deny b7z2")
        await dispatcher.dispatch(msg)

        self.assertTrue(req.future.done())
        self.assertFalse(req.future.result(), "Request should be denied")
        reply_text = connector.send_text.call_args[0][1].text
        self.assertIn("denied", reply_text)

    async def test_uppercase_APPROVE_normalised(self):
        """APPROVE A3K9 (uppercase) is accepted due to IGNORECASE regex."""
        registry = PermissionRegistry()
        dispatcher, connector = _make_dispatcher(registry)

        req = PermissionRequest(
            request_id="a3k9",
            tool_name="Read",
            tool_input={},
            room_id="room_1",
            session_id="ses_1",
        )
        registry.register(req)

        msg = _make_msg("APPROVE A3K9")
        result = await dispatcher.dispatch(msg)

        self.assertTrue(result)
        self.assertTrue(req.future.result(), "Uppercase APPROVE should work")

    async def test_mixed_case_deny_normalised(self):
        """Deny with mixed-case ID is correctly normalised."""
        registry = PermissionRegistry()
        dispatcher, connector = _make_dispatcher(registry)

        req = PermissionRequest(
            request_id="ff11",
            tool_name="Bash",
            tool_input={},
            room_id="room_1",
            session_id="ses_1",
        )
        registry.register(req)

        msg = _make_msg("Deny FF11")
        await dispatcher.dispatch(msg)
        self.assertFalse(req.future.result())

    async def test_id_too_short_returns_error_reply(self):
        """approve <3-char-id> triggers 'Invalid ID' error reply."""
        registry = PermissionRegistry()
        dispatcher, connector = _make_dispatcher(registry)

        msg = _make_msg("approve abc")
        result = await dispatcher.dispatch(msg)

        self.assertTrue(result)  # command processed, watermark advances
        reply_text = connector.send_text.call_args[0][1].text
        self.assertIn("Invalid ID", reply_text)
        self.assertIn("abc", reply_text)

    async def test_id_too_long_returns_error_reply(self):
        """approve <5-char-id> triggers 'Invalid ID' error reply."""
        registry = PermissionRegistry()
        dispatcher, connector = _make_dispatcher(registry)

        msg = _make_msg("approve abcde")
        await dispatcher.dispatch(msg)

        reply_text = connector.send_text.call_args[0][1].text
        self.assertIn("Invalid ID", reply_text)

    async def test_unknown_4char_id_returns_no_pending_reply(self):
        """approve <valid-4-char-id> that is not in registry → 'No pending' reply."""
        registry = PermissionRegistry()
        dispatcher, connector = _make_dispatcher(registry)

        msg = _make_msg("approve zzzz")
        await dispatcher.dispatch(msg)

        reply_text = connector.send_text.call_args[0][1].text
        self.assertIn("No pending", reply_text)
        self.assertIn("zzzz", reply_text)

    async def test_non_owner_message_not_intercepted(self):
        """Permission commands from non-owner (GUEST) users are NOT intercepted."""
        registry = PermissionRegistry()
        dispatcher, connector = _make_dispatcher(registry)

        req = PermissionRequest(
            request_id="a1b2",
            tool_name="Bash",
            tool_input={},
            room_id="room_1",
            session_id="ses_1",
        )
        registry.register(req)

        # Guest sends the command — should NOT be intercepted
        msg = _make_msg("approve a1b2", role=UserRole.GUEST)
        # No processor registered, so dispatch returns False (no fan-out destination)
        result = await dispatcher.dispatch(msg)

        self.assertFalse(result)
        # Request must NOT be resolved
        self.assertFalse(req.future.done(), "Guest command must not resolve the request")
        connector.send_text.assert_not_awaited()

    async def test_permission_command_returns_true_even_without_processors(self):
        """dispatch() returns True for permission commands regardless of processor state."""
        registry = PermissionRegistry()
        dispatcher, connector = _make_dispatcher(registry)
        # No processors registered

        req = PermissionRequest(
            request_id="c5d6",
            tool_name="Read",
            tool_input={},
            room_id="room_1",
            session_id="ses_1",
        )
        registry.register(req)

        result = await dispatcher.dispatch(_make_msg("approve c5d6"))
        self.assertTrue(result)


# ── Watermark semantics (any vs all) ─────────────────────────────────────────


class TestDispatchWatermarkSemantics(unittest.IsolatedAsyncioTestCase):
    """dispatch() uses any(results): watermark advances if at least one processor accepts."""

    def _make_processor(self, accepts: bool) -> MagicMock:
        proc = MagicMock()
        proc.enqueue = AsyncMock(return_value=accepts)
        proc.is_accepting = accepts
        return proc

    async def test_both_accept_returns_true(self):
        dispatcher, _ = _make_dispatcher()
        p1 = self._make_processor(True)
        p2 = self._make_processor(True)
        dispatcher.add_processor("room_1", p1)
        dispatcher.add_processor("room_1", p2)

        result = await dispatcher.dispatch(_make_msg("hello", role=UserRole.GUEST))
        self.assertTrue(result)

    async def test_one_accepts_one_drops_returns_true(self):
        """If p1 accepts and p2 drops (queue full), watermark still advances."""
        dispatcher, _ = _make_dispatcher()
        p1 = self._make_processor(True)
        p2 = self._make_processor(False)
        dispatcher.add_processor("room_1", p1)
        dispatcher.add_processor("room_1", p2)

        result = await dispatcher.dispatch(_make_msg("hello", role=UserRole.GUEST))
        self.assertTrue(result)

    async def test_both_drop_returns_false(self):
        """If ALL processors drop the message, watermark must NOT advance."""
        dispatcher, _ = _make_dispatcher()
        p1 = self._make_processor(False)
        p2 = self._make_processor(False)
        dispatcher.add_processor("room_1", p1)
        dispatcher.add_processor("room_1", p2)

        result = await dispatcher.dispatch(_make_msg("hello", role=UserRole.GUEST))
        self.assertFalse(result)

    async def test_no_processors_returns_false(self):
        """No processors registered for the room → dispatch returns False."""
        dispatcher, _ = _make_dispatcher()
        result = await dispatcher.dispatch(_make_msg("hello", role=UserRole.GUEST))
        self.assertFalse(result)


if __name__ == "__main__":
    unittest.main()


# ── Appended from test_code_review_fixes.py ───────────────────────────────────

from gateway.agents import AgentBackend as _AgentBackend  # noqa: E402
from gateway.agents.response import AgentResponse as _AgentResponse  # noqa: E402
from tests.helpers import IsolatedTestCase as _IsolatedTestCase  # noqa: E402


class _MockAgentBackend(_AgentBackend):
    def __init__(self, responses=None, default_response="mock reply"):
        self._responses = list(responses or [])
        self._default_response = default_response
        self.sent_messages = []
        self._session_counter = 0

    async def create_session(self, working_directory, extra_args=None, session_title=None):
        self._session_counter += 1
        return f"mock-session-{self._session_counter:04d}"

    async def send(self, session_id, prompt, working_directory, timeout, attachments=None, env=None):
        self.sent_messages.append({"prompt": prompt, "session_id": session_id, "attachments": attachments})
        text = self._responses.pop(0) if self._responses else self._default_response
        return _AgentResponse(text=text)


def _make_watcher_cr(room="script", name=None):
    from gateway.config import WatcherConfig
    return WatcherConfig(
        name=name or room, connector="script", room=room, agent="default"
    )


def _make_permission_request_cr(registry, room_id="script", session_id="mock-session-0001"):
    from gateway.core.permission import PermissionRequest, _generate_id
    req = PermissionRequest(
        request_id=_generate_id(),
        tool_name="Bash",
        tool_input={"command": "ls"},
        room_id=room_id,
        session_id=session_id,
    )
    registry.register(req)
    return req


def _make_manager_cr(connector, agent, watcher_configs=None, permission_registry=None):
    from gateway.config import AgentConfig
    from gateway.core.config import CoreConfig
    from gateway.core.session_manager import SessionManager

    agent_cfg = AgentConfig(timeout=10)
    config = CoreConfig(agents={"default": agent_cfg}, default_agent="default")
    return SessionManager(
        connector,
        {"default": agent},
        "default",
        config,
        watcher_configs=watcher_configs or [],
        permission_registry=permission_registry,
    )


class TestPermissionCommandPreFanOut(_IsolatedTestCase):
    """Issue #1: approve/deny must be handled once at room level, not per-processor."""

    async def test_approve_command_resolves_permission(self):
        """An 'approve XXXX' message from an owner resolves the pending request."""
        from gateway.connectors.script import ScriptConnector
        from gateway.core.connector import UserRole
        from gateway.core.permission import PermissionRegistry

        connector = ScriptConnector()
        agent = _MockAgentBackend()
        registry = PermissionRegistry()
        manager = _make_manager_cr(
            connector, agent, watcher_configs=[_make_watcher_cr()], permission_registry=registry
        )
        await manager.run_once()

        req = _make_permission_request_cr(registry)
        await connector.inject(f"approve {req.request_id}", role=UserRole.OWNER)
        reply = await connector.receive_reply(timeout=5.0)

        self.assertIn("approved", reply.lower())
        self.assertIn(req.request_id, reply)

        await manager.shutdown()

    async def test_deny_command_resolves_permission(self):
        from gateway.connectors.script import ScriptConnector
        from gateway.core.connector import UserRole
        from gateway.core.permission import PermissionRegistry

        connector = ScriptConnector()
        agent = _MockAgentBackend()
        registry = PermissionRegistry()
        manager = _make_manager_cr(
            connector, agent, watcher_configs=[_make_watcher_cr()], permission_registry=registry
        )
        await manager.run_once()

        req = _make_permission_request_cr(registry)
        await connector.inject(f"deny {req.request_id}", role=UserRole.OWNER)
        reply = await connector.receive_reply(timeout=5.0)

        self.assertIn("denied", reply.lower())
        await manager.shutdown()

    async def test_permission_command_not_forwarded_to_agent(self):
        from gateway.connectors.script import ScriptConnector
        from gateway.core.connector import UserRole
        from gateway.core.permission import PermissionRegistry

        connector = ScriptConnector()
        agent = _MockAgentBackend()
        registry = PermissionRegistry()
        manager = _make_manager_cr(
            connector, agent, watcher_configs=[_make_watcher_cr()], permission_registry=registry
        )
        await manager.run_once()

        req = _make_permission_request_cr(registry)
        await connector.inject(f"approve {req.request_id}", role=UserRole.OWNER)
        await connector.receive_reply(timeout=5.0)

        approve_msgs = [m for m in agent.sent_messages if "approve" in m["prompt"]]
        self.assertEqual(approve_msgs, [])

        await manager.shutdown()

    async def test_invalid_request_id_length_rejected(self):
        from gateway.connectors.script import ScriptConnector
        from gateway.core.connector import UserRole
        from gateway.core.permission import PermissionRegistry

        connector = ScriptConnector()
        agent = _MockAgentBackend()
        registry = PermissionRegistry()
        manager = _make_manager_cr(
            connector, agent, watcher_configs=[_make_watcher_cr()], permission_registry=registry
        )
        await manager.run_once()

        await connector.inject("approve ab", role=UserRole.OWNER)
        reply = await connector.receive_reply(timeout=5.0)

        self.assertIn("Invalid ID", reply)
        await manager.shutdown()

    async def test_unknown_request_id_gets_no_pending_reply(self):
        from gateway.connectors.script import ScriptConnector
        from gateway.core.connector import UserRole
        from gateway.core.permission import PermissionRegistry

        connector = ScriptConnector()
        agent = _MockAgentBackend()
        registry = PermissionRegistry()
        manager = _make_manager_cr(
            connector, agent, watcher_configs=[_make_watcher_cr()], permission_registry=registry
        )
        await manager.run_once()

        await connector.inject("approve zzzz", role=UserRole.OWNER)
        reply = await connector.receive_reply(timeout=5.0)

        self.assertIn("No pending", reply)
        await manager.shutdown()

    async def test_guest_cannot_approve(self):
        from gateway.connectors.script import ScriptConnector
        from gateway.core.connector import UserRole
        from gateway.core.permission import PermissionRegistry

        connector = ScriptConnector()
        agent = _MockAgentBackend()
        registry = PermissionRegistry()
        manager = _make_manager_cr(
            connector, agent, watcher_configs=[_make_watcher_cr()], permission_registry=registry
        )
        await manager.run_once()

        req = _make_permission_request_cr(registry)
        await connector.inject(f"approve {req.request_id}", role=UserRole.GUEST)
        await connector.receive_reply(timeout=5.0)

        guest_msgs = [m for m in agent.sent_messages if "approve" in m["prompt"]]
        self.assertEqual(len(guest_msgs), 1)

        await manager.shutdown()

    async def test_non_permission_messages_still_fan_out(self):
        from gateway.connectors.script import ScriptConnector
        from gateway.core.connector import UserRole
        from gateway.core.permission import PermissionRegistry

        connector = ScriptConnector()
        agent = _MockAgentBackend(responses=["hello back"])
        registry = PermissionRegistry()
        manager = _make_manager_cr(
            connector, agent, watcher_configs=[_make_watcher_cr()], permission_registry=registry
        )
        await manager.run_once()

        await connector.inject("hello", role=UserRole.OWNER)
        reply = await connector.receive_reply(timeout=5.0)

        self.assertEqual(reply, "hello back")
        await manager.shutdown()
