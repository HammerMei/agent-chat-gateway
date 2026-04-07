"""E2E Test: Permission approve / deny flow.

Parameterized over two backends:
  test_permission_approve[claude]    → #acg-e2e-permission           → Claude Code
  test_permission_approve[opencode]  → #acg-e2e-opencode-permission  → OpenCode
  test_permission_deny[claude]       → #acg-e2e-permission           → Claude Code
  test_permission_deny[opencode]     → #acg-e2e-opencode-permission  → OpenCode

Both channels are bound to agents with empty owner_allowed_tools, so EVERY
tool call triggers the approval flow, making the tests deterministic.
"""
from __future__ import annotations

import time
from typing import Any

import pytest
from rc_client import RCClient

BOT_USERNAME = "acg_bot"
ECHO_MARKER = "e2e_permission_test_ok"
DENY_MARKER = "e2e_should_be_denied"


@pytest.fixture(scope="session", params=["claude", "opencode"])
def permission_channel_id(
    request: pytest.FixtureRequest,
    rc_setup: dict[str, Any],
    test_client: RCClient,
) -> str:
    """Returns the room _id for the permission test channel.

    claude    → #acg-e2e-permission           (Claude Code, empty allowlist)
    opencode  → #acg-e2e-opencode-permission  (OpenCode, empty allowlist)
    """
    if request.param == "claude":
        channel_name = rc_setup["permission_channel"]
    else:
        channel_name = rc_setup["opencode_permission_channel"]

    ch = test_client.get_channel(channel_name)
    if ch is None:
        pytest.fail(
            f"Permission channel '#{channel_name}' not found. "
            "Run 'make e2e-up' first."
        )
    return ch["_id"]


@pytest.mark.e2e
def test_permission_approve(
    acg: None,
    test_client: RCClient,
    permission_channel_id: str,
) -> None:
    """Owner approves → agent completes the task.

    Flow:
        1. test_user asks agent to echo a unique marker.
        2. Agent requests permission (empty allowlist → every tool needs approval).
        3. test_client extracts the 4-char permission ID from the bot message.
        4. test_client sends 'approve <id>'.
        5. Agent runs the command and the marker appears in the room.
    """
    before_ts = int(time.time() * 1000)

    test_client.post_message(
        permission_channel_id,
        f"@{BOT_USERNAME} please run the shell command: echo {ECHO_MARKER}",
    )

    perm_msg = test_client.poll_for_message(
        permission_channel_id,
        before_ts,
        predicate=lambda m: (
            m["u"]["username"] == BOT_USERNAME
            and test_client.extract_permission_id(m) is not None
        ),
        timeout=90,
        room_type="channel",
    )
    perm_id = test_client.extract_permission_id(perm_msg)
    assert perm_id, f"No permission ID in bot message: {perm_msg['msg']!r}"

    test_client.post_message(permission_channel_id, f"approve {perm_id}")

    result_msg = test_client.poll_for_message(
        permission_channel_id,
        before_ts,
        predicate=lambda m: (
            m["u"]["username"] == BOT_USERNAME and ECHO_MARKER in m["msg"]
        ),
        timeout=120,
        room_type="channel",
    )
    assert ECHO_MARKER in result_msg["msg"]


@pytest.mark.e2e
def test_permission_deny(
    acg: None,
    test_client: RCClient,
    permission_channel_id: str,
) -> None:
    """Owner denies → task is NOT completed.

    Flow:
        1. test_user asks agent to echo the deny marker.
        2. Agent requests permission.
        3. test_client sends 'deny <id>'.
        4. Agent acknowledges denial; deny marker does NOT appear.
    """
    before_ts = int(time.time() * 1000)

    test_client.post_message(
        permission_channel_id,
        f"@{BOT_USERNAME} please run the shell command: echo {DENY_MARKER}",
    )

    perm_msg = test_client.poll_for_message(
        permission_channel_id,
        before_ts,
        predicate=lambda m: (
            m["u"]["username"] == BOT_USERNAME
            and test_client.extract_permission_id(m) is not None
        ),
        timeout=90,
        room_type="channel",
    )
    perm_id = test_client.extract_permission_id(perm_msg)
    assert perm_id

    test_client.post_message(permission_channel_id, f"deny {perm_id}")

    denial_msg = test_client.poll_for_message(
        permission_channel_id,
        before_ts,
        predicate=lambda m: (
            m["u"]["username"] == BOT_USERNAME and m["_id"] != perm_msg["_id"]
        ),
        timeout=60,
        room_type="channel",
    )
    assert DENY_MARKER not in denial_msg["msg"], (
        f"Deny marker found after denial — task should not have run. "
        f"Got: {denial_msg['msg']!r}"
    )
