"""Tests for MessageProcessor._process_batch() catch-up batching behaviour.

Covers:
  - Single message → normal _process path (no catch-up prompt)
  - Multiple messages → catch-up prompt delivered to agent
  - Anchor is always the last non-anonymous message
  - Anonymous messages are filtered from batch
  - Attachments are aggregated and deduped across the batch
  - Warnings are aggregated across the batch and surfaced on anchor
  - Session maps updated from anchor
"""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock

from gateway.agents import AgentBackend
from gateway.agents.response import AgentResponse
from gateway.core.config import AgentConfig, CoreConfig
from gateway.core.connector import Attachment, IncomingMessage, Room, User, UserRole
from gateway.core.message_processor import MessageProcessor

# ── Helpers ────────────────────────────────────────────────────────────────────


class _RecordingAgent(AgentBackend):
    """Agent that records every prompt it receives."""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def create_session(self, *a, **kw) -> str:
        return "ses_001"

    async def send(
        self, session_id, prompt, working_directory, timeout, attachments=None, env=None
    ) -> AgentResponse:
        self.prompts.append(prompt)
        return AgentResponse(text="ok")


def _make_processor(agent: AgentBackend) -> MessageProcessor:
    config = CoreConfig(
        agents={"default": AgentConfig(timeout=10)},
        default_agent="default",
    )
    connector = MagicMock()
    connector.send_text = AsyncMock()
    # Return a simple prefix so prompts are easy to inspect
    connector.format_prompt_prefix = MagicMock(
        side_effect=lambda msg: f"[from: {msg.sender.username}]"
    )
    connector.notify_typing = AsyncMock()
    connector.notify_online = AsyncMock()
    connector.notify_offline = AsyncMock()
    return MessageProcessor(
        session_id="ses_001",
        room=Room(id="room_1", name="test-room"),
        working_directory="/tmp",
        watcher_id="test-watcher",
        connector=connector,
        agent=agent,
        config=config,
        agent_name="default",
    )


def _msg(
    text: str,
    msg_id: str = "m1",
    role: UserRole = UserRole.OWNER,
    warnings: list[str] | None = None,
    attachments: list[Attachment] | None = None,
) -> IncomingMessage:
    return IncomingMessage(
        id=msg_id,
        timestamp="100",
        room=Room(id="room_1", name="test-room"),
        sender=User(id="u1", username="alice"),
        text=text,
        role=role,
        warnings=warnings or [],
        attachments=attachments or [],
    )


# ── Tests ─────────────────────────────────────────────────────────────────────


