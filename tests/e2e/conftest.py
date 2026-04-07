"""pytest fixtures for E2E tests.

Session-scoped:
    rc_setup    — runs setup.py; verifies RC is reachable
    acg         — waits for ACG Docker container to be ready

Function-scoped:
    test_client  — RCClient logged in as test_user
    admin_client — RCClient logged in as admin
    e2e_room     — parameterized: "dm" (→ OpenCode) or "channel" (→ Claude Code)
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

# Allow importing rc_client and setup from the same e2e directory
sys.path.insert(0, str(Path(__file__).parent))
from rc_client import RCClient
from setup import RC_URL
from setup import setup as _run_setup

# ── Constants ─────────────────────────────────────────────────────────────────

E2E_DIR = Path(__file__).parent
COMPOSE_FILE = str(E2E_DIR / "docker-compose.yml")
ACG_CONTAINER = "acg-e2e"
BOT_USERNAME = "acg_bot"
ACG_READY_TIMEOUT = 180  # seconds — includes OpenCode pre-warm (~60s)
ACG_READY_INTERVAL = 5


# ── Session fixtures ──────────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def rc_setup() -> dict[str, Any]:
    """Run E2E setup and return config dict.

    Expects RC to already be running (started by Makefile / CI).
    If RC is not reachable, the fixture fails with a clear message.
    """
    rc_url = os.environ.get("E2E_RC_URL", RC_URL)
    try:
        return _run_setup(rc_url)
    except RuntimeError as exc:
        pytest.fail(
            f"E2E setup failed — is RC running?\n"
            f"  Start with: make e2e-up\n"
            f"  Error: {exc}"
        )


@pytest.fixture(scope="session")
def acg(rc_setup: dict[str, Any]) -> None:
    """Wait for the ACG Docker container to be ready, then warm up agents.

    Expects the container to already be started (by Makefile / CI).
    Polls `docker exec acg-e2e agent-chat-gateway status` until it succeeds
    or times out.

    After ACG reports ready, sends a warm-up ping to both the DM (OpenCode)
    and the team channel (Claude Code).  OpenCode starts its subprocess lazily
    on the first request, so without this warm-up the first real test can time
    out waiting for the cold-start initialisation to complete.
    """
    print(f"\n[acg] Waiting for ACG container '{ACG_CONTAINER}' ...", flush=True)
    _wait_for_acg(timeout=ACG_READY_TIMEOUT, interval=ACG_READY_INTERVAL)
    print("[acg] ACG is ready.", flush=True)

    # ── Warm-up: trigger both agents so their subprocesses are initialised ────
    _warmup_agents(rc_setup)

    yield
    # Do NOT stop the container here — Makefile / CI handles lifecycle.
    # This lets tests be re-run quickly without restarting ACG.


def _warmup_agents(rc_setup: dict[str, Any]) -> None:
    """Send a warm-up ping to DM (OpenCode) and channel (Claude Code).

    OpenCode initialises its subprocess lazily on the first message, which can
    take 60–90 s.  This function fires a simple 'pong' request at both agents
    and waits up to 120 s for each response — ensuring they are fully warmed up
    before the test suite starts.  Failures here are non-fatal (a warning is
    printed) so that individual tests can still provide actionable failure info.
    """
    rc_url = rc_setup["rc_url"]
    warmup_timeout = 120

    with RCClient(rc_url) as c:
        c.login(rc_setup["test_user_username"], rc_setup["test_user_password"])

        # ── DM → OpenCode ─────────────────────────────────────────────────────
        try:
            print("[acg] Warming up OpenCode (DM) ...", flush=True)
            dm_room_id = c.get_dm_room_id(BOT_USERNAME)
            before_ts = int(time.time() * 1000)
            c.post_message(dm_room_id, "respond with exactly the single word 'ready'")
            c.poll_for_message(
                dm_room_id,
                before_ts,
                predicate=lambda m: m["u"]["username"] == BOT_USERNAME,
                timeout=warmup_timeout,
                room_type="dm",
            )
            print("[acg] OpenCode (DM) warm-up done.", flush=True)
        except Exception as exc:
            print(f"[acg] WARNING: OpenCode warm-up failed: {exc}", flush=True)

        # ── Channel → Claude Code ──────────────────────────────────────────────
        try:
            print("[acg] Warming up Claude Code (channel) ...", flush=True)
            ch = c.get_channel(rc_setup["claude_channel"])
            if ch:
                before_ts = int(time.time() * 1000)
                c.post_message(
                    ch["_id"],
                    f"@{BOT_USERNAME} respond with exactly the single word 'ready'",
                )
                c.poll_for_message(
                    ch["_id"],
                    before_ts,
                    predicate=lambda m: m["u"]["username"] == BOT_USERNAME,
                    timeout=warmup_timeout,
                    room_type="channel",
                )
                print("[acg] Claude Code (channel) warm-up done.", flush=True)
        except Exception as exc:
            print(f"[acg] WARNING: Claude Code warm-up failed: {exc}", flush=True)


def _wait_for_acg(timeout: float, interval: float) -> None:
    """Poll docker exec until agent-chat-gateway status returns 0."""
    deadline = time.monotonic() + timeout
    last_output = ""
    while time.monotonic() < deadline:
        result = subprocess.run(
            ["docker", "exec", ACG_CONTAINER, "agent-chat-gateway", "status"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return
        last_output = (result.stdout + result.stderr).strip()
        time.sleep(interval)

    # On timeout, dump ACG logs for debugging
    logs = subprocess.run(
        ["docker", "logs", "--tail", "50", ACG_CONTAINER],
        capture_output=True,
        text=True,
    ).stdout
    pytest.fail(
        f"ACG did not become ready within {timeout}s.\n"
        f"Last status output: {last_output}\n"
        f"Container logs (last 50 lines):\n{logs}"
    )


# ── Function-scoped fixtures ──────────────────────────────────────────────────


@pytest.fixture(scope="session")
def test_client(rc_setup: dict[str, Any]) -> RCClient:
    """RC client logged in as test_user.

    Session-scoped to avoid RC's login rate limit (429) when running many tests.
    The httpx.Client maintains a persistent connection pool for the session.
    """
    c = RCClient(rc_setup["rc_url"])
    c.login(rc_setup["test_user_username"], rc_setup["test_user_password"])
    yield c
    c.close()


@pytest.fixture(scope="session")
def admin_client(rc_setup: dict[str, Any]) -> RCClient:
    """RC client logged in as admin.

    Session-scoped to avoid RC's login rate limit (429).
    """
    c = RCClient(rc_setup["rc_url"])
    c.login(rc_setup["admin_username"], rc_setup["admin_password"])
    yield c
    c.close()


@pytest.fixture(scope="session", params=["dm", "channel"])
def e2e_room(
    request: pytest.FixtureRequest,
    rc_setup: dict[str, Any],
    test_client: RCClient,
) -> dict[str, Any]:
    """Parameterized room fixture: runs each test twice.

    "dm"      → DM room between test_user and acg_bot → OpenCode agent
    "channel" → #acg-e2e-claude public channel        → Claude Code agent

    Returned dict:
        id:             RC room _id
        type:           "dm" or "channel"
        agent:          "opencode" or "claude"
        name:           human-readable label
        mention_prefix: "" for DM, "@acg_bot " for channel
                        (channel messages need @bot mention to be processed)
    """
    if request.param == "dm":
        room_id = test_client.get_dm_room_id(BOT_USERNAME)
        return {
            "id": room_id,
            "type": "dm",
            "agent": "opencode",
            "name": f"DM with {BOT_USERNAME}",
            "mention_prefix": "",
        }
    else:
        ch = test_client.get_channel(rc_setup["claude_channel"])
        if ch is None:
            pytest.fail(
                f"Channel '#{rc_setup['claude_channel']}' not found. "
                "Run 'make e2e-up' first."
            )
        return {
            "id": ch["_id"],
            "type": "channel",
            "agent": "claude",
            "name": rc_setup["claude_channel"],
            "mention_prefix": f"@{BOT_USERNAME} ",
        }
