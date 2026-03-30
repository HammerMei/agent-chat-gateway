"""Tests for gateway.core.agent_turn_runner.AgentTurnRunner.

Covers:
  - Successful turn: agent called, response posted, typing bracketed
  - Timeout: timeout error message posted
  - Exception: error message posted
  - Usage logging on response with usage metadata
"""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock

from gateway.agents import AgentBackend
from gateway.agents.errors import AgentPermissionError, AgentRateLimitedError
from gateway.agents.response import AgentResponse
from gateway.config import AgentConfig
from gateway.core.agent_turn_runner import AgentTurnRunner
from gateway.core.config import CoreConfig


# ── Helpers ────────────────────────────────────────────────────────────────────


class _MockAgent(AgentBackend):
    def __init__(self, response: AgentResponse | Exception):
        self._response = response

    async def create_session(self, *a, **kw):
        return "ses_001"

    async def send(
        self, session_id, prompt, working_directory, timeout, attachments=None, env=None
    ):
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


def _make_runner(agent: AgentBackend) -> tuple[AgentTurnRunner, MagicMock]:
    connector = MagicMock()
    connector.send_text = AsyncMock()
    connector.notify_typing = AsyncMock()
    config = CoreConfig(
        agents={"default": AgentConfig(timeout=10)},
        default_agent="default",
    )
    runner = AgentTurnRunner(
        agent=agent,
        connector=connector,
        config=config,
        agent_name="default",
        room_name="test-room",
    )
    return runner, connector


# ── Tests ─────────────────────────────────────────────────────────────────────


