"""AgentTurnRunner: executes one agent turn — send, handle response, post reply.

Extracted from MessageProcessor._process() so the processor is focused on queue
orchestration and session-map bookkeeping, while the turn runner owns:
  - Typing indicator bracket (on before send, off after)
  - Agent invocation with configured timeout
  - Usage logging
  - Response posting
  - Timeout / error handling with user-facing messages
"""

from __future__ import annotations

import asyncio
import logging

from ..agents import AgentBackend
from ..agents.errors import (
    AgentPermissionError,
    AgentRateLimitedError,
    AgentUnavailableError,
)
from ..agents.response import AgentResponse
from .config import CoreConfig
from .connector import Connector

logger = logging.getLogger("agent-chat-gateway.core.turn_runner")


def _user_facing_agent_error_message(exc: Exception, session_id: str = "") -> str:
    """Return a production-safe chat message for agent execution failures.

    Keep detailed diagnostics in logs, but avoid leaking backend internals,
    local paths, HTTP details, or raw CLI errors back into the chat room.
    """
    ref_suffix = f" (ref: {session_id[:8]})" if session_id else ""
    if isinstance(exc, AgentRateLimitedError):
        return (
            "❌ Agent is temporarily unavailable due to a usage limit. "
            f"Please try again later.{ref_suffix}"
        )
    if isinstance(exc, AgentPermissionError):
        return (
            "❌ Agent could not complete the request because of a permission restriction."
            f"{ref_suffix}"
        )
    if isinstance(exc, AgentUnavailableError):
        return (
            f"❌ Agent is temporarily unavailable. Please try again later.{ref_suffix}"
        )
    return f"❌ Agent failed to process the request. Please try again.{ref_suffix}"


class AgentTurnRunner:
    """Runs a single agent turn: prompt → agent → reply (or error message).

    Stateless per-turn — the same runner instance is reused across multiple
    turns within a single MessageProcessor.
    """

    def __init__(
        self,
        agent: AgentBackend,
        connector: Connector,
        config: CoreConfig,
        agent_name: str = "",
        room_name: str = "",
    ) -> None:
        self._agent = agent
        self._connector = connector
        self._config = config
        self._agent_name = agent_name
        self._room_name = room_name

    async def run_turn(
        self,
        session_id: str,
        prompt: str,
        working_directory: str,
        room_id: str,
        thread_id: str | None,
        file_paths: list[str] | None = None,
        role_env: dict[str, str] | None = None,
    ) -> None:
        """Execute one turn: send prompt to agent, post response (or error) to room.

        Two separate error boundaries:
          - **Stage 1** (agent execution): agent.send() failure produces an error
            response object but does not touch the connector.
          - **Stage 2** (delivery): connector.send_text() failure is logged
            distinctly and does NOT attempt to send another error message through
            the same potentially broken transport (avoids recursive error loops).

        Typing indicators bracket both stages regardless of outcome.
        """
        await self._notify_typing(room_id, True)
        try:
            # Stage 1: execute agent turn
            response = await self._execute_agent(
                session_id,
                prompt,
                working_directory,
                file_paths,
                role_env,
            )

            # Stage 2: deliver response to chat room
            await self._deliver_response(room_id, response, thread_id)
        finally:
            await self._notify_typing(room_id, False)

    async def _execute_agent(
        self,
        session_id: str,
        prompt: str,
        working_directory: str,
        file_paths: list[str] | None,
        role_env: dict[str, str] | None,
    ) -> AgentResponse:
        """Run the agent backend and return a response (or an error response).

        Exceptions from the agent are caught here and converted to error
        AgentResponse objects so the delivery stage always has something to post.
        """
        try:
            response = await self._agent.send(
                session_id=session_id,
                prompt=prompt,
                working_directory=working_directory,
                timeout=self._config.timeout_for(self._agent_name),
                attachments=file_paths,
                env=role_env,
            )
            if response.usage:
                logger.info(
                    "Agent usage [%s] in=%d out=%d cache_read=%d cost=%s",
                    self._room_name,
                    response.usage.input_tokens,
                    response.usage.output_tokens,
                    response.usage.cache_read_tokens,
                    f"${response.cost_usd:.4f}" if response.cost_usd else "n/a",
                )
            return response
        except asyncio.TimeoutError:
            logger.error("Agent timed out for message: %s", prompt[:80])
            return AgentResponse(
                text="⏱️ Request timed out. Please try again.",
                is_error=True,
            )
        except Exception as e:
            logger.exception(
                "Agent error (session=%s room=%s): %s",
                session_id[:8],
                self._room_name,
                e,
            )
            return AgentResponse(
                text=_user_facing_agent_error_message(e, session_id=session_id),
                is_error=True,
            )

    async def _deliver_response(
        self,
        room_id: str,
        response: AgentResponse,
        thread_id: str | None,
    ) -> None:
        """Post an AgentResponse to the chat room.

        Connector delivery failures are logged with connector-specific context
        and do NOT attempt to send another error message through the same
        potentially broken transport — this prevents recursive error loops.
        """
        try:
            await self._connector.send_text(room_id, response, thread_id=thread_id)
        except Exception as e:
            logger.error(
                "Failed to deliver response to room %s: %s: %s (response text was: %s)",
                room_id,
                type(e).__name__,
                e,
                response.text[:100],
            )

    async def _notify_typing(self, room_id: str, is_typing: bool) -> None:
        try:
            await self._connector.notify_typing(room_id, is_typing)
        except Exception as e:
            logger.debug("Failed to send typing notification: %s", e)
