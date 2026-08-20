"""E2E Test: Mattermost delivery tracks membership, not readability (§6.2).

The runtime leans on this. `MattermostConnector.supports_unsolicited_inbound()`
returns True on the grounds that one socket carries every channel the bot
belongs to "and only those", so the creation path performs **no REST
membership check** — a channel that produces an event is by definition one the
bot is in. If Mattermost ever delivered events for merely-readable channels,
ACG would silently start answering in rooms nobody invited it to.

`scripts/probe_a2_mm.py` already verifies this at the PLATFORM level, and §6.2
records the result. This test is not a copy of that: it verifies the same
property **through the whole ACG runtime** — a rule that matches the channel,
a poster on the allow-list, a bot that is mentioned — and the probe cannot,
because the probe does not run ACG.

Three things make it a test rather than a sleep-and-hope:

1. **The rule claims the outside channel.** `acg-e2e-mm-*` matches it. So the
   rule declining is not on the list of explanations for silence.
2. **The poster is the allow-listed human, in both channels.** Posting as the
   admin would have been easier and would have quietly broken the test:
   `mmadmin` is not in `allowed_users.owners`, so the sender filter would
   explain the silence just as well as the missing event.
3. **A round trip in the member channel is the liveness control**, posted
   *after* the outside post. Waiting for that reply proves ACG was alive and
   consuming events after the outside message — which is what makes this a
   causal bound instead of a magic sleep. A pure negative assertion would pass
   just as happily against a dead gateway.
"""
from __future__ import annotations

import time
from typing import Any

import pytest
from conftest import MM_CONNECTOR_NAME, acg_watcher_list
from mm_client import MMClient


@pytest.mark.e2e
def test_a_post_in_a_readable_unjoined_channel_produces_nothing(
    mm_connected: None,
    mm_setup: dict[str, Any],
    mm_test_client: MMClient,
    mm_admin_client: MMClient,
    mm_bot_client: MMClient,
) -> None:
    outside_id = mm_setup["outside_channel_id"]
    member_id = mm_setup["member_channel_id"]
    bot_id = mm_setup["bot_user_id"]

    # ── Preconditions, asserted rather than assumed ───────────────────────────
    # Without these, "no reply" is indistinguishable from "no access", and the
    # test would keep passing after someone joins the bot to the channel or
    # makes it private.
    assert not mm_admin_client.is_channel_member(outside_id, bot_id), (
        f"the bot is a MEMBER of #{mm_setup['outside_channel']} — this test "
        "requires it to be readable but unjoined; re-run mm_setup.py, which "
        "removes it"
    )
    assert mm_admin_client.is_channel_member(member_id, bot_id), (
        f"the bot is not in #{mm_setup['member_channel']}, so the liveness "
        "control below cannot work"
    )
    # Readability from the BOT's own token — the half §6.2 insists on, because
    # an admin-token read would prove nothing about what the bot can see.
    readable = mm_bot_client.get_posts(outside_id)
    assert isinstance(readable, list), (
        f"the bot cannot read #{mm_setup['outside_channel']}, so silence would "
        "be explained by access rather than by delivery"
    )

    before_ts = int(time.time() * 1000)

    # ── 1. The message that must vanish ───────────────────────────────────────
    mm_test_client.post_message(
        outside_id,
        f"@{mm_setup['bot_username']} respond with exactly the single word 'leak'",
    )

    # ── 2. The liveness control, posted afterwards ────────────────────────────
    mm_test_client.post_message(
        member_id,
        f"@{mm_setup['bot_username']} respond with exactly the single word 'pong'",
    )
    reply = mm_test_client.poll_for_message(
        member_id,
        before_ts,
        predicate=lambda p: (
            p.get("user_id") == bot_id and "pong" in p.get("message", "").lower()
        ),
        timeout=120,
    )
    assert "pong" in reply["message"].lower()

    # ── 3. Now the negatives mean something ───────────────────────────────────
    leaked = [
        p
        for p in mm_bot_client.get_posts(outside_id, since_ms=before_ts)
        if p.get("user_id") == bot_id
    ]
    assert leaked == [], (
        f"the bot posted in #{mm_setup['outside_channel']}, a channel it is "
        f"not a member of: {[p.get('message') for p in leaked]!r}"
    )

    # A watcher for the outside channel would mean the event arrived even if
    # the reply did not — a different and worse failure than a missing reply,
    # because the room would then be tracked, persisted and woken on schedule.
    watchers = acg_watcher_list()
    outside_handle = f"{MM_CONNECTOR_NAME}:{mm_setup['outside_channel']}"
    member_handle = f"{MM_CONNECTOR_NAME}:{mm_setup['member_channel']}"
    # Guard on the guard: if the member watcher is missing, the listing is not
    # telling us what we think it is, and the absence below proves nothing.
    assert member_handle in watchers, (
        f"expected a watcher {member_handle!r} after the round trip above — "
        f"'list --all' says:\n{watchers}"
    )
    assert outside_handle not in watchers, (
        f"a watcher materialized for a channel the bot never joined "
        f"({outside_handle!r}) — 'list --all' says:\n{watchers}"
    )