class TestAgentTurnRunner(unittest.IsolatedAsyncioTestCase):
    async def test_successful_turn_posts_response(self):
        agent = _MockAgent(AgentResponse(text="Hi there!"))
        runner, connector = _make_runner(agent)

        await runner.run_turn(
            session_id="ses_001",
            prompt="hello",
            working_directory="/tmp",
            room_id="room_1",
            thread_id="t1",
        )

        connector.send_text.assert_called_once()
        call_args = connector.send_text.call_args
        self.assertEqual(call_args[0][0], "room_1")
        self.assertEqual(call_args[0][1].text, "Hi there!")
        self.assertEqual(call_args[1]["thread_id"], "t1")

    async def test_typing_notifications_bracket_execution(self):
        agent = _MockAgent(AgentResponse(text="ok"))
        runner, connector = _make_runner(agent)

        await runner.run_turn(
            session_id="ses_001",
            prompt="hello",
            working_directory="/tmp",
            room_id="room_1",
            thread_id=None,
        )

        # notify_typing called twice: True (before send), False (after)
        calls = connector.notify_typing.call_args_list
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[0][0][1])  # first call: is_typing=True
        self.assertFalse(calls[1][0][1])  # second call: is_typing=False

    async def test_timeout_posts_error_message(self):
        agent = _MockAgent(asyncio.TimeoutError())
        runner, connector = _make_runner(agent)

        await runner.run_turn(
            session_id="ses_001",
            prompt="slow query",
            working_directory="/tmp",
            room_id="room_1",
            thread_id=None,
        )

        connector.send_text.assert_called_once()
        response = connector.send_text.call_args[0][1]
        self.assertTrue(response.is_error)
        self.assertIn("timed out", response.text)

    async def test_exception_posts_error_message(self):
        agent = _MockAgent(RuntimeError("backend crashed"))
        runner, connector = _make_runner(agent)

        await runner.run_turn(
            session_id="ses_001",
            prompt="trigger error",
            working_directory="/tmp",
            room_id="room_1",
            thread_id="t1",
        )

        connector.send_text.assert_called_once()
        response = connector.send_text.call_args[0][1]
        self.assertTrue(response.is_error)
        self.assertIn("failed to process the request", response.text.lower())
        self.assertIn("ref: ses_001", response.text)

    async def test_rate_limited_exception_maps_to_friendly_message(self):
        agent = _MockAgent(AgentRateLimitedError("quota reached"))
        runner, connector = _make_runner(agent)

        await runner.run_turn(
            session_id="ses_001",
            prompt="trigger limit",
            working_directory="/tmp",
            room_id="room_1",
            thread_id=None,
        )

        response = connector.send_text.call_args[0][1]
        self.assertIn("usage limit", response.text.lower())
        self.assertIn("ref: ses_001", response.text)

    async def test_permission_exception_maps_to_friendly_message(self):
        agent = _MockAgent(AgentPermissionError("approval required"))
        runner, connector = _make_runner(agent)

        await runner.run_turn(
            session_id="ses_001",
            prompt="blocked",
            working_directory="/tmp",
            room_id="room_1",
            thread_id=None,
        )

        response = connector.send_text.call_args[0][1]
        self.assertIn("permission restriction", response.text.lower())
        self.assertIn("ref: ses_001", response.text)

    async def test_typing_off_sent_even_on_error(self):
        """Typing indicator is turned off even when the agent raises."""
        agent = _MockAgent(RuntimeError("boom"))
        runner, connector = _make_runner(agent)

        await runner.run_turn(
            session_id="ses_001",
            prompt="fail",
            working_directory="/tmp",
            room_id="room_1",
            thread_id=None,
        )

        # Last notify_typing call must be False
        last_call = connector.notify_typing.call_args_list[-1]
        self.assertFalse(last_call[0][1])

    async def test_file_paths_and_env_forwarded_to_agent(self):
        """file_paths and role_env are forwarded to agent.send()."""
        agent = MagicMock()
        agent.send = AsyncMock(return_value=AgentResponse(text="ok"))
        agent.supports_per_message_env = True

        connector = MagicMock()
        connector.send_text = AsyncMock()
        connector.notify_typing = AsyncMock()
        config = CoreConfig(
            agents={"default": AgentConfig(timeout=10)},
            default_agent="default",
        )
        runner = AgentTurnRunner(agent, connector, config, "default", "room")

        await runner.run_turn(
            session_id="ses_001",
            prompt="hello",
            working_directory="/tmp",
            room_id="room_1",
            thread_id=None,
            file_paths=["/tmp/a.txt"],
            role_env={"ACG_ROLE": "owner"},
        )

        agent.send.assert_called_once()
        call_kwargs = agent.send.call_args[1]
        self.assertEqual(call_kwargs["attachments"], ["/tmp/a.txt"])
        self.assertEqual(call_kwargs["env"], {"ACG_ROLE": "owner"})

    async def test_connector_send_failure_logged_not_recursive(self):
        """Connector send_text failure is logged, not recursively retried."""
        agent = _MockAgent(AgentResponse(text="Good reply"))
        runner, connector = _make_runner(agent)
        connector.send_text = AsyncMock(side_effect=ConnectionError("RC down"))

        # Must not raise — delivery failure is caught and logged
        await runner.run_turn(
            session_id="ses_001",
            prompt="hello",
            working_directory="/tmp",
            room_id="room_1",
            thread_id=None,
        )

        # send_text was called exactly once — no recursive error posting
        connector.send_text.assert_called_once()

    async def test_agent_error_delivered_even_when_connector_healthy(self):
        """Agent error produces an error response that goes through delivery."""
        agent = _MockAgent(RuntimeError("agent crashed"))
        runner, connector = _make_runner(agent)

        await runner.run_turn(
            session_id="ses_001",
            prompt="fail",
            working_directory="/tmp",
            room_id="room_1",
            thread_id=None,
        )

        # Error response should have been delivered
        connector.send_text.assert_called_once()
        delivered = connector.send_text.call_args[0][1]
        self.assertTrue(delivered.is_error)
        self.assertIn("failed to process the request", delivered.text.lower())
        self.assertIn("ref: ses_001", delivered.text)

    async def test_connector_failure_after_agent_success_no_error_loop(self):
        """When agent succeeds but connector fails, no second send is attempted."""
        agent = _MockAgent(AgentResponse(text="Success"))
        runner, connector = _make_runner(agent)
        connector.send_text = AsyncMock(side_effect=ConnectionError("transport down"))

        await runner.run_turn(
            session_id="ses_001",
            prompt="hello",
            working_directory="/tmp",
            room_id="room_1",
            thread_id=None,
        )

        # Only ONE send_text call (the failed delivery), not a second one
        # attempting to send "Error: transport down"
        self.assertEqual(connector.send_text.call_count, 1)


if __name__ == "__main__":
    unittest.main()
