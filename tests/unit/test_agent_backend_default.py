"""Tests for AgentBackend's durable-instruction-delivery contract (issue #52).

``ensure_durable_instructions()`` has no usable default — calling it on a
backend that doesn't override it raises NotImplementedError, forcing every
backend to make an explicit, visible choice. The shared one-time-send
fallback (the pre-#52 behavior, still used by OpenCode today) lives in
``_send_once_as_durable_fallback()``, which a backend must opt into
explicitly (see OpenCodeBackend.ensure_durable_instructions()).
"""

from __future__ import annotations

import unittest

from gateway.agents import AgentBackend
from gateway.agents.errors import AgentExecutionError
from gateway.agents.response import AgentResponse


class _RecordingBackend(AgentBackend):
    """Minimal AgentBackend that records send() calls and returns a
    configurable response — exercises _send_once_as_durable_fallback()
    directly, and (deliberately) does NOT override
    ensure_durable_instructions() so the no-default contract can be tested."""

    def __init__(self, response: AgentResponse | None = None):
        self._response = response or AgentResponse(text="ok")
        self.send_calls: list[dict] = []

    async def create_session(self, *a, **kw):
        return "ses_001"

    async def send(
        self, session_id, prompt, working_directory, timeout,
        attachments=None, env=None, append_system_prompt_file=None,
    ):
        self.send_calls.append(
            {"session_id": session_id, "prompt": prompt, "working_directory": working_directory}
        )
        return self._response


class TestEnsureDurableInstructionsHasNoDefault(unittest.IsolatedAsyncioTestCase):
    """A backend that does not override ensure_durable_instructions() must
    get a loud, explicit failure — never a silently-inherited fallback."""

    async def test_raises_not_implemented_when_not_overridden(self):
        backend = _RecordingBackend()

        with self.assertRaises(NotImplementedError):
            await backend.ensure_durable_instructions(
                "ses_001", "/tmp", 10, "some content",
                watcher_name="w1", already_delivered=False,
            )

    async def test_not_implemented_message_names_the_backend_class(self):
        backend = _RecordingBackend()

        with self.assertRaises(NotImplementedError) as ctx:
            await backend.ensure_durable_instructions(
                "ses_001", "/tmp", 10, "some content",
                watcher_name="w1", already_delivered=False,
            )

        self.assertIn("_RecordingBackend", str(ctx.exception))

    async def test_send_never_called_when_not_overridden(self):
        """The no-default method must not silently fall back to send() on
        its own — that would defeat the point of forcing an explicit opt-in."""
        backend = _RecordingBackend()

        with self.assertRaises(NotImplementedError):
            await backend.ensure_durable_instructions(
                "ses_001", "/tmp", 10, "some content",
                watcher_name="w1", already_delivered=False,
            )

        self.assertEqual(len(backend.send_calls), 0)


class TestSendOnceAsDurableFallback(unittest.IsolatedAsyncioTestCase):
    """The shared fallback helper a backend can explicitly opt into (see
    OpenCodeBackend.ensure_durable_instructions()) — same behavior as the
    old ContextInjector's one-time send()."""

    async def test_calls_send_once_when_not_already_delivered(self):
        backend = _RecordingBackend()

        result = await backend._send_once_as_durable_fallback(
            "ses_001", "/tmp", 10, "some content", already_delivered=False,
        )

        self.assertIsNone(result)
        self.assertEqual(len(backend.send_calls), 1)
        self.assertEqual(backend.send_calls[0]["prompt"], "some content")
        self.assertEqual(backend.send_calls[0]["session_id"], "ses_001")
        self.assertEqual(backend.send_calls[0]["working_directory"], "/tmp")

    async def test_does_not_call_send_when_already_delivered(self):
        backend = _RecordingBackend()

        result = await backend._send_once_as_durable_fallback(
            "ses_001", "/tmp", 10, "some content", already_delivered=True,
        )

        self.assertIsNone(result)
        self.assertEqual(len(backend.send_calls), 0)

    async def test_raises_agent_execution_error_when_send_response_is_error(self):
        backend = _RecordingBackend(response=AgentResponse(text="boom", is_error=True))

        with self.assertRaises(AgentExecutionError):
            await backend._send_once_as_durable_fallback(
                "ses_001", "/tmp", 10, "some content", already_delivered=False,
            )

    async def test_error_message_truncated_to_200_chars(self):
        long_error = "x" * 500
        backend = _RecordingBackend(response=AgentResponse(text=long_error, is_error=True))

        with self.assertRaises(AgentExecutionError) as ctx:
            await backend._send_once_as_durable_fallback(
                "ses_001", "/tmp", 10, "some content", already_delivered=False,
            )

        self.assertLessEqual(len(str(ctx.exception)), 200)

    async def test_success_returns_none_not_a_path(self):
        """The fallback fully handles delivery — callers must not be told to
        re-supply anything on subsequent turns."""
        backend = _RecordingBackend(response=AgentResponse(text="ok", is_error=False))

        result = await backend._send_once_as_durable_fallback(
            "ses_001", "/tmp", 10, "some content", already_delivered=False,
        )

        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
