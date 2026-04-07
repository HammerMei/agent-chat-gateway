"""E2E Test: File Upload — user sends a file, agent acknowledges it.

Runs twice via e2e_room fixture:
  test_file_upload[dm]      → DM room → OpenCode agent
  test_file_upload[channel] → #acg-e2e-claude → Claude Code agent
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
from rc_client import RCClient

FIXTURES_DIR = Path(__file__).parent / "fixtures"
TEST_FILE = FIXTURES_DIR / "test_upload.txt"


@pytest.mark.e2e
def test_file_upload(
    acg: None,
    test_client: RCClient,
    e2e_room: dict[str, Any],
) -> None:
    """Agent acknowledges a file uploaded by the user.

    The test uploads test_upload.txt and asks the agent to describe it.
    Success criterion: the bot replies with a non-trivial message (> 10 chars),
    confirming it received and processed the file.
    """
    before_ts = int(time.time() * 1000)

    # RC attaches the description as a caption alongside the file
    description = (
        e2e_room["mention_prefix"]
        + "I am sending you a file. Please confirm you received it and describe its contents."
    )
    test_client.upload_file(
        e2e_room["id"],
        TEST_FILE,
        description=description,
    )

    bot_msg = test_client.poll_for_message(
        e2e_room["id"],
        before_ts,
        predicate=lambda m: m["u"]["username"] == "acg_bot",
        timeout=120,
        room_type=e2e_room["type"],
    )

    # Agent should produce a meaningful response about the file
    assert len(bot_msg["msg"]) > 10, (
        f"Bot reply too short — agent may not have processed the file. "
        f"Got: {bot_msg['msg']!r}"
    )
    # Bonus: check the agent mentioned the file marker or content
    # (not strictly required — the agent may paraphrase)
    lower = bot_msg["msg"].lower()
    assert any(
        kw in lower
        for kw in ["file", "content", "upload", "test", "marker", "received", "acg"]
    ), f"Bot reply doesn't seem to reference the file: {bot_msg['msg']!r}"