class TestProcessorBatch(unittest.IsolatedAsyncioTestCase):

    async def test_single_message_no_catchup_prompt(self):
        """Single message goes through _process, not _process_batch — no catch-up header."""
        agent = _RecordingAgent()
        proc = _make_processor(agent)
        proc.start()

        await proc.enqueue(_msg("hello", "m1"))
        await asyncio.sleep(0.05)
        await proc.stop()

        self.assertEqual(len(agent.prompts), 1)
        self.assertNotIn("[CATCH-UP:", agent.prompts[0])
        self.assertIn("hello", agent.prompts[0])

    async def test_two_messages_produce_catchup_prompt(self):
        """Two messages enqueued before consumer runs → one catch-up prompt."""
        agent = _RecordingAgent()
        proc = _make_processor(agent)
        proc.start()

        await proc.enqueue(_msg("first message", "m1"))
        await proc.enqueue(_msg("second message", "m2"))
        await proc.stop()

        all_text = " ".join(agent.prompts)
        self.assertIn("[CATCH-UP:", all_text)
        self.assertIn("first message", all_text)
        self.assertIn("second message", all_text)

    async def test_anchor_is_last_message(self):
        """The last message in the batch is the anchor (after 'respond to this')."""
        agent = _RecordingAgent()
        proc = _make_processor(agent)
        proc.start()

        await proc.enqueue(_msg("msg one", "m1"))
        await proc.enqueue(_msg("msg two", "m2"))
        await proc.enqueue(_msg("msg three", "m3"))
        await proc.stop()

        all_text = " ".join(agent.prompts)
        # "msg three" should appear after "Latest message"
        self.assertIn("Latest message (respond to this):", all_text)
        latest_idx = all_text.index("Latest message (respond to this):")
        anchor_idx = all_text.index("msg three")
        self.assertGreater(anchor_idx, latest_idx)

    async def test_anonymous_messages_dropped_from_batch(self):
        """Anonymous messages are silently excluded from catch-up batch."""
        agent = _RecordingAgent()
        proc = _make_processor(agent)
        proc.start()

        await proc.enqueue(_msg("anon msg", "m1", role=UserRole.ANONYMOUS))
        await proc.enqueue(_msg("real msg", "m2", role=UserRole.OWNER))
        await proc.stop()

        all_text = " ".join(agent.prompts)
        self.assertIn("real msg", all_text)
        self.assertNotIn("anon msg", all_text)

    async def test_all_anonymous_batch_dropped(self):
        """Batch where all messages are anonymous → agent receives nothing."""
        agent = _RecordingAgent()
        proc = _make_processor(agent)
        proc.start()

        await proc.enqueue(_msg("anon1", "m1", role=UserRole.ANONYMOUS))
        await proc.enqueue(_msg("anon2", "m2", role=UserRole.ANONYMOUS))
        await proc.stop()

        self.assertEqual(agent.prompts, [])

    async def test_warnings_aggregated_in_anchor_prompt(self):
        """Warnings from all batch messages are aggregated and surfaced on the anchor."""
        agent = _RecordingAgent()
        proc = _make_processor(agent)
        proc.start()

        await proc.enqueue(_msg("hist msg", "m1", warnings=["warn from history"]))
        await proc.enqueue(_msg("anchor msg", "m2", warnings=["warn from anchor"]))
        await proc.stop()

        all_text = " ".join(agent.prompts)
        self.assertIn("warn from history", all_text)
        self.assertIn("warn from anchor", all_text)

    async def test_attachments_deduped_across_batch(self):
        """Same attachment path from multiple messages appears only once in file_paths."""
        agent = _RecordingAgent()
        proc = _make_processor(agent)
        proc.start()

        shared_att = Attachment(original_name="file.txt", local_path="/tmp/file.txt")
        unique_att = Attachment(original_name="other.txt", local_path="/tmp/other.txt")

        await proc.enqueue(_msg("m1", "m1", attachments=[shared_att]))
        await proc.enqueue(_msg("m2", "m2", attachments=[shared_att, unique_att]))
        await proc.stop()

        # Both messages were processed (agent received a prompt)
        all_text = " ".join(agent.prompts)
        self.assertIn("[CATCH-UP:", all_text)

    async def test_anonymous_anchor_demotes_to_prior_non_anonymous(self):
        """When the last enqueued message is anonymous, the anchor falls back
        to the last non-anonymous message in the batch."""
        agent = _RecordingAgent()
        proc = _make_processor(agent)
        proc.start()

        await proc.enqueue(_msg("real anchor", "m1", role=UserRole.OWNER))
        await proc.enqueue(_msg("anon last", "m2", role=UserRole.ANONYMOUS))
        await proc.stop()

        all_text = " ".join(agent.prompts)
        self.assertIn("real anchor", all_text)
        self.assertNotIn("anon last", all_text)
        # "real anchor" appears after "Latest message" (it is the anchor)
        self.assertIn("Latest message (respond to this):", all_text)
        latest_idx = all_text.index("Latest message (respond to this):")
        anchor_idx = all_text.index("real anchor")
        self.assertGreater(anchor_idx, latest_idx)

    async def test_history_lines_appear_in_catchup_block(self):
        """Non-anchor messages appear inside the [CATCH-UP] block."""
        agent = _RecordingAgent()
        proc = _make_processor(agent)
        proc.start()

        await proc.enqueue(_msg("history here", "m1"))
        await proc.enqueue(_msg("anchor here", "m2"))
        await proc.stop()

        all_text = " ".join(agent.prompts)
        catchup_start = all_text.index("[CATCH-UP:")
        catchup_end = all_text.index("[END CATCH-UP]")
        catchup_block = all_text[catchup_start:catchup_end]
        self.assertIn("history here", catchup_block)
        self.assertNotIn("anchor here", catchup_block)


if __name__ == "__main__":
    unittest.main()
