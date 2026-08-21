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
from conftest import MM_CONNECTOR_NAME, acg_watcher_list
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
    # `before_ts` is what excludes a reply from an earlier test; the "pong"
    # substring cannot, since test_mm_membership_delivery.py asks for the same
    # word in the same channel. What the substring does buy is that a bot
    # message which is NOT the answer — a permission prompt, an error notice —
    # does not satisfy the wait.
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

    # Close the loop where the `%40` bug was actually found: by reading a
    # populated `list --all` by hand. Nothing in the suite asserted a DM
    # watcher's HANDLE, and this test creates exactly the room whose handle had
    # regressed to `mm-e2e:dm:%40test_user` — so the one observable that would
    # have caught it is checked here, against the real runtime rather than
    # against a helper the test calls itself.
    #
    # Only for the DM leg: a channel watcher's handle is already asserted by
    # test_mm_membership_delivery.py.
    if mm_room["type"] == "dm":
        expected = f"{MM_CONNECTOR_NAME}:dm:{mm_setup['test_user_username']}"
        watchers = acg_watcher_list()
        assert expected in watchers, (
            f"expected a watcher {expected!r} after this round trip.\n"
            "If the handle shows `%40` the connector is carrying Mattermost's "
            "`@` prefix into the DM counterpart again. If it is missing "
            "entirely, note that `participants` is frozen at creation — a "
            "record created before that fix keeps the old spelling until it is "
            f"expired: make e2e-acg C=\"expire '<old name>'\"\n"
            f"'list --all' says:\n{watchers}"
        )
