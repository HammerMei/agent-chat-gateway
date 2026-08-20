"""E2E Test: Mattermost ping / pong — basic message exchange.

Runs twice via the mm_room fixture:
  test_mm_ping_pong[dm]      → DM with acg_bot
  test_mm_ping_pong[channel] → #acg-e2e-mm-claude

The Rocket.Chat twin of this test is `test_ping_pong.py`. Keeping them as two
files rather than one parameterized over both platforms is deliberate: the two
clients differ in their message shape (`m["u"]["username"]`/`m["msg"]` versus
`post["user_id"]`/`post["message"]`), and a single test papering over that
would hide the very difference the MM suite exists to exercise.
"""
from __future__ import annotations

import time
from typing import Any

import pytest
from mm_client import MMClient


@pytest.mark.e2e
def test_mm_ping_pong(
    mm_connected: None,
    mm_setup: dict[str, Any],
    mm_test_client: MMClient,
    mm_room: dict[str, Any],
) -> None:
    """Bot responds with 'pong' when asked, on Mattermost."""
    bot = mm_test_client.get_user(mm_setup["bot_username"])
    assert bot, f"bot user {mm_setup['bot_username']!r} not found on Mattermost"

    before_ts = int(time.time() * 1000)
    prompt = mm_room["mention_prefix"] + "respond with exactly the single word 'pong'"
    mm_test_client.post_message(mm_room["id"], prompt)

    # Match on the bot's USER ID, not a username: a Mattermost post carries
    # `user_id` and no username at all, so resolving it once above is cheaper
    # and less brittle than a per-post lookup.
    #
    # The "pong" substring is load-bearing for the same reason as in the RC
    # twin — it stops a stale reply from an earlier test in the same session
    # from satisfying the wait.
    reply = mm_test_client.poll_for_message(
        mm_room["id"],
        before_ts,
        predicate=lambda p: (
            p.get("user_id") == bot["id"] and "pong" in p.get("message", "").lower()
        ),
        timeout=120,
    )

    assert "pong" in reply["message"].lower(), (
        f"Expected 'pong' in bot reply, got: {reply['message']!r}"
    )
