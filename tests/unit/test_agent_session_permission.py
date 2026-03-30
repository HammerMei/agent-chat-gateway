"""Tests and usage examples for AgentSession with a permission_handler.

Shows how a caller can register an async callable to approve or deny
Claude tool calls programmatically — no Connector or RC room required.
"""

from __future__ import annotations

import asyncio
import json
import unittest
from unittest.mock import AsyncMock, MagicMock

from gateway.agents.session import AgentSession

# ── Minimal mock backend ───────────────────────────────────────────────────────

class MockBackend:
    """Minimal AgentBackend stand-in that mimics Claude's settings_path behavior."""

    def __init__(self, response_text: str = "done") -> None:
        self.settings_path: str = ""
        self._response_text = response_text
        self.create_session_called_with_settings: str = ""
        self._pre_broker_settings_path: str | None = None

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    def create_callable_broker(self, handler, timeout_seconds: int):
        from gateway.agents.claude.callable_broker import CallablePermissionBroker
        return CallablePermissionBroker(handler, timeout_seconds=timeout_seconds)

    def attach_callable_broker(self, broker: object) -> None:
        sp = getattr(broker, "settings_path", "")
        if sp:
            self._pre_broker_settings_path = self.settings_path
            self.settings_path = sp

    def detach_callable_broker(self) -> None:
        if self._pre_broker_settings_path is not None:
            self.settings_path = self._pre_broker_settings_path
            self._pre_broker_settings_path = None

    async def create_session(self, cwd, extra_args=None, session_title=None) -> str:
        self.create_session_called_with_settings = self.settings_path
        return "mock-session-id"

    async def send(self, session_id, prompt, cwd, timeout, attachments=None, env=None):
        from gateway.agents.response import AgentResponse
        return AgentResponse(text=self._response_text, session_id=session_id)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _post_hook(port: int, tool_name: str, tool_input: dict) -> dict:
    """Simulate a Claude PreToolUse POST to the broker's HTTP server."""
    import urllib.request
    body = json.dumps({
        "tool_name": tool_name,
        "tool_input": tool_input,
        "session_id": "mock-session-id",
        "cwd": "/tmp",
    }).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/hook",
        data=body,
        headers={"Content-Type": "application/json", "Content-Length": str(len(body))},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read())


# ── Tests ──────────────────────────────────────────────────────────────────────

class TestCallablePermissionBroker(unittest.IsolatedAsyncioTestCase):
    """Unit tests for CallablePermissionBroker in isolation."""

    async def test_handler_approve_returns_allow(self):
        """Handler returning True → broker responds with allow."""
        from gateway.agents.claude.callable_broker import CallablePermissionBroker

        async def allow_all(tool_name: str, tool_input: dict) -> bool:
            return True

        broker = CallablePermissionBroker(allow_all)
        await broker.start()
        try:
            result = await asyncio.get_event_loop().run_in_executor(
                None, _post_hook, broker._port, "bash", {"command": "ls"}
            )
            self.assertEqual(result["decision"], "allow")
        finally:
            await broker.stop()

    async def test_handler_deny_returns_block(self):
        """Handler returning False → broker responds with block."""
        from gateway.agents.claude.callable_broker import CallablePermissionBroker

        async def deny_all(tool_name: str, tool_input: dict) -> bool:
            return False

        broker = CallablePermissionBroker(deny_all)
        await broker.start()
        try:
            result = await asyncio.get_event_loop().run_in_executor(
                None, _post_hook, broker._port, "bash", {"command": "rm -rf /"}
            )
            self.assertEqual(result["decision"], "block")
        finally:
            await broker.stop()

    async def test_handler_receives_tool_name_and_input(self):
        """Handler receives correct tool_name and tool_input arguments."""
        from gateway.agents.claude.callable_broker import CallablePermissionBroker

        received: list = []

        async def capture(tool_name: str, tool_input: dict) -> bool:
            received.append((tool_name, tool_input))
            return True

        broker = CallablePermissionBroker(capture)
        await broker.start()
        try:
            await asyncio.get_event_loop().run_in_executor(
                None, _post_hook, broker._port, "Read", {"file_path": "/tmp/foo.txt"}
            )
            self.assertEqual(len(received), 1)
            self.assertEqual(received[0][0], "Read")
            self.assertEqual(received[0][1], {"file_path": "/tmp/foo.txt"})
        finally:
            await broker.stop()

    async def test_handler_exception_returns_block(self):
        """Handler that raises → broker blocks the tool call safely."""
        from gateway.agents.claude.callable_broker import CallablePermissionBroker

        async def broken_handler(tool_name: str, tool_input: dict) -> bool:
            raise ValueError("something went wrong")

        broker = CallablePermissionBroker(broken_handler)
        await broker.start()
        try:
            result = await asyncio.get_event_loop().run_in_executor(
                None, _post_hook, broker._port, "bash", {}
            )
            self.assertEqual(result["decision"], "block")
        finally:
            await broker.stop()

    async def test_settings_path_written_after_start(self):
        """settings_path is non-empty after start() and cleaned up after stop()."""
        import os

        from gateway.agents.claude.callable_broker import CallablePermissionBroker

        broker = CallablePermissionBroker(AsyncMock(return_value=True))
        self.assertEqual(broker.settings_path, "")
        await broker.start()
        self.assertTrue(os.path.exists(broker.settings_path))
        path = broker.settings_path
        await broker.stop()
        self.assertFalse(os.path.exists(path))

    async def test_settings_file_contains_hook_url(self):
        """The written settings JSON points to the broker's localhost port."""
        from gateway.agents.claude.callable_broker import CallablePermissionBroker

        broker = CallablePermissionBroker(AsyncMock(return_value=True))
        await broker.start()
        try:
            with open(broker.settings_path) as f:
                settings = json.load(f)
            hooks = settings["hooks"]["PreToolUse"][0]["hooks"]
            self.assertIn(str(broker._port), hooks[0]["url"])
        finally:
            await broker.stop()


