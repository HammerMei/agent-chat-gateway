"""E2E Test: Thread — conversation continues inside a thread.

Uses #acg-e2e-claude (Claude Code agent) because threads are a channel-native
feature; DMs don't have threads in Rocket.Chat.

Flow:
    1. test_user sends an initial message to the channel.
    2. Bot replies at the top level.
    3. test_user replies *in the thread* anchored to the bot's reply.
    4. Bot replies *in the same thread*.
    5. Assert: the second bot reply is a thread message (has tmid field).
"""
from __future__ import annotations

import time
from typing import Any

import pytest
from rc_client import RCClient

BOT_USERNAME = "acg_bot"


@pytest.mark.e2e
def test_thread_reply(
    acg: None,
    test_client: RCClient,
    rc_setup: dict[str, Any],
) -> None:
    """Bot continues a conversation inside a thread."""
    ch = test_client.get_channel(rc_setup["claude_channel"])
    if ch is None:
        pytest.fail(
            f"Channel '#{rc_setup['claude_channel']}' not found. "
            "Run 'make e2e-up' first."
        )
    channel_id = ch["_id"]

    # ── Step 1: Send initial message ─────────────────────────────────────────
    before_ts = int(time.time() * 1000)
    test_client.post_message(
        channel_id,
        f"@{BOT_USERNAME} reply with exactly: 'thread started'",
    )

    # ── Step 2: Wait for bot's first reply ───────────────────────────────────
    bot_first = test_client.poll_for_message(
        channel_id,
        before_ts,
        predicate=lambda m: m["u"]["username"] == BOT_USERNAME,
        timeout=120,
        room_type="channel",
    )
    assert bot_first, "Bot did not reply to the initial message"

    # ── Step 3: Reply inside the thread ──────────────────────────────────────
    # tmid = the bot's message (thread root is anchored to bot's reply)
    # @mention needed — channel filter requires it even inside threads
    test_client.post_message(
        channel_id,
        f"@{BOT_USERNAME} reply with exactly: 'thread continued'",
        tmid=bot_first["_id"],
    )

    # ── Step 4 & 5: Wait for bot to reply in the thread ──────────────────────
    deadline = time.monotonic() + 120
    bot_thread_replies: list[dict[str, Any]] = []

    while time.monotonic() < deadline:
        thread_msgs = test_client.get_thread_messages(bot_first["_id"])
        bot_thread_replies = [
            m for m in thread_msgs if m["u"]["username"] == BOT_USERNAME
        ]
        if bot_thread_replies:
            break
        time.sleep(2)

    assert bot_thread_replies, (
        f"Bot did not reply in the thread (tmid={bot_first['_id']}) within 120s"
    )

    # The bot's thread reply should be about "thread continued"
    combined_text = " ".join(m["msg"] for m in bot_thread_replies).lower()
    assert "thread" in combined_text or "continued" in combined_text, (
        f"Bot thread reply doesn't reference the thread context: "
        f"{[m['msg'] for m in bot_thread_replies]}"
    )
