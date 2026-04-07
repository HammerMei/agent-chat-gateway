"""E2E Test: Ping / Pong — basic message exchange.

Runs twice via e2e_room fixture:
  test_ping_pong[dm]      → DM room → OpenCode agent
  test_ping_pong[channel] → #acg-e2e-claude → Claude Code agent
"""
from __future__ import annotations

import time
from typing import Any

import pytest
from rc_client import RCClient


@pytest.mark.e2e
def test_ping_pong(
    acg: None,
    test_client: RCClient,
    e2e_room: dict[str, Any],
) -> None:
    """Bot responds with 'pong' when asked."""
    before_ts = int(time.time() * 1000)

    prompt = e2e_room["mention_prefix"] + "respond with exactly the single word 'pong'"
    test_client.post_message(e2e_room["id"], prompt)

    # Poll for a bot message that actually contains "pong" —
    # avoids picking up stale replies from a previous test in the same session.
    bot_msg = test_client.poll_for_message(
        e2e_room["id"],
        before_ts,
        predicate=lambda m: (
            m["u"]["username"] == "acg_bot" and "pong" in m["msg"].lower()
        ),
        timeout=120,
        room_type=e2e_room["type"],
    )

    assert "pong" in bot_msg["msg"].lower(), (
        f"Expected 'pong' in bot reply, got: {bot_msg['msg']!r}"
    )