class TestAgentSessionWithPermissionHandler(unittest.IsolatedAsyncioTestCase):
    """Tests for AgentSession permission_handler integration."""

    async def test_broker_started_on_enter(self):
        """Broker is started and settings_path patched before create_session."""
        backend = MockBackend()

        async def allow_all(tool_name, tool_input):
            return True

        async with AgentSession(backend, "/tmp", permission_handler=allow_all):
            # settings_path was set before create_session() was called
            self.assertNotEqual(backend.create_session_called_with_settings, "")

    async def test_broker_stopped_on_exit(self):
        """Broker is stopped and settings_path restored after context exit."""
        import os
        backend = MockBackend()

        settings_path_during: list[str] = []

        async def capture(tool_name, tool_input):
            settings_path_during.append(backend.settings_path)
            return True

        async with AgentSession(backend, "/tmp", permission_handler=capture):
            path_inside = backend.settings_path
            self.assertTrue(os.path.exists(path_inside))

        # After exit: file deleted, settings_path restored to original ""
        self.assertFalse(os.path.exists(path_inside))
        self.assertEqual(backend.settings_path, "")

    async def test_no_broker_without_handler(self):
        """No broker is created when permission_handler is not provided."""
        backend = MockBackend()
        async with AgentSession(backend, "/tmp") as session:
            self.assertIsNone(session._broker)
            self.assertEqual(backend.settings_path, "")

    async def test_session_id_set_after_enter(self):
        """session_id is populated after entering the context."""
        backend = MockBackend()

        async def allow_all(tool_name, tool_input):
            return True

        async with AgentSession(backend, "/tmp", permission_handler=allow_all) as session:
            self.assertEqual(session.session_id, "mock-session-id")

    async def test_original_settings_path_restored(self):
        """Pre-existing settings_path on backend is restored after exit."""
        backend = MockBackend()
        backend.settings_path = "/pre-existing/settings.json"

        async def allow_all(tool_name, tool_input):
            return True

        async with AgentSession(backend, "/tmp", permission_handler=allow_all):
            # During: broker's path is active
            self.assertNotEqual(backend.settings_path, "/pre-existing/settings.json")

        # After: original path restored
        self.assertEqual(backend.settings_path, "/pre-existing/settings.json")

    async def test_backend_stop_called_even_if_broker_stop_fails(self):
        """Backend.stop() must run even when broker.stop() raises (no orphaned processes)."""
        backend = MockBackend()
        stopped: list[str] = []

        original_stop = backend.stop

        async def tracking_stop():
            stopped.append("backend")
            await original_stop()

        backend.stop = tracking_stop

        async def allow_all(tool_name, tool_input):
            return True

        session = AgentSession(backend, "/tmp", permission_handler=allow_all)
        await session.__aenter__()
        # Sabotage the broker's stop method
        session._broker.stop = AsyncMock(side_effect=RuntimeError("broker stop failed"))

        # __aexit__ should not propagate the broker error before stopping backend
        with self.assertRaises(RuntimeError):
            await session.__aexit__(None, None, None)

        self.assertIn("backend", stopped)


# ── Usage examples (documentation as runnable code) ───────────────────────────

class TestUsageExamples(unittest.IsolatedAsyncioTestCase):
    """Runnable usage examples — also serve as documentation."""

    async def test_example_allow_read_only(self):
        """Example: allow only Read tool calls, deny everything else."""
        backend = MockBackend(response_text="Here are the files: main.py, README.md")

        async def read_only_handler(tool_name: str, tool_input: dict) -> bool:
            """Approve Read calls, deny all others."""
            return tool_name.lower() == "read"

        async with AgentSession(
            backend,
            "/my/project",
            permission_handler=read_only_handler,
        ) as session:
            reply = await session.send("List the files here")
            self.assertIn("main.py", str(reply))

        # Verify the broker actually enforced the policy
        from gateway.agents.claude.callable_broker import CallablePermissionBroker
        broker = CallablePermissionBroker(read_only_handler)
        await broker.start()
        try:
            allow = await asyncio.get_event_loop().run_in_executor(
                None, _post_hook, broker._port, "Read", {"file_path": "/tmp/foo"}
            )
            deny = await asyncio.get_event_loop().run_in_executor(
                None, _post_hook, broker._port, "bash", {"command": "ls"}
            )
            self.assertEqual(allow["decision"], "allow")
            self.assertEqual(deny["decision"], "block")
        finally:
            await broker.stop()

    async def test_example_logging_handler(self):
        """Example: log every tool call, approve all (audit mode)."""
        backend = MockBackend()
        audit_log: list[tuple[str, dict]] = []

        async def audit_handler(tool_name: str, tool_input: dict) -> bool:
            """Log every tool call but approve all — audit mode."""
            audit_log.append((tool_name, tool_input))
            return True

        async with AgentSession(
            backend,
            "/my/project",
            permission_handler=audit_handler,
        ) as session:
            await session.send("Do something")

        # Audit log can be inspected after the session
        # (In real usage, tool calls are logged as they happen)

    async def test_example_conditional_handler(self):
        """Example: approve only safe paths for file tools."""
        SAFE_PREFIXES = ("/tmp/", "/home/user/safe/")

        async def safe_path_handler(tool_name: str, tool_input: dict) -> bool:
            """Approve file tools only for safe path prefixes."""
            if tool_name.lower() in ("read", "edit", "write"):
                path = tool_input.get("file_path", "")
                return any(path.startswith(prefix) for prefix in SAFE_PREFIXES)
            return True  # approve all non-file tools

        from gateway.agents.claude.callable_broker import CallablePermissionBroker
        broker = CallablePermissionBroker(safe_path_handler)
        await broker.start()
        try:
            safe = await asyncio.get_event_loop().run_in_executor(
                None, _post_hook, broker._port, "Read", {"file_path": "/tmp/ok.txt"}
            )
            unsafe = await asyncio.get_event_loop().run_in_executor(
                None, _post_hook, broker._port, "Read", {"file_path": "/etc/passwd"}
            )
            self.assertEqual(safe["decision"], "allow")
            self.assertEqual(unsafe["decision"], "block")
        finally:
            await broker.stop()


# ── attach / detach callable broker hooks (P2-3) ─────────────────────────────

class TestAttachDetachCallableBroker(unittest.IsolatedAsyncioTestCase):
    """ClaudeBackend.attach/detach manage settings_path patching internally."""

    def _make_claude_backend(self):
        """Build a minimal ClaudeBackend for testing attach/detach hooks."""
        from gateway.agents.claude.adapter import ClaudeBackend
        return ClaudeBackend(command="claude", new_session_args=[], timeout=10)

    async def test_attach_patches_settings_path(self):
        backend = self._make_claude_backend()
        self.assertEqual(backend.settings_path, "")

        mock_broker = MagicMock()
        mock_broker.settings_path = "/tmp/broker-settings.json"

        backend.attach_callable_broker(mock_broker)
        self.assertEqual(backend.settings_path, "/tmp/broker-settings.json")

    async def test_detach_restores_original_settings_path(self):
        backend = self._make_claude_backend()
        backend.settings_path = "/original/settings.json"

        mock_broker = MagicMock()
        mock_broker.settings_path = "/tmp/broker-settings.json"

        backend.attach_callable_broker(mock_broker)
        self.assertEqual(backend.settings_path, "/tmp/broker-settings.json")

        backend.detach_callable_broker()
        self.assertEqual(backend.settings_path, "/original/settings.json")

    async def test_detach_without_attach_is_noop(self):
        backend = self._make_claude_backend()
        backend.settings_path = "/original/settings.json"
        backend.detach_callable_broker()  # must not raise
        self.assertEqual(backend.settings_path, "/original/settings.json")

    async def test_attach_noop_when_broker_has_no_settings_path(self):
        """Brokers without settings_path (e.g. OpenCode) don't trigger patching."""
        backend = self._make_claude_backend()
        backend.settings_path = "/original/settings.json"

        mock_broker = MagicMock(spec=[])  # no attributes

        backend.attach_callable_broker(mock_broker)
        # settings_path unchanged
        self.assertEqual(backend.settings_path, "/original/settings.json")

    async def test_base_class_hooks_are_noop(self):
        """AgentBackend default attach/detach are harmless no-ops."""
        from gateway.agents import AgentBackend

        class StubBackend(AgentBackend):
            async def create_session(self, *a, **kw):
                return "stub"
            async def send(self, *a, **kw):
                from gateway.agents.response import AgentResponse
                return AgentResponse(text="ok")

        backend = StubBackend()
        backend.attach_callable_broker(object())  # no-op, must not raise
        backend.detach_callable_broker()  # no-op, must not raise
